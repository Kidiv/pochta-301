# -*- coding: utf-8 -*-
# Пересылает новые письма из почтового ящика в тему группы Телеграма.
# Нужен только стандартный питон. Настройки берутся из переменных среды:
#   IMAP_HOST      сервер почты, например imap.yandex.ru или imap.mail.ru
#   IMAP_USER      адрес ящика
#   IMAP_PASSWORD  пароль приложения (не обычный пароль от почты)
#   TG_TOKEN       токен бота от @BotFather
#   TG_CHAT_ID     номер группы, например -1004493927052
#   TG_THREAD_ID   номер темы в группе (можно не задавать, тогда в общий чат)
#   ONLY_FROM      необязательно: через запятую куски адресов, от кого пересылать
# Проверка без почты и без Телеграма:
#   python pochta_v_telegram.py --test письмо.eml
import email
import email.policy
import html
import imaplib
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

TG_TEXT_LIMIT = 3900          # у Телеграма предел 4096 знаков на сообщение
TG_FILE_LIMIT = 45 * 1024 * 1024  # бот может отправить файл до 50 МБ
MAX_PER_RUN = 10              # не больше писем за один запуск


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
    s = re.sub(r"(?i)<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
               lambda m: "%s (%s)" % (re.sub(r"<[^>]+>", "", m.group(2)).strip(), m.group(1)), s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def tidy(s):
    s = s.replace("\r", "")
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def parse(raw):
    """Возвращает (шапка, текст, [(имя файла, байты)])."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = parseaddr(dec(msg.get("From")))
    subject = dec(msg.get("Subject")) or "(без темы)"
    try:
        when = parsedate_to_datetime(msg.get("Date")).strftime("%d.%m.%Y %H:%M")
    except Exception:
        when = ""
    plain, rich, files = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fname = part.get_filename()
        ctype = part.get_content_type()
        if fname or part.get_content_disposition() == "attachment":
            data = part.get_payload(decode=True) or b""
            files.append((dec(fname) or "вложение", data))
        elif ctype == "text/plain":
            plain.append(part.get_content())
        elif ctype == "text/html":
            rich.append(part.get_content())
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


class Telegram:
    def __init__(self, token, chat_id, thread_id=None, dry=False):
        self.url = "https://api.telegram.org/bot%s/" % token
        self.chat_id, self.thread_id, self.dry = chat_id, thread_id, dry

    def _base(self):
        d = {"chat_id": self.chat_id}
        if self.thread_id:
            d["message_thread_id"] = self.thread_id
        return d

    def _call(self, method, data, content_type):
        req = urllib.request.Request(self.url + method, data=data, headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError("Телеграм ответил %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))

    def text(self, text):
        if self.dry:
            print("--- сообщение (%d знаков) ---\n%s" % (len(text), text))
            return
        d = self._base()
        d.update({"text": text, "disable_web_page_preview": True})
        self._call("sendMessage", json.dumps(d).encode("utf-8"), "application/json")

    def document(self, name, data):
        if self.dry:
            print("--- файл: %s, %d байт ---" % (name, len(data)))
            return
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in self._base().items():
            body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode("utf-8")
        safe = name.replace('"', "'").replace("\r", " ").replace("\n", " ")
        body += ("--%s\r\nContent-Disposition: form-data; name=\"document\"; filename=\"%s\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n" % (boundary, safe)).encode("utf-8")
        body += data + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        self._call("sendDocument", body, "multipart/form-data; boundary=" + boundary)


def forward(raw, tg, only_from=()):
    head, body, files, addr = parse(raw)
    if only_from and not any(x in addr.lower() for x in only_from):
        return False
    notes = []
    for name, data in files:
        if len(data) > TG_FILE_LIMIT:
            notes.append("Вложение «%s» (%d МБ) слишком большое для бота, возьмите его из почты."
                         % (name, len(data) // (1024 * 1024)))
    full = head + "\n\n" + (body or "(текста нет)")
    if notes:
        full += "\n\n" + "\n".join(notes)
    # Текст не ушёл: исключение летит наверх, письмо остаётся непрочитанным и повторится.
    for piece in chunks(full):
        tg.text(piece)
    # Текст ушёл: дальше письмо считается пересланным, иначе оно повторялось бы при каждой проверке.
    for name, data in files:
        if 0 < len(data) <= TG_FILE_LIMIT:
            try:
                tg.document(name, data)
            except Exception as e:
                print("вложение не ушло:", name, e)
                try:
                    tg.text("Вложение «%s» переслать не удалось, возьмите его из почты." % name)
                except Exception:
                    pass
    return True


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--test":
        with open(sys.argv[2], "rb") as f:
            forward(f.read(), Telegram("x", "0", dry=True))
        return 0
    env = os.environ
    missing = [k for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "TG_TOKEN", "TG_CHAT_ID") if not env.get(k)]
    if missing:
        # Пока секреты не вписаны, расписание не должно падать и слать письма об ошибке.
        print("Не настроено, пропускаю. Не заданы секреты:", ", ".join(missing))
        return 0
    only = tuple(x.strip().lower() for x in env.get("ONLY_FROM", "").split(",") if x.strip())
    tg = Telegram(env["TG_TOKEN"], env["TG_CHAT_ID"], env.get("TG_THREAD_ID") or None)
    box = imaplib.IMAP4_SSL(env["IMAP_HOST"])
    box.login(env["IMAP_USER"], env["IMAP_PASSWORD"])
    box.select("INBOX")
    _, found = box.search(None, "UNSEEN")
    ids = found[0].split()[:MAX_PER_RUN]
    print("новых писем:", len(ids))
    failed = 0
    for num in ids:
        _, data = box.fetch(num, "(BODY.PEEK[])")   # PEEK: письмо остаётся непрочитанным
        raw = data[0][1]
        try:
            sent = forward(raw, tg, only)
            box.store(num, "+FLAGS", "\\Seen")       # отмечаем только после удачной отправки
            print("письмо", num.decode(), "переслано" if sent else "пропущено по отправителю")
        except Exception as e:
            failed += 1
            print("письмо", num.decode(), "НЕ переслано:", e)
    box.logout()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
