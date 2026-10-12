# -*- coding: utf-8 -*-
# Пересылает письма из почтового ящика в тему группы Телеграма.
# Настройки берутся из переменных среды:
#   IMAP_HOST      сервер почты, например imap.mail.ru
#   IMAP_USER      адрес ящика
#   IMAP_PASSWORD  пароль для внешних приложений (не обычный пароль от почты)
#   TG_TOKEN       токен бота от @BotFather
#   TG_CHAT_ID     номер группы
#   TG_THREAD_ID   номер темы в группе (можно не задавать, тогда в общий чат)
#   ONLY_FROM      необязательно: через запятую куски адресов, от кого пересылать
#   SKIP_FROM      от кого не пересылать; по умолчанию ".mail.ru", то есть служебные
#                  письма самой почты (security@id.mail.ru и подобные)
#
# Как устроено. Очередь это все письма без отметки «отвечено» во всех папках, кроме
# отправленных, черновиков, корзины и архива. Прочитано письмо или нет, значения не
# имеет. Обработанное письмо получает отметку и переносится в папку Pereslano
# (служебное в Sluzhebnoe, непересылаемое в Ne-pereslano), так что в почте всё цело.
#
# Проверка без почты и без Телеграма:
#   python pochta_v_telegram.py --test письмо.eml
import base64
import email
import email.policy
import html
import imaplib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

TG_TEXT_LIMIT = 3900                  # у Телеграма предел 4096 знаков на сообщение
TG_FILE_LIMIT = 45 * 1024 * 1024      # бот может отправить файл до 50 МБ
MAX_PER_RUN = 10                      # не больше писем за один запуск
INLINE_MIN = 15 * 1024                # картинки в теле письма мельче этого не шлём (значки подписей)
DONE, SKIPPED, FAILED = "Pereslano", "Sluzhebnoe", "Ne-pereslano"
CLOUD_RE = re.compile(r"https?://(?:cloud\.mail\.ru|files\.mail\.ru|[\w.-]*attachmail\.ru)/", re.I)


class TextFailed(Exception):
    """Не ушёл текст письма."""


class MailDown(Exception):
    """Почта недоступна: нет связи или не пускает."""


# ---------- разбор письма ----------

