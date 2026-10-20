#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
«Скинулись» — бот + мини-аппа для общих сборов в Telegram-чатах.

Чистый Python 3.9+, ноль зависимостей.
  bot/  <- этот файл: бот + HTTP-сервер + мини-аппа-бэкенд + напоминания
  public/  — фронт мини-аппы
  data/state.json — всё состояние (分钟即可 понять содержимое)

Запуск:
    BOT_TOKEN=123:abc python3 bot.py

Без BOT_TOKEN работает только веб-часть (мини-аппа + API) — удобно смотреть
в браузере как демо.
"""

import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
PUBLIC_DIR = os.path.join(BASE_DIR, "public")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "3001"))  # 3000 обычно занят музыкальным сайтом :)
DEV_MODE = os.environ.get("DEV_MODE", "") == "1" or not BOT_TOKEN

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("skinulis")

STATE = {
    "users": {},        # "777" -> {"name","username"}
    "collections": {},  # "c_ab12" -> {...}
    "payments": {},     # "c_ab12" -> {"777": {"amount","paid_ts"}}
    "nags": {},         # "c_ab12:777:t24" -> ts
}
LOCK = threading.RLock()
bot_username = os.environ.get("BOT_USERNAME", "")  # заполнится из getMe


# ────────────────────────────────────────────────── состояние

def load_state():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                disk = json.load(f)
            for k in STATE:
                STATE[k].update(disk.get(k, {}))
        except Exception as e:
            log.warning("state.json повреждён (%s) — начинаю с чистого", e)
    if DEV_MODE and not STATE["collections"]:
        seed_demo()


def save_state():
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def uid(u):
    return str(u["id"])


def nice_name(u):
    n = (((u or {}).get("first_name") or "") + " " + ((u or {}).get("last_name") or "")).strip()
    return n or (u or {}).get("username") or ("Без имени " + str((u or {}).get("id", "")))


def upsert_user(u):
    if not u or not u.get("id"):
        return
    STATE["users"][uid(u)] = {"name": nice_name(u), "username": u.get("username") or ""}


def save():
    with LOCK:
        save_state()


# ────────────────────────────────────────────────── Telegram API

def tg(method, **params):
    """Bot API POST. Возвращает result | None (всё молча в лог)."""
    if not BOT_TOKEN:
        return None
    params = {k: v for k, v in params.items() if v is not None}  # None в replyMarkup = Bad Request
    url = "%s/bot%s/%s" % ("https://api.telegram.org", BOT_TOKEN, method)
    req = urllib.request.Request(
        url,
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        log.warning("tg %s -> %s", method, e.read().decode()[:200])
        return None
    except Exception as e:
        log.warning("tg %s сеть: %s", method, e)
        return None
    if not body.get("ok"):
        log.warning("tg %s -> %s", method, body.get("description"))
        return None
    return body.get("result")


def deep_link(start):
    u = bot_username or "skinulis_bot"
    return "https://t.me/%s?startapp=%s" % (u, start)


# ────────────────────────────────────────────────── initData (HMAC)

def validate_init_data(init_data):
    if not init_data:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    check_hash = parsed.pop("hash", None)
    if not check_hash:
        return None
    dcs = "\n".join("%s=%s" % (k, parsed[k]) for k in sorted(parsed))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    good = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, check_hash):
        return None
    auth_date = int(parsed.get("auth_date", "0") or 0)
    if auth_date and time.time() - auth_date > 86400 * 3:
        return None
    try:
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None


# ────────────────────────────────────────────────── разбор /skor

RE_VAL = re.compile(r"^(\d+)([₽$€])?$")
RE_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.?(\d{2,4})?$")
RE_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_skor(args):
    """'Подарок Лене по 300 до 28.12 20:00 анонимно'
    -> ('Подарок Лене', {per, goal, until(iso)|None, anon, cur})"""
    toks = args.split()
    title = []
    o = {"per": None, "goal": None, "until": None, "anon": False, "cur": "₽"}
    i = 0
    while i < len(toks):
        t = toks[i]
        low = t.lower()
        if low in ("по", "на", "до") and i + 1 < len(toks):
            mval = RE_VAL.match(toks[i + 1])
            mdate = RE_DATE.match(toks[i + 1])
            if mval and low != "до":
                if low == "по":
                    o["per"] = int(mval.group(1))
                else:
                    o["goal"] = int(mval.group(1))
                if mval.group(2):
                    o["cur"] = mval.group(2)
                i += 2
                continue
            if mdate:
                day, mon = int(mdate.group(1)), int(mdate.group(2))
                year = mdate.group(3)
                hh, mm = 23, 59
                j = i + 2
                if j < len(toks):
                    mt = RE_TIME.match(toks[j])
                    if mt:
                        hh, mm = int(mt.group(1)), int(mt.group(2))
                        j += 1
                now = datetime.now()
                if year:
                    y = 2000 + int(year) if len(year) == 2 else int(year)
                else:
                    y = now.year
                try:
                    dt = datetime(y, mon, day, hh, mm)
                    if not year and dt <= now:
                        dt = dt.replace(year=y + 1)
                    o["until"] = dt.strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    pass
                i = j
                continue
        if low.startswith("анон"):
            o["anon"] = True
            i += 1
            continue
        title.append(t)
        i += 1
    return (" ".join(title) or "Сбор", o)


# ────────────────────────────────────────────────── модель

def new_id():
    return "c_" + secrets.token_hex(4)


def get_collection(cid):
    return STATE["collections"].get(cid)


def user_of(code):
    st = STATE["users"].get(str(code), {})
    return {"id": str(code), "name": st.get("name") or "???", "username": st.get("username") or ""}


def payments_of(cid):
    return STATE["payments"].setdefault(cid, {})


def members_of(cid):
    """Список кодов участников: joined + платившие."""
    c = STATE["collections"][cid]
    codes = set(c.get("joined") or [])
    codes |= set(payments_of(cid).keys())
    return sorted(codes)


def total_collected(cid):
    return sum(p["amount"] for p in payments_of(cid).values())


def unpaid_codes(cid):
    paid = payments_of(cid)
    return [m for m in members_of(cid) if m not in paid]


def create_collection(title, chat_id, created_by, per=None, goal=None, until=None, anon=False, cur="₽"):
    c = {
        "id": new_id(),
        "title": title or "Сбор",
        "chat_id": chat_id,
        "message_id": None,
        "mode": "per_person" if per else ("total" if goal else "free"),
        "per_person": per,
        "goal_total": goal,
        "currency": cur,
        "deadline": until,
        "anonymous": bool(anon),
        "pay_hint": "",
        "created_by": str(created_by),
        "status": "active",
        "created_ts": time.time(),
        "closed_ts": None,
        "joined": [str(created_by)],
    }
    STATE["collections"][c["id"]] = c
    payments_of(c["id"])
    save_state()
    return c


def money(n, cur="₽"):
    if n is None:
        return ""
    return "%d %s" % (n, cur)


def deadline_left(iso):
    """('до 28.10 20:00 (2 д 3 ч)', left|None)"""
    if not iso:
        return "", None
    d = datetime.strptime(iso, "%Y-%m-%d %H:%M")
    left = d - datetime.now()
    pretty = "до " + d.strftime("%d.%m %H:%M")
    secs = left.total_seconds()
    if secs <= 0:
        return pretty + " (прошёл)", True
    if secs < 3600:
        return pretty + " (%d мин)" % (secs // 60), False
    if secs < 86400:
        return pretty + " (%d ч)" % (secs // 3600), False
    return pretty + " (%d д)" % (secs // 86400), False


# ────────────────────────────────────────────────── карточка в чате

def card_html(cid):
    c = STATE["collections"][cid]
    paid = payments_of(cid)
    closed_head = "🏁 <b>Сбор закрыт</b>\n" if c["status"] == "closed" else ""

    if c["mode"] == "per_person":
        target = "по %s" % money(c["per_person"], c["currency"])
    elif c["mode"] == "total":
        target = "💰 %s из %s" % (money(total_collected(cid), c["currency"]), money(c["goal_total"], c["currency"]))
    else:
        target = "💰 уже %s" % money(total_collected(cid), c["currency"])

    dl, _ = deadline_left(c["deadline"])

    names = []
    for code in members_of(cid):
        mark = "✅" if code in payments_of(cid) else "⭕"
        names.append(mark + " " + html.escape(user_of(code)["name"]))
    if c["anonymous"]:
        who = "👥 %d уже, %d ждём 💀" % (len(paid), len(unpaid_codes(cid)))
    elif names:
        who = ", ".join(names[:10])
        if len(names) > 10:
            who += " +ещё %d" % (len(names) - 10)
    else:
        who = "Пока пусто — стань первым 💸"

    org = user_of(c["created_by"])
    parts = [
        closed_head + "💸 <b>%s</b>" % html.escape(c["title"]),
        target + (" · " + dl if dl else ""),
        who,
        fmt_hint_line(c),
    ]
    if c["status"] == "active":
        parts.append("✏️ Создать новый: /skor Название по 500 до 30.12")
    parts.append("Организатор: " + html.escape(org["name"]))
    return "\n".join([p for p in parts if p])


def fmt_hint_line(c):
    if c.get("pay_hint"):
        return "💳 <i>Куда скидывать:</i> " + html.escape(c["pay_hint"])
    return ""


def card_keyboard(cid):
    c = STATE["collections"][cid]
    label = "💸 Скинуться" if c["status"] == "active" else "🗂 Посмотреть итоги"
    return {"inline_keyboard": [[{"text": label, "url": deep_link(cid)}]]}


def update_card(cid):
    """Обновить карточку в чате (или создать, если нет)."""
    c = STATE["collections"].get(cid)
    if not c or not c["chat_id"] or not BOT_TOKEN:
        return
    if c["message_id"]:
        try:
            ok = tg("editMessageText", chat_id=c["chat_id"], message_id=c["message_id"],
                    text=card_html(cid), parse_mode="HTML",
                    reply_markup=card_keyboard(cid), disable_web_page_preview=True)
            if ok:
                return
        except Exception:
            pass
    sent = tg("sendMessage", chat_id=c["chat_id"], text=card_html(cid),
              parse_mode="HTML", reply_markup=card_keyboard(cid),
              disable_web_page_preview=True)
    if sent:
        c["message_id"] = sent["message_id"]
        save_state()


# ────────────────────────────────────────────────── веб-вью (для мини-аппы)

def build_view(cid, me_code):
    c = STATE["collections"].get(cid)
    if not c:
        return None
    paid = payments_of(cid)
    me_paid = me_code in paid
    members = members_of(cid)

    roster = []
    for code in members:
        u = user_of(code)
        p = paid.get(code)
        row = {"id": code, "paid": bool(p), "me": code == me_code}
        if row["me"]:
            row["name"] = "Ты"
            if p:
                row["amount"] = p["amount"]
        elif c["anonymous"]:
            row["name"] = None
        else:
            row["name"] = u["name"]
            row["username"] = u["username"]
            if p:
                row["amount"] = p["amount"]
        roster.append(row)

    me_u = user_of(me_code)
    dl, left_days = deadline_left(c["deadline"])
    view = {
        "id": c["id"],
        "title": c["title"],
        "mode": c["mode"],
        "per_person": c["per_person"],
        "goal_total": c["goal_total"],
        "currency": c["currency"],
        "deadline": c["deadline"],
        "deadline_left": dl,
        "anonymous": c["anonymous"],
        "pay_hint": c["pay_hint"],
        "status": c["status"],
        "is_organizer": me_code == c["created_by"],
        "organizer": {"name": user_of(c["created_by"])["name"], "username": user_of(c["created_by"])["username"]},
        "paid_count": len(paid),
        "unpaid_count": len(unpaid_codes(cid)),
        "total": total_collected(cid),
        "payments": [{"amount": p["amount"]} for p in paid.values()],
        "my_paid": me_paid,
        "my_amount": paid.get(me_code, {}).get("amount"),
        "roster": roster,
        "link": deep_link(cid),
    }
    return view


def my_items(me_code):
    items = []
    for c in STATE["collections"].values():
        if c["status"] != "active":
            continue
        paid = payments_of(c["id"])
        if me_code not in paid and me_code not in members_of(c["id"]):
            continue
        dl, _ = deadline_left(c["deadline"])
        items.append({
            "id": c["id"], "title": c["title"], "deadline_left": dl,
            "my_paid": me_code in paid, "currency": c["currency"],
            "paid_count": len(paid), "unpaid_count": len(unpaid_codes(c["id"])),
        })
    items.sort(key=lambda x: x["deadline_left"] == "")
    return items


# ────────────────────────────────────────────────── дожиматель

def nag(cid, code, stage, text):
    key = "%s:%s:%s" % (cid, code, stage)
    with LOCK:
        if key in STATE["nags"]:
            return
        STATE["nags"][key] = time.time()
    save_state()
    if BOT_TOKEN:
        tg("sendMessage", chat_id=int(code), text=text,
           reply_markup={"inline_keyboard": [[{"text": "💸 Открыть сбор", "url": deep_link(cid)}]]},
           disable_web_page_preview=True)
    else:
        log.info("[демо] не отправляю юзеру %s: %s", code, text[:60])


def scheduler_loop():
    while True:
        try:
            with LOCK:
                cids = [c["id"] for c in STATE["collections"].values() if c["status"] == "active"]
            for cid in cids:
                tick_collection(cid)
            time.sleep(60)
        except Exception as e:
            log.warning("scheduler: %s", e)
            time.sleep(60)


def tick_collection(cid):
    with LOCK:
        c = get_collection(cid)
        if not c or c["status"] != "active":
            return
        debtors = [u for u in unpaid_codes(cid) if u != c["created_by"]]
        left = None
        if c["deadline"]:
            dl = datetime.strptime(c["deadline"], "%Y-%m-%d %H:%M")
            left = dl - datetime.now()
        age_h = (time.time() - c["created_ts"]) / 3600

    if left is not None:
        t = left.total_seconds()
        if 0 < t <= 86400:
            for code in debtors:
                nag(cid, code, "t24",
                    "⏰ Меньше суток до конца «%s». Ты ещё не в списке ✅ — открой сбор и исправься 🙃"
                    % c["title"])
            return
        if t <= 0 and t > -86400:
            for code in debtors:
                nag(cid, code, "t0",
                    "🏁 Дедлайн по «%s» прошёл. Успеешь до финального подсчёта — жми «Я скинулся», "
                    "а то попадёшь в список позора 💀" % c["title"])
            return
        if t <= -86400:
            close_collection(cid, c["created_by"], auto=True)
            return
    elif age_h > 24 * 14:
        nag(cid, c["created_by"], "stale", "👋 Сбор «%s» висит 2 недельки. Если он уже не нужен — закрой: /close" % c["title"])


def close_collection(cid, by, auto=False):
    with LOCK:
        c = get_collection(cid)
        if not c or c["status"] != "active":
            return
        if not auto and c["created_by"] != str(by):
            return
        c["status"] = "closed"
        c["closed_ts"] = time.time()
        paid = payments_of(cid)
        missed = unpaid_codes(cid)
        names_missed = [user_of(m)["name"] for m in missed] if not c["anonymous"] else []
        total = total_collected(cid)
        chat = c["chat_id"]
        save_state()

    if chat and BOT_TOKEN:
        text = "🏁 <b>«%s» — итог</b>\nСкинулись: %d (сумма %s)" % (
            html.escape(c["title"]), len(paid), money(total, c["currency"]))
        if names_missed:
            text += "\n💀 Слились: " + html.escape(", ".join(names_missed))
        elif c["anonymous"]:
            text += "\nТихоня прячется 🙂"
        tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True)
    update_card(cid)
    return True


# ────────────────────────────────────────────────── команды бота

def cmd_start(msg):
    tg("sendMessage",
       chat_id=msg["chat"]["id"],
       text="Привет! Это <b>«Скинулись»</b> 💸\n\n"
            "Создавай сбор прямо в групповом чате:\n"
            "<code>/skor Подарок Лене по 300 до 28.12</code>\n\n"
            "Остальное — в мини-аппе:",
       parse_mode="HTML",
       reply_markup={"inline_keyboard": [[{"text": "🗂 Мои сборы", "url": deep_link("home")}]]},
       disable_web_page_preview=True)


def cmd_skor(msg, args):
    chat = msg["chat"]
    title, o = parse_skor(args)
    with LOCK:
        c = create_collection(
            title=title, chat_id=chat["id"], created_by=uid(msg["from"]),
            per=o["per"], goal=o["goal"], until=o["until"], anon=o["anon"], cur=o["cur"],
        )
        cid = c["id"]
    update_card(cid)


def cmd_list(msg):
    chat = msg["chat"]
    with LOCK:
        items = [c for c in STATE["collections"].values()
                 if c.get("chat_id") == chat["id"] and c["status"] == "active"]
    if not items:
        tg("sendMessage", chat_id=chat["id"], text="Активных сборов нет. Создай: /skor Пицца по 400", disable_web_page_preview=True)
        return
    lines = []
    for c in items:
        dl, _ = deadline_left(c["deadline"])
        lines.append("💸 <b>%s</b> — скинулись %d · %s · %s" % (
            html.escape(c["title"]), len(payments_of(c["id"])),
            dl.strip(), deep_link(c["id"])))
    tg("sendMessage", chat_id=chat["id"], text="Активные сборы:\n" + "\n".join(lines),
       parse_mode="HTML", disable_web_page_preview=True)


def cmd_close(msg, args):
    chat = msg["chat"]
    who = uid(msg["from"])
    cid = args.strip()
    with LOCK:
        if cid in STATE["collections"]:
            target = cid
        else:
            mine = [c for c in STATE["collections"].values()
                    if c.get("chat_id") == chat["id"] and c["status"] == "active"
                    and c["created_by"] == who]
            target = max(mine, key=lambda x: x["created_ts"])["id"] if mine else None
    if not target:
        tg("sendMessage", chat_id=chat["id"], text="Твоих активных сборов в этом чате нет.", disable_web_page_preview=True)
        return
    close_collection(target, who)


def cmd_help(msg):
    tg("sendMessage", chat_id=msg["chat"]["id"],
       text="💸 <b>Как это работает</b>\n\n"
            "/skor Название <b>по 300</b> — фикс с каждого\n"
            "/skor Название <b>на 5000</b> — цель на всех\n"
            "/skor Название <b>до 28.12 20:00</b> — дедлайн\n"
            "/skor ... <b>анонимно</b> — суммы скрыть\n"
            "Можно всё сразу: /skor Подарок Лене по 300 до 28.12 анонимно\n\n"
            "/list — активные сборы в чате\n"
            "/close — закрыть свой последний сбор\n"
            "/start — показать свои сборы",
       parse_mode="HTML", disable_web_page_preview=True)


def handle_update(upd):
    msg = upd.get("message")
    if not msg or "from" not in msg:
        return
    upsert_user(msg.get("from"))
    chat_type = msg["chat"]["type"]
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split(" ", 1)
    cmd = parts[0].lower().split("@")[0]
    rest = parts[1] if len(parts) > 1 else ""
    if cmd == "/start":
        cmd_start(msg)
    elif cmd == "/help":
        cmd_help(msg)
    elif cmd == "/skor":
        cmd_skor(msg, args=parts[1] if len(parts) > 1 else "")
    elif cmd == "/list":
        cmd_list(msg)
    elif cmd == "/close":
        cmd_close(msg, args=rest)
    else:
        pass  # неизвестная команда — молчим
    save()


# ────────────────────────────────────────────────── HTTP сервер


class Handler(BaseHTTPRequestHandler):
    server_version = "SkinulisBot/0.1"

    # ---------- helpers

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorize(self):
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("tma "):
            u = validate_init_data(hdr[4:])
            if u:
                with LOCK:
                    upsert_user(u)
                    save_state()
                return u
        if DEV_MODE:
            with LOCK:
                return {"id": 900000, "first_name": "Ты (демо)", "username": "tester"}
        return None

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n <= 0 or n > 65536:
                return {}
            return json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return {}

    # ---------- static

    MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
            ".png": "image/png", ".ico": "image/x-icon"}

    def serve_static(self, path):
        rel = path.split("?")[0]
        if rel.endswith("/") and rel != "/":
            rel = rel[:-1]
        if rel == "" or rel in ("/app", "/app/"):
            rel = "index.html"
        elif rel.startswith("/app/"):
            rel = rel[len("/app/"):]
        else:
            self.send_response(302)
            self.send_header("Location", "/app")
            self.end_headers()
            return
        fname = os.path.normpath(rel)
        full = os.path.join(PUBLIC_DIR, fname)
        if not full.startswith(PUBLIC_DIR) or not os.path.isfile(full):
            self.send_json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(full)[1].lower()
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            self.send_json({"error": "not_found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", self.MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- API

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self.read_json()
        me = self.authorize()
        if not me:
            return self.send_json({"error": "нужен Telegram initData"}, 401)
        code = uid(me)

        if path == "/api/init":
            start = (body.get("start_param") or "").strip()
            view = None
            if start.startswith("c_"):
                with LOCK:
                    c = get_collection(start)
                    if c:
                        if code not in c["joined"]:
                            c["joined"].append(code)
                            save_state()
                        view = build_view(start, code)
                if c:
                    update_card(start)  # обновить карточку с новым участником
            me_view = {"id": code, "name": nice_name(me), "username": me.get("username")}
            return self.send_json({"user": me_view, "collection": view,
                                   "bot_username": bot_username or "skinulis_bot"})

        m = re.match(r"^/api/c/([a-z0-9_]+)/(.+)$", path)
        if not m:
            return self.send_json({"error": "not_found"}, 404)
        cid, action = m.group(1), m.group(2)
        with LOCK:
            c = get_collection(cid)
        if not c:
            return self.send_json({"error": "нет такого сбора"}, 404)

        if action == "paid":
            amount = body.get("amount")
            with LOCK:
                if c["status"] != "active":
                    return self.send_json({"error": "Сбор закрыт"}, 409)
                if amount is None and c["mode"] == "per_person" and c["per_person"]:
                    amount = c["per_person"]
                try:
                    amount = int(amount)
                except (TypeError, ValueError):
                    return self.send_json({"error": "Сколько?"}, 400)
                if amount <= 0 or amount > 10_000_000:
                    return self.send_json({"error": "Странная сумма"}, 400)
                if code not in c["joined"]:
                    c["joined"].append(code)
                payments_of(cid)[code] = {"amount": amount, "paid_ts": time.time()}
                save_state()
            update_card(cid)
            return self.send_json({"view": build_view(cid, code)})

        if action == "hint":
            if code != c["created_by"]:
                return self.send_json({"error": "только организатор"}, 403)
            with LOCK:
                c["pay_hint"] = (body.get("hint") or "")[:200]
                save_state()
            update_card(cid)
            return self.send_json({"view": build_view(cid, code)})

        if action == "close":
            if code != c["created_by"]:
                return self.send_json({"error": "только организатор"}, 403)
            with LOCK:
                c["status"] = "closed"
                c["closed_ts"] = time.time()
                save_state()
            update_card(cid)
            return self.send_json({"view": build_view(cid, code)})

        if action == "delete":
            if code != c["created_by"]:
                return self.send_json({"error": "удалить может только организатор"}, 403)
            chat_id, message_id = c.get("chat_id"), c.get("message_id")
            with LOCK:
                STATE["collections"].pop(cid, None)
                STATE["payments"].pop(cid, None)
                for k in [k for k in STATE["nags"] if k.startswith(cid + ":")]:
                    STATE["nags"].pop(k, None)
                save_state()
            # карточку в чате тоже подчистим; старые (>48ч) телега не даёт удалять —
            # тогда хотя бы перепишем на «удалён»
            if chat_id and message_id and BOT_TOKEN:
                if not tg("deleteMessage", chat_id=chat_id, message_id=message_id):
                    tg("editMessageText", chat_id=chat_id, message_id=message_id,
                       text="%s — 🗑 сбор удалён организатором" % html.escape(c["title"]),
                       parse_mode="HTML", disable_web_page_preview=True)
            return self.send_json({"deleted": True, "id": cid})

        return self.send_json({"error": "не знаю такое"}, 404)

    def do_GET(self):
        path = self.path.split("?")[0]
        me = self.authorize()
        if path == "/health":  # пульс для мониторинга bothost
            return self.send_json({"ok": True, "port": PORT, "bot": bool(BOT_TOKEN)})
        if path.startswith("/api"):
            if not me:
                return self.send_json({"error": "нужен Telegram initData"}, 401)
            code = uid(me)
            if path == "/api/my":
                with LOCK:
                    items = my_items(code)
                return self.send_json({"items": items, "bot_username": bot_username or "skinulis_bot"})
            m = re.match(r"^/api/c/([a-z0-9_]+)$", path)
            if m:
                with LOCK:
                    v = build_view(m.group(1), code)
                if not v:
                    return self.send_json({"error": "нет такого сбора"}, 404)
                return self.send_json(v)
            return self.send_json({"error": "not_found"}, 404)
        return self.serve_static(path)

    def log_message(self, fmt, *args):
        if "api" in (args[0] if args else ""):
            log.info(fmt % args)


# ────────────────────────────────────────────────── демо-данные


def seed_demo():
    people = [
        {"id": 900000, "first_name": "Ты (демо)", "username": "tester"},
        {"id": 900001, "first_name": "Паша", "username": "pavel"},
        {"id": 900002, "first_name": "Лена", "username": "lena"},
    ]
    for p in people:
        upsert_user(p)
    dl = (datetime.now() + timedelta(days=2)).replace(hour=20, minute=0, second=0, microsecond=0)
    c = create_collection("Пицца на пятницу", -100900, "900000",
                          per=400, until=dl.strftime("%Y-%m-%d %H:%M"))
    c["pay_hint"] = "СБП +7 900 ***-**-45 (Иван)"
    payments_of(c["id"])["900002"] = {"amount": 400, "paid_ts": time.time()}
    for pc in ("900000", "900001"):
        if pc not in c["joined"]:
            c["joined"].append(pc)
    total_note = "демо-сбор создан"
    save_state()
    log.info("демо-данные готовы: «%s»", c["title"])


# ────────────────────────────────────────────────── main

def polling_loop():
    offset = 0
    log.info("Бот запущен: @%s", bot_username)
    while True:
        r = tg("getUpdates", offset=offset, timeout=25, allowed_updates=["message"])
        if r is None:
            time.sleep(3)
            continue
        for upd in r:
            offset = upd.get("update_id", offset) + 1
            try:
                handle_update(upd)
            except Exception as e:
                log.warning("update обработка: %s", e)


def main():
    load_state()
    global bot_username
    if BOT_TOKEN:
        me = tg("getMe")
        if me and me.get("username"):
            bot_username = me["username"]
        threading.Thread(target=polling_loop, daemon=True).start()
    else:
        log.info("BOT_TOKEN не задан — запускается только веб-часть (демо-режим)")
    threading.Thread(target=scheduler_loop, daemon=True).start()

    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    log.info("Мини-аппа: http://localhost:%d/app  (DEV_MODE=%s)", PORT, DEV_MODE)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("пока 💀")


if __name__ == "__main__":
    main()