def dec(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def html_to_text(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h\d|blockquote)>", "\n", s)
    s = re.sub(r"(?is)<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
               lambda m: "%s (%s)" % (re.sub(r"<[^>]+>", "", m.group(2)).strip(), m.group(1)), s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def tidy(s):
    s = s.replace("\r", "")
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def part_text(part):
    try:
        return part.get_content()
    except Exception:
        return (part.get_payload(decode=True) or b"").decode("utf-8", "replace")


def parse(raw):
    """Возвращает (шапка, текст, [(имя файла, байты)], адрес отправителя)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = parseaddr(dec(msg.get("From")))
    subject = dec(msg.get("Subject")) or "(без темы)"
    try:
        when = parsedate_to_datetime(msg.get("Date")).strftime("%d.%m.%Y %H:%M")
    except Exception:
        when = ""
    plain, rich, files, pictures = [], [], [], 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        fname = part.get_filename()
        ctype = part.get_content_type()
        if fname or part.get_content_disposition() == "attachment":
            files.append((dec(fname) or "вложение", part.get_payload(decode=True) or b""))
        elif ctype == "text/plain":
            plain.append(part_text(part))
        elif ctype == "text/html":
            rich.append(part_text(part))
        elif ctype.startswith("image/"):
            data = part.get_payload(decode=True) or b""
            if len(data) >= INLINE_MIN:
                pictures += 1
                files.append(("картинка из письма %d.%s" % (pictures, ctype.split("/")[1].split("+")[0]), data))
    body = "\n".join(plain) if any(p.strip() for p in plain) else html_to_text("\n".join(rich))
    head = "Письмо на почту группы\nОт: %s\nТема: %s" % ((name + " " if name else "") + "<" + addr + ">", subject)
    if when:
        head += "\nДата: " + when
    return head, tidy(body), files, addr


def chunks(text, limit=TG_TEXT_LIMIT):
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out


def skipped(addr, skip_from):
    """Пункт с точкой в начале сравнивается с концом домена, остальные ищутся в адресе."""
    addr = addr.lower()
    domain = addr.rsplit("@", 1)[-1]
    for x in skip_from:
        if x.startswith("."):
            if domain.endswith(x):
                return True
        elif x in addr:
            return True
    return False


# ---------- Телеграм ----------

class Telegram:
    def __init__(self, token, chat_id, thread_id=None, dry=False):
        self.url = "https://api.telegram.org/bot%s/" % token
        self.chat_id, self.thread_id, self.dry = chat_id, thread_id, dry

    def _base(self):
        d = {"chat_id": self.chat_id}
        if self.thread_id:
            d["message_thread_id"] = self.thread_id
        return d

    def _call(self, method, build):
        """build() собирает запрос заново: номер темы между попытками может измениться."""
        for attempt in range(4):
            data, content_type = build()
            req = urllib.request.Request(self.url + method, data=data, headers={"Content-Type": content_type})
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                try:
                    info = json.loads(text)
                except Exception:
                    info = {}
                wait = (info.get("parameters") or {}).get("retry_after")
                if e.code == 429 and wait and attempt < 3:      # просят подождать
                    time.sleep(min(int(wait) + 1, 60))
                    continue
                if e.code == 400 and "thread not found" in text and self.thread_id:
                    self.thread_id = None                        # тему удалили: пишем в общий чат
                    continue
                raise RuntimeError("Телеграм ответил %s: %s" % (e.code, text[:300]))
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(5)
                    continue
                raise RuntimeError("нет связи с Телеграмом: %s" % e.reason)
        raise RuntimeError("Телеграм не принял запрос за четыре попытки")

    def text(self, text):
        if self.dry:
            print("--- сообщение (%d знаков) ---\n%s" % (len(text), text))
            return

        def build():
            d = self._base()
            d.update({"text": text, "disable_web_page_preview": True})
            return json.dumps(d).encode("utf-8"), "application/json"
        self._call("sendMessage", build)

    def document(self, name, data):
        if self.dry:
            print("--- файл: %s, %d байт ---" % (name, len(data)))
            return
        safe = name.replace('"', "'").replace("\r", " ").replace("\n", " ")

        def build():
            boundary = uuid.uuid4().hex
            body = b""
            for k, v in self._base().items():
                body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode("utf-8")
            body += ("--%s\r\nContent-Disposition: form-data; name=\"document\"; filename=\"%s\"\r\n"
                     "Content-Type: application/octet-stream\r\n\r\n" % (boundary, safe)).encode("utf-8")
            return body + data + ("\r\n--%s--\r\n" % boundary).encode("utf-8"), "multipart/form-data; boundary=" + boundary
        self._call("sendDocument", build)


# ---------- большие файлы ----------

def mb(n):
    return max(1, round(n / (1024 * 1024)))


def need_pypdf():
    try:
        import pypdf
    except ImportError:
        # На сервере GitHub системный питон не даёт ставить пакеты без этого ключа.
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--user", "pypdf"]
        if subprocess.run(cmd + ["--break-system-packages"]).returncode != 0:
            subprocess.run(cmd, check=True)
        import site
        if site.getusersitepackages() not in sys.path:
            sys.path.append(site.getusersitepackages())
        import pypdf
    return pypdf.PdfReader, pypdf.PdfWriter


def split_pdf(name, data):
    """Режет PDF по листам на части, каждая из которых сама открывается как PDF."""
    PdfReader, PdfWriter = need_pypdf()
    reader = PdfReader(io.BytesIO(data), strict=False)
    n = len(reader.pages)
    per = max(1, int(n * (TG_FILE_LIMIT * 0.75) / len(data)))
    parts, i = [], 0
    while i < n:
        k = min(per, n - i)
        while True:
            w = PdfWriter()
            for p in range(i, i + k):
                w.add_page(reader.pages[p])
            buf = io.BytesIO()
            w.write(buf)
            b = buf.getvalue()
            if len(b) <= TG_FILE_LIMIT or k == 1:
                break
            k = max(1, k // 2)
        if len(b) > TG_FILE_LIMIT:
            raise ValueError("один лист больше предела")
        parts.append((i + 1, i + k, b))
        i += k
    base = name[:-4] if name.lower().endswith(".pdf") else name
    return [("%s (часть %d из %d, листы %d-%d).pdf" % (base, j + 1, len(parts), a, z), d)
            for j, (a, z, d) in enumerate(parts)]


def split_raw(name, data):
    step = TG_FILE_LIMIT
    count = (len(data) + step - 1) // step
    return [("%s.%03d" % (name, j + 1), data[j * step:(j + 1) * step]) for j in range(count)]


def member_name(info):
    name = info.filename
    if not info.flag_bits & 0x800:
        try:
            name = name.encode("cp437").decode("cp866")
        except Exception:
            pass
    return os.path.basename(name) or "файл"


def send_file(tg, name, data, depth=0):
    """Шлёт вложение любого размера. Ошибка одного файла не мешает остальным."""
    try:
        if not data:
            return
        if len(data) <= TG_FILE_LIMIT:
            tg.document(name, data)
            return
        low = name.lower()
        if low.endswith(".zip") and depth < 2:
            try:
                z = zipfile.ZipFile(io.BytesIO(data))
                infos = [i for i in z.infolist() if not i.is_dir()]
                if 0 < len(infos) <= 200 and sum(i.file_size for i in infos) <= 1500 * 1024 * 1024:
                    members = [(member_name(i), z.read(i)) for i in infos]
                    tg.text("Архив «%s» (%d МБ) больше, чем бот может отправить. Ниже его содержимое по файлам: %d."
                            % (name, mb(len(data)), len(members)))
                    for mname, mdata in members:
                        send_file(tg, mname, mdata, depth + 1)
                    return
            except Exception:
                pass                                    # архив с паролем или битый: пойдёт кусками
        if low.endswith(".pdf"):
            try:
                parts = split_pdf(name, data)
                tg.text("Файл «%s» (%d МБ) больше, чем бот может отправить одним куском. Ниже он же по частям: %d. "
                        "Каждая часть открывается сама." % (name, mb(len(data)), len(parts)))
                for pname, pdata in parts:
                    tg.document(pname, pdata)
                return
            except Exception:
                pass
        parts = split_raw(name, data)
        tg.text("Файл «%s» (%d МБ) больше, чем бот может отправить одним куском. Ниже он нарезан на %d частей. "
                "Чтобы собрать: скачайте все части в одну папку и откройте первую (.001) программой 7-Zip."
                % (name, mb(len(data)), len(parts)))
        for pname, pdata in parts:
            tg.document(pname, pdata)
    except Exception as e:
        print("вложение не ушло:", str(e)[:200])
        try:
            tg.text("Вложение «%s» переслать не удалось. Напишите тому, кто ведёт бота." % name)
        except Exception:
            pass


# ---------- пересылка одного письма ----------

def forward(raw, tg, only_from=(), skip_from=(), where=""):
    """Возвращает "sent" или "skipped". Если не ушёл текст, бросает TextFailed."""
    head, body, files, addr = parse(raw)
    if skipped(addr, skip_from):
        return "skipped"
    if only_from and not any(x in addr.lower() for x in only_from):
        return "skipped"
    full = head + ("\n" + where if where else "") + "\n\n" + (body or "(текста нет)")
    if CLOUD_RE.search(body):
        full += "\n\nВ письме есть ссылки на файлы в облаке Mail.ru. Такие ссылки временные: скачайте файлы сейчас."
    try:
        for piece in chunks(full):
            tg.text(piece)
    except Exception as e:
        raise TextFailed(str(e))
    for name, data in files:
        send_file(tg, name, data)
    return "sent"


# ---------- почта ----------

def utf7_decode(s):
    """Имена папок приходят в особой кодировке (modified UTF-7)."""
    def rep(m):
        b = m.group(1)
        if not b:
            return "&"
        b = b.replace(",", "/")
        try:
            return base64.b64decode(b + "=" * (-len(b) % 4)).decode("utf-16-be", "replace")
        except Exception:
            return m.group(0)
    return re.sub(r"&([^-]*)-", rep, s)


def q(name):
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def folders(box):
    typ, data = box.list()
    out = []
    for line in data or []:
        if not isinstance(line, bytes):
            continue
        m = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', line.decode("utf-8", "replace"))
        if not m:
            continue
        raw = m.group("name").strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        out.append((m.group("flags").lower(), raw, utf7_decode(raw)))
    return out


def kind(flags, human):
    h = human.lower()
    if "\\noselect" in flags or h in (DONE.lower(), SKIPPED.lower(), FAILED.lower()):
        return "skip"
    if any(x in flags for x in ("\\sent", "\\drafts", "\\trash", "\\all", "\\archive")):
        return "skip"
    if any(x in h for x in ("отправлен", "чернов", "корзин", "удал", "архив", "sent", "draft", "trash", "archive")):
        return "skip"
    if "\\junk" in flags or "спам" in h or "spam" in h or "junk" in h:
        return "spam"
    return "normal"


def uids(box, criterion):
    typ, d = box.uid("SEARCH", None, criterion)
    return d[0].split() if typ == "OK" and d and d[0] else []


def settle(box, uid, dest):
    """Отметка «отвечено» выводит письмо из очереди, даже если перенос не удастся."""
    box.uid("STORE", uid, "+FLAGS", "(\\Answered \\Seen)")
    typ, _ = box.uid("COPY", uid, dest)
    if typ == "OK":
        box.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
    else:
        print("письмо помечено, но в папку", dest, "не перенеслось")


def run(env, tg, only, skip):
    """Возвращает число писем, которые не удалось переслать."""
    try:
        box = imaplib.IMAP4_SSL(env["IMAP_HOST"])
        box.login(env["IMAP_USER"], env["IMAP_PASSWORD"])
        fl = folders(box)
    except Exception as e:
        raise MailDown(str(e)[:200])
    have = [h.lower() for _, _, h in fl]
    first = DONE.lower() not in have
    for name in (DONE, SKIPPED, FAILED):
        if name.lower() not in have:
            box.create(name)
    plan = sorted(((raw, kind(flags, human)) for flags, raw, human in fl), key=lambda x: x[0].upper() != "INBOX")
    left, failed, total = MAX_PER_RUN, 0, 0
    for raw_name, k in plan:
        if k == "skip" or left <= 0:
            continue
        typ, _ = box.select(q(raw_name))
        if typ != "OK":
            continue
        if first and raw_name.upper() == "INBOX":
            old = uids(box, "SEEN")                       # прочитанное до этой схемы уже пересылалось
            for u in old:
                settle(box, u, DONE)
            print("первый запуск по новой схеме, старых писем убрано в папку:", len(old))
        for u in uids(box, "UNANSWERED")[:left]:
            left -= 1
            total += 1
            typ, data = box.uid("FETCH", u, "(BODY.PEEK[])")
            raw = next((x[1] for x in data or [] if isinstance(x, tuple)), None)
            if raw is None:
                continue
            where = "Письмо лежало в папке «Спам»: может быть нежелательным." if k == "spam" else ""
            try:
                result = forward(raw, tg, only, skip, where)
                settle(box, u, DONE if result == "sent" else SKIPPED)
                print("письмо переслано" if result == "sent" else "письмо пропущено: служебное или не от нужного отправителя")
            except TextFailed as e:
                failed += 1
                print("текст письма не ушёл:", str(e)[:200])
                head = parse(raw)[0]
                # Если не уйдёт и это короткое сообщение, Телеграм недоступен: исключение
                # остановит запуск, письмо останется в очереди до следующего раза.
                tg.text(head + "\n\nЭто письмо бот переслать не смог. Напишите тому, кто ведёт бота: письмо цело и лежит в почте.")
                settle(box, u, FAILED)
        box.expunge()
    print("писем в очереди было:", total)
    box.logout()
    return failed


# ---------- оповещения о сбое почты ----------

def previous_runs():
    """Итоги двух прошлых запусков этого расписания; None, если узнать не удалось."""
    repo, cur = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    if not repo:
        return None
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    url = "https://api.github.com/repos/%s/actions/workflows/pochta.yml/runs?per_page=6&status=completed" % repo
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        return [x["conclusion"] for x in d.get("workflow_runs", []) if str(x["id"]) != str(cur)][:2]
    except Exception:
        return None


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--test":
        with open(sys.argv[2], "rb") as f:
            forward(f.read(), Telegram("x", "0", dry=True), (), (".mail.ru",))
        return 0
    env = os.environ
    missing = [k for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "TG_TOKEN", "TG_CHAT_ID") if not env.get(k)]
    if missing:
        # Пока секреты не вписаны, расписание не должно падать и слать письма об ошибке.
        print("Не настроено, пропускаю. Не заданы секреты:", ", ".join(missing))
        return 0
    # В ONLY_FROM считаются только куски адресов: с «@» или с точкой. Прочее не действует.
    only = tuple(x.strip().lower() for x in env.get("ONLY_FROM", "").split(",") if "@" in x or "." in x)
    skip = tuple(x.strip().lower() for x in (env.get("SKIP_FROM") or ".mail.ru").split(",") if x.strip())
    tg = Telegram(env["TG_TOKEN"], env["TG_CHAT_ID"], env.get("TG_THREAD_ID") or None)
    try:
        failed = run(env, tg, only, skip)
    except MailDown as e:
        print("почта недоступна:", e)
        runs = previous_runs()
        # Сообщаем один раз: когда это второй сбой подряд, а до него всё работало.
        if runs and runs[0] == "failure" and (len(runs) < 2 or runs[1] == "success"):
            try:
                tg.text("Бот не может проверить почту группы: нет связи с ящиком или ящик не пускает. "
                        "Письма не теряются: когда связь вернётся, все придут сюда. "
                        "Тому, кто ведёт бота, GitHub уже прислал уведомление.")
            except Exception:
                pass
        return 1
    runs = previous_runs()
    if runs and len(runs) == 2 and runs[0] == "failure" and runs[1] == "failure":
        try:
            tg.text("Проверка почты группы снова работает.")
        except Exception:
            pass
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
