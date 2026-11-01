#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
«Скинулись» — бот + приложение бота для общих сборов в Telegram-чатах.

Чистый Python 3.9+, ноль зависимостей.
  bot/  <- этот файл: бот + HTTP-сервер + приложение бота-бэкенд + напоминания
  public/  — фронт приложения бота
  data/state.json — всё состояние (分钟即可 понять содержимое)

Запуск:
    BOT_TOKEN=123:abc python3 bot.py

Без BOT_TOKEN работает только веб-часть (приложение бота + API) — удобно смотреть
в браузере как демо.
"""

import hashlib
import copy
import functools
import gzip
import hmac
import html
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
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
    "hidden": {},       # "777" -> ["c_ab12", ...] — сборы, которые юзер убрал из своих (у других остаются)
}
LOCK = threading.RLock()
_TRANSACTION = threading.local()


def after_state(fn, *args, **kwargs):
    """Send Telegram side effects after the outer state transaction releases LOCK."""
    if getattr(_TRANSACTION, "depth", 0):
        _TRANSACTION.jobs.append((fn, args, kwargs))
        return None
    return fn(*args, **kwargs)


def state_locked(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        outer = not getattr(_TRANSACTION, "depth", 0)
        if outer:
            _TRANSACTION.jobs = []
        with LOCK:
            _TRANSACTION.depth = getattr(_TRANSACTION, "depth", 0) + 1
            try:
                result = fn(*args, **kwargs)
            finally:
                _TRANSACTION.depth -= 1
        if outer:
            jobs, _TRANSACTION.jobs = _TRANSACTION.jobs, []
            for task, task_args, task_kwargs in jobs:
                try:
                    task(*task_args, **task_kwargs)
                except Exception:
                    log.exception("Telegram side effect failed")
        return result
    return wrapped
# "chat_id:message_id" -> последний отправленный текст карточки (чтобы не гонять
# холостые editMessageText, упирающиеся в 400 «message is not modified»)
CARD_CACHE = {}
bot_username = os.environ.get("BOT_USERNAME", "")  # заполнится из getMe


# ────────────────────────────────────────────────── состояние

@state_locked
def load_state():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                disk = json.load(f)
            if not isinstance(disk, dict) or any(not isinstance(v, dict) for v in disk.values()):
                raise ValueError("state sections must be objects")
            # Validate everything before changing memory. Never overwrite damaged financial data.
            for cid, c in disk.get("collections", {}).items():
                if not isinstance(c, dict) or c.get("id") != cid:
                    raise ValueError("invalid collection")
            STATE.update(disk)
        except (OSError, ValueError, TypeError) as e:
            raise RuntimeError("state.json повреждён; восстановите из резервной копии") from e
    if DEV_MODE and not STATE["collections"]:
        seed_demo()


@state_locked
def save_state():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=DATA_DIR,
                                         prefix="state-", suffix=".tmp", delete=False) as f:
            tmp = f.name
            json.dump(STATE, f, ensure_ascii=False, indent=1, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def uid(u):
    return str(u["id"])


def nice_name(u):
    n = (((u or {}).get("first_name") or "") + " " + ((u or {}).get("last_name") or "")).strip()
    return n or (u or {}).get("username") or ("Без имени " + str((u or {}).get("id", "")))


@state_locked
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
    if method in ("sendMessage", "answerCallbackQuery", "editMessageText", "deleteMessage") and getattr(_TRANSACTION, "depth", 0):
        return after_state(tg, method, **params)
    if not BOT_TOKEN:
        return None
    if os.environ.get("BOT_OFFLINE", "") == "1":  # тестовый режим без сети Telegram
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


def chat_link(start):
    """Ссылка на бот в чате (работает даже там, где аппа не открывается)."""
    u = bot_username or "skinulis_bot"
    return "https://t.me/%s?start=%s" % (u, start)


def share_url(cid):
    msg = "Вас пригласили в сбор «%s» 💸 Жми — откроется сразу в приложении, и скажешь, ты в деле или нет." % STATE["collections"][cid]["title"]
    return ("https://t.me/share/url?url=" + urllib.parse.quote(deep_link(cid), safe="") +
            "&text=" + urllib.parse.quote(msg, safe=""))


# ────────────────────────────────────────────────── initData (HMAC)

def _dbg_initdata(lines):
    """Диагностика initData в файл /app/data/debug_initdata.log — читается cat'ом из панели.
    Хвост файла держим коротким (последние ~40 строк), чтобы он не рос бесконечно."""
    try:
        p = os.path.join(DATA_DIR, "debug_initdata.log")
        old = ""
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                old = f.read()
        keep = old.splitlines()[-38:]
        stamp = time.strftime("%d.%m %H:%M:%S")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(keep) + "\n" + "\n".join("[%s] %s" % (stamp, x) for x in lines) + "\n")
    except Exception:
        pass


def validate_init_data(init_data, reason=None):
    """Validate decoded Telegram HMAC fields and a bounded authentication date."""
    try:
        if not BOT_TOKEN or not isinstance(init_data, str) or len(init_data) > 16384:
            raise ValueError("invalid initData")
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        d = dict(pairs)
        if len(d) != len(pairs):
            raise ValueError("duplicate fields")
        received = d.pop("hash", "")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", received):
            raise ValueError("invalid hash")
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        # HMAC includes signature on current clients; keep compatibility with earlier clients.
        variants = [d, {k: v for k, v in d.items() if k != "signature"}]
        if not any(hmac.compare_digest(
                hmac.new(secret, "\n".join("%s=%s" % item for item in sorted(fields.items())).encode(),
                         hashlib.sha256).hexdigest(), received.lower()) for fields in variants):
            raise ValueError("invalid signature")
        stamp = int(d.get("auth_date", "0"))
        if stamp <= 0 or not -60 <= time.time() - stamp <= 86400:
            raise ValueError("expired or future auth_date")
        u = json.loads(d.get("user", ""))
        if not isinstance(u, dict) or type(u.get("id")) is not int or u["id"] <= 0:
            raise ValueError("invalid user")
        if any(k in u and not isinstance(u[k], str) for k in ("first_name", "last_name", "username")):
            raise ValueError("invalid user fields")
        return u
    except (ValueError, TypeError, OverflowError):
        if reason is not None:
            reason["r"] = "invalid or expired Telegram initData"
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
            if mdate and low == "до":
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
                    o["error"] = "Дедлайн сломался, попробуй ещё раз"
                i = j
                continue
            if low == "до":
                o["error"] = "Дедлайн: например до 28.12 20:00"
            else:
                o["error"] = "Сумма — целое положительное число"
        elif low in ("по", "на", "до"):
            o["error"] = "После «%s» нужно значение" % low
        if low.startswith("анон"):
            o["anon"] = True
            i += 1
            continue
        title.append(t)
        i += 1
    return (" ".join(title) or "Сбор", o)


# ────────────────────────────────────────────────── модель

def new_id():
    return "c_" + secrets.token_hex(16)


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
    """Ждём от них денег: не платили и не отказались."""
    paid = payments_of(cid)
    declined = set(STATE["collections"][cid].get("declined") or [])
    return [m for m in members_of(cid) if m not in paid and m not in declined]


@state_locked
def create_collection(title, chat_id, created_by, per=None, goal=None, until=None, anon=False, cur="₽"):
    if not isinstance(title, str) or not title.strip() or len(title) > 64:
        raise ValueError("Название должно содержать от 1 до 64 символов")
    if any(isinstance(valid_amount(v), str) for v in (per, goal)) or (per and goal):
        raise ValueError("Укажи одну корректную сумму: с каждого или общую")
    if valid_deadline(until) == "bad_date":
        raise ValueError("Дедлайн сломался, попробуй ещё раз")
    per, goal = valid_amount(per), valid_amount(goal)
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
        "declined": [],
        "cards": {},  # chat_id -> message_id с карточкой (может быть в нескольких чатах)
    }
    STATE["collections"][c["id"]] = c
    payments_of(c["id"])
    save_state()
    return c


def money(n, cur="₽"):
    if n is None:
        return ""
    return "%d %s" % (n, cur)


def parse_deadline(iso):
    """aware-дата из строки дедлайна. Строка «… +0300» — как отдал фронт (локальная зона юзера)."""
    if not iso:
        return None
    try:
        if len(iso) > 16:
            d = datetime.strptime(iso, "%Y-%m-%d %H:%M %z")
        else:
            d = datetime.strptime(iso, "%Y-%m-%d %H:%M").astimezone()
    except (ValueError, TypeError):
        return None
    # юзер выбирает минуты без секунд: считаем дедлайном середину минуты +29 с,
    # иначе «07:47», поставленный на 07:47:50, сразу «прошёл»
    return d + timedelta(seconds=29)


def deadline_left(iso):
    """('до 28.10 20:00 (2 д 3 ч)', left|None)"""
    if not iso:
        return "", None
    d = parse_deadline(iso)
    if not d:
        return "", None
    left = d - datetime.now(d.tzinfo)
    pretty = "до " + d.strftime("%d.%m %H:%M")
    secs = left.total_seconds()
    if secs <= 0:
        return pretty + " (прошёл)", True
    if secs < 60:
        return pretty + " (меньше минуты)", False
    if secs < 3600:
        return pretty + " (%d мин)" % max(1, round(secs / 60)), False
    if secs < 86400:
        return pretty + " (%d ч)" % max(1, round(secs / 3600)), False
    return pretty + " (%d д)" % max(1, round(secs / 86400)), False


# ────────────────────────────────────────────────── карточка в чате

def declined_set(cid):
    return set(STATE["collections"][cid].get("declined") or [])


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
        if code in declined_set(cid):
            mark = "🚫"
        elif code in paid:
            mark = "✅"
        else:
            mark = "⭕"
        name = "Участник %d" % (len(names) + 1) if c["anonymous"] else user_of(code)["name"]
        names.append(mark + " " + html.escape(name))
    if names:
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
        parts.append("✏️ Свой сбор создаётся так же: <code>/skor Пицца по 400 до 25.10</code>")
    parts.append("Организатор: " + html.escape(org["name"]))
    return "\n".join([p for p in parts if p])


def fmt_hint_line(c):
    if c.get("pay_hint"):
        return "💳 <i>Куда скидывать:</i> " + html.escape(c["pay_hint"])
    return ""


def card_keyboard(cid):
    c = STATE["collections"][cid]
    if c["status"] != "active":
        return {"inline_keyboard": [
            [{"text": "🏁 Итоги в боте", "url": deep_link(cid)}],
            [{"text": "➕ Создать новый сбор", "url": deep_link("new")}],
        ]}
    kb = [
        [
            {"text": "✅ Я скинулся", "callback_data": "p:" + cid},
            {"text": "🙅 Не участвую", "callback_data": "d:" + cid},
        ],
        [
            {"text": "🔁 Передумал", "callback_data": "j:" + cid},
            {"text": "🔗 Позвать друзей", "url": share_url(cid)},
        ],
    ]
    return {"inline_keyboard": kb}


def update_card(cid):
    return after_state(_update_card, cid)


def _update_card(cid):
    """Обновить карточку во всех чатах, где она показана (или создать в основном)."""
    with LOCK:
        c = STATE["collections"].get(cid)
        if not c or not BOT_TOKEN:
            return
        cards = c.setdefault("cards", {})
        if not cards and c.get("chat_id"):
            cards[str(c["chat_id"])] = c.get("message_id")
        targets = list(cards.items())
        text, keyboard = card_html(cid), card_keyboard(cid)
    for chat_id, mid in targets:
        key = "%s:%s" % (chat_id, mid)
        if mid:
            # Telegram оозвращает 400 «not modified», если править тем же текстом —
            # кешируем последний отправленный текст и пропускаем холостые правки
            if CARD_CACHE.get(key) == text:
                continue
            try:
                ok = tg("editMessageText", chat_id=int(chat_id), message_id=mid,
                        text=text, parse_mode="HTML",
                        reply_markup=keyboard, disable_web_page_preview=True)
                if ok:
                    CARD_CACHE[key] = text
                    continue
            except Exception:
                pass
            # A failed edit may be a transient network/Telegram error. Do not create duplicates.
            continue
        sent = tg("sendMessage", chat_id=int(chat_id), text=text,
                  parse_mode="HTML", reply_markup=keyboard,
                  disable_web_page_preview=True)
        if sent:
            with LOCK:
                if get_collection(cid) is c:
                    cards[chat_id] = sent["message_id"]
                    CARD_CACHE["%s:%s" % (chat_id, sent["message_id"])] = text
                    save_state()
        elif chat_id != str(c.get("chat_id")):
            cards.pop(chat_id, None)


def post_card(cid, chat_id):
    return after_state(_post_card, cid, chat_id)


def _post_card(cid, chat_id):
    """Выложить карточку сбора в конкретный чат (когда юзер пришёл по /start <код>)."""
    with LOCK:
        c = STATE["collections"].get(cid)
        if not c:
            return False
        cards = c.setdefault("cards", {})
        text, keyboard = card_html(cid), card_keyboard(cid)
    if str(chat_id) in cards and cards[str(chat_id)]:
        try:
            old = int(cards[str(chat_id)])
            key = "%s:%s" % (chat_id, old)
            if CARD_CACHE.get(key) == text:
                return True
            ok = tg("editMessageText", chat_id=int(chat_id), message_id=old,
               text=text, parse_mode="HTML",
               reply_markup=keyboard, disable_web_page_preview=True)
            if ok:
                CARD_CACHE[key] = text
            return bool(ok)
        except Exception:
            pass
    sent = tg("sendMessage", chat_id=int(chat_id), text=text,
              parse_mode="HTML", reply_markup=keyboard,
              disable_web_page_preview=True)
    if sent:
        with LOCK:
            if get_collection(cid) is c:
                cards[str(chat_id)] = sent["message_id"]
                CARD_CACHE["%s:%s" % (chat_id, sent["message_id"])] = text
                save_state()
    return True


# ────────────────────────────────────────── кнопки в чате (callback_query)

def chat_paid(cid, user, amount=None):
    """«Я скинулся» из чата. Возвращает (короткое подтверждение, ошибка|None)."""
    c = STATE["collections"].get(cid)
    if not c:
        return None, "Сбор не найден"
    with LOCK:
        c = STATE["collections"].get(cid)
        if not c:
            return None, "Сбор не найден"
        if c["status"] != "active":
            return None, "Сбор уже закрыт"
        code = str(user["id"])
        if code in (c.get("left") or []):
            return None, "Ты вышел из этого сбора — вернуться можно в приложении «⭕ В деле» 👆"
        paid = payments_of(cid)
        if code in paid:
            if c["mode"] == "per_person":
                # toggle: у «по N с каждого» нажатие второй раз снимает галочку
                paid.pop(code, None)
                save_state()
                return "Галочку снял 🔄", None
            # total/free: уже скинулся — досыпание; честно говорим, сколько есть,
            # и что сумму на довнесение можно просто написать числом в чат
            STATE.setdefault("pendings", {})[code] = {"cid": cid, "chat": user["id"], "ts": time.time()}
            save_state()
            return "paid-again", None
        if amount is None and c["mode"] == "per_person" and c["per_person"]:
            amount = c["per_person"]
        if amount is None:
            STATE.setdefault("pendings", {})[code] = {"cid": cid, "chat": user["id"], "ts": time.time()}
            save_state()
            return "png-amount", None  # спросим сумму текстом
        amount = valid_amount(amount)
        if amount is None or isinstance(amount, str):
            return None, "Странная сумма"
        if code not in c["joined"]:
            c["joined"].append(code)
        payments_of(cid)[code] = {"amount": amount, "paid_ts": time.time()}
        dec = c.setdefault("declined", [])
        if code in dec:
            dec.remove(code)
        save_state()
    finalize(cid)
    return "✅ Красавчик! Отметил: %s %s" % (money(amount, c["currency"]), user_of(code)["name"]), None


def chat_finalize_check(amount_msg, user_code, cid):
    """Проверка: платежи есть и все не-отказники скинулись → close."""
    return finalize(cid)


def chat_decline(cid, user):
    c = STATE["collections"].get(cid)
    if not c:
        return None, "Сбор не найден"
    with LOCK:
        c = STATE["collections"].get(cid)
        if not c:
            return None, "Сбор не найден"
        if c["status"] != "active":
            return None, "Сбор уже закрыт"
        code = str(user["id"])
        if code in (c.get("left") or []):
            return None, "Ты вышел из этого сбора — вернуться можно в приложении «⭕ В деле» 👆"
        if code in payments_of(cid):
            return None, "Ты уже скинулся — сначала «Передумал» и потом отказ 🔄"
        dec = c.setdefault("declined", [])
        joined = c.setdefault("joined", [])
        if code not in joined:
            joined.append(code)
        if code in dec:
            dec.remove(code)  # toggle второй раз → снова в деле
            save_state()
            return "Снова в деле! 💪", None
        dec.append(code)
        save_state()
    finalize(cid)
    return "Отметил тебя как отказника 🙅", None


@state_locked
def handle_callback(upd):
    cq = upd.get("callback_query")
    if not cq:
        return
    upsert_user(cq.get("from"))
    data = cq.get("data") or ""
    chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
    if ":" not in data:
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        return
    act, cid = data.split(":", 1)
    c = STATE["collections"].get(cid)
    code = str(cq.get("from", {}).get("id"))
    # вышедший участник нажимает старые кнопки — не зачисляем обратно (возврат — «⭕ В деле» в аппе)
    if act in ("p", "d", "j") and c and code in (c.get("left") or []):
        tg("answerCallbackQuery", callback_query_id=cq["id"],
           text="Ты вышел из этого сбора — вернуться можно в приложении: «⭕ В деле» по ссылке 👆",
           show_alert=True)
        return
    if act == "hint":
        if cid == "skor":
            tg("answerCallbackQuery", callback_query_id=cq["id"],
               text="Напиши боту: /skor Пицца по 400 до 25.10 — «по 400» с каждого, «на 5000» общая сумма, «до дата» дедлайн",
               show_alert=True)
        elif cid == "how":
            if chat_id:
                send_how_manual(chat_id)
            tg("answerCallbackQuery", callback_query_id=cq["id"])
        else:
            tg("answerCallbackQuery", callback_query_id=cq["id"])
        return
    text = None
    if act in ("p", "d", "j") and chat_id:
        if act == "p":
            text, err = chat_paid(cid, cq["from"])
            pending = STATE.get("pendings", {}).get(code)
            if pending and pending.get("cid") == cid:
                pending["chat"] = chat_id
                save_state()
            if text == "png-amount":
                tg("answerCallbackQuery", callback_query_id=cq["id"],
                   text="Напиши сумму в поле ввода этого чата ⌨️ — например «350» — и я отмечу её в сборе",
                   show_alert=True)
            elif text == "paid-again":
                cc = STATE["collections"].get(cid)
                prev = int((payments_of(cid).get(str(cq["from"]["id"])) or {}).get("amount") or 0)
                tg("answerCallbackQuery", callback_query_id=cq["id"],
                   text="✅ Ты скинулся %s. Довнести ещё? Напиши сумму числом в поле ввода — приплюсую."
                        % money(prev, (cc or {}).get("currency") or "₽"),
                   show_alert=True)
            elif err:
                tg("answerCallbackQuery", callback_query_id=cq["id"], text=err, show_alert=True)
            else:
                tg("answerCallbackQuery", callback_query_id=cq["id"], text=text)
        elif act == "d":
            text, err = chat_decline(cid, cq["from"])
            if err:
                tg("answerCallbackQuery", callback_query_id=cq["id"], text=err, show_alert=True)
            else:
                tg("answerCallbackQuery", callback_query_id=cq["id"], text=text)
        elif act == "j":
            # «Передумал»: снять и галочку, и отказ — вернуться в ожидание
            c = STATE["collections"].get(cid)
            code = str(cq["from"]["id"])
            if not c:
                tg("answerCallbackQuery", callback_query_id=cq["id"], text="Сбор не найден", show_alert=True)
            elif c["status"] != "active":
                tg("answerCallbackQuery", callback_query_id=cq["id"], text="Сбор уже закрыт", show_alert=True)
            else:
                with LOCK:
                    p = payments_of(cid)
                    changed = False
                    if code in p:
                        p.pop(code)
                        changed = True
                    dec = c.setdefault("declined", [])
                    if code in dec:
                        dec.remove(code)
                        joined = c.setdefault("joined", [])
                        if code not in joined:
                            joined.append(code)
                        changed = True
                    if changed:
                        save_state()
                tg("answerCallbackQuery", callback_query_id=cq["id"],
                   text="Снова в ожидании 💤")
    else:
        tg("answerCallbackQuery", callback_query_id=cq["id"])
    if act in ("p", "d", "j") and STATE["collections"].get(cid):
        finalize(cid)
        update_card(cid)


# ────────────────────────────────────────────────── веб-вью (для приложения бота)

@state_locked
def build_view(cid, me_code):
    c = STATE["collections"].get(cid)
    if not c:
        return None
    paid = payments_of(cid)
    me_paid = me_code in paid
    members = members_of(cid)
    declined = set(c.get("declined") or [])

    roster = []
    for code in members:
        u = user_of(code)
        p = paid.get(code)
        row = {"id": code, "paid": bool(p), "me": code == me_code,
               "declined": code in declined}
        if row["me"]:
            row["name"] = "Ты"
            if p:
                row["amount"] = p["amount"]
        else:
            private = c["anonymous"] and me_code != c["created_by"]
            row["name"] = "Участник %d" % (len(roster) + 1) if private else u["name"]
            row["username"] = "" if private else u["username"]
            if private:
                row["id"] = "anonymous_%d" % len(roster)
            if p:
                row["amount"] = p["amount"]
        roster.append(row)

    me_u = user_of(me_code)
    dl, left_days = deadline_left(c["deadline"])
    me_state = "paid" if me_paid else ("declined" if me_code in declined
               else ("waiting" if me_code in members else "none"))
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
        "created_by": c["created_by"],
        "organizer": {"name": user_of(c["created_by"])["name"], "username": user_of(c["created_by"])["username"]},
        "paid_count": len(paid),
        "unpaid_count": len(unpaid_codes(cid)),
        "total": total_collected(cid),
        "payments": [{"amount": p["amount"]} for p in paid.values()],
        "my_paid": me_paid,
        "me_state": me_state,
        "my_amount": paid.get(me_code, {}).get("amount"),
        "roster": roster,
        "link": deep_link(cid),
    }
    return view


def my_items(me_code):
    items = []
    hidden = set((STATE.get("hidden") or {}).get(me_code) or [])
    for c in STATE["collections"].values():
        if c["id"] in hidden:
            continue  # юзер убрал этот сбор из своих — у других он остался
        paid = payments_of(c["id"])
        if me_code not in paid and me_code not in members_of(c["id"]):
            continue
        dl, _ = deadline_left(c["deadline"])
        items.append({
            "id": c["id"], "title": c["title"], "deadline_left": dl,
            "my_paid": me_code in paid, "currency": c["currency"],
            "paid_count": len(paid), "unpaid_count": len(unpaid_codes(c["id"])),
            "is_organizer": c["created_by"] == me_code,
            "status": c["status"],
        })
    # активные сверху, завершённые снизу; внутри — ближайший дедлайн первым
    items.sort(key=lambda x: (x["status"] != "active", x["deadline_left"] == ""))
    return items


def build_stats():
    """Сводка для владельца: пользователи, сборы, оплаты, активность."""
    with LOCK:
        users = copy.deepcopy(STATE.get("users") or {})
        collections = copy.deepcopy(STATE.get("collections") or {})
        payments = copy.deepcopy(STATE.get("payments") or {})
        hidden = copy.deepcopy(STATE.get("hidden") or {})
    test_ids = TEST_USER_IDS
    real_users = {uid: u for uid, u in users.items() if uid not in test_ids and not str(uid).startswith("900")}
    active = [c for c in collections.values() if c.get("status") == "active"]
    closed = [c for c in collections.values() if c.get("status") != "active"]
    pay_count = 0
    total_paid = 0
    for cid, p in payments.items():
        for uid, info in (p or {}).items():
            pay_count += 1
            total_paid += (info or {}).get("amount") or 0
    participants = set()
    for c in collections.values():
        participants.update(c.get("joined") or [])
        participants.update(c.get("declined") or [])
    # суммы по сборам
    coll_amounts = []
    for c in collections.values():
        p = payments.get(c["id"]) or {}
        amt = sum((info or {}).get("amount", 0) for info in p.values())
        coll_amounts.append((amt, c))
    top = sorted(coll_amounts, key=lambda x: x[0], reverse=True)[:5]
    recent = sorted(collections.values(), key=lambda c: c.get("created_ts") or 0, reverse=True)[:12]
    # активность за 7 дней
    now = time.time()
    week_ago = now - 7 * 86400
    colls_7 = sum(1 for c in collections.values() if (c.get("created_ts") or 0) >= week_ago)
    pays_7 = 0
    for cid, p in payments.items():
        for info in (p or {}).values():
            if (info or {}).get("paid_ts", 0) >= week_ago:
                pays_7 += 1
    hidden_count = sum(len(v or []) for v in hidden.values())
    left_count = 0
    for c in collections.values():
        # «вышли» не хранятся отдельно — считаем declined? оставим 0
        pass
    return {
        "users_total": len(users),
        "users_real": len(real_users),
        "users_test": len(users) - len(real_users),
        "collections_total": len(collections),
        "collections_active": len(active),
        "collections_closed": len(closed),
        "payments_count": pay_count,
        "payments_sum": total_paid,
        "participants_unique": len(participants),
        "hidden_marks": hidden_count,
        "colls_7": colls_7,
        "pays_7": pays_7,
        "top": top,
        "recent": recent,
        "real_users": real_users,
        "payments": payments,
    }


def cmd_stat(msg):
    """Статистика — только для владельца."""
    code = str((msg.get("from") or {}).get("id"))
    if code != str(OWNER_ID):
        return
    s = build_stats()
    lines = [
        "📊 <b>Статистика бота</b>",
        "",
        "👥 Пользователей: %d (реальных %d, тестовых %d)" % (s["users_total"], s["users_real"], s["users_test"]),
        "🗂 Сборов: %d (активных %d, закрытых %d)" % (s["collections_total"], s["collections_active"], s["collections_closed"]),
        "💸 Оплат: %d на сумму %d ₽" % (s["payments_count"], s["payments_sum"]),
        "🤝 Уникальных участников: %d" % s["participants_unique"],
        "🙈 Скрытых у себя: %d" % s["hidden_marks"],
        "",
        "За 7 дней: сборов %d, оплат %d" % (s["colls_7"], s["pays_7"]),
    ]
    if s["top"]:
        lines.append("")
        lines.append("🏆 Топ по суммам:")
        for amt, c in s["top"]:
            if amt <= 0:
                continue
            lines.append("  • %s — %d ₽ (%s)" % (html.escape(c.get("title", "?")), amt, c.get("status", "?")))
    tg("sendMessage", chat_id=OWNER_ID, text="\n".join(lines), parse_mode="HTML")


def delete_collection(cid, by=None):
    """Удалить сбор и всё, что к нему прицеплено. Ошибка — строкой, иначе None."""
    with LOCK:
        c = STATE["collections"].get(cid)
        if not c:
            return "нет такого сбора"
        if by is not None and c["created_by"] != str(by):
            return "только организатор"
        chat_id, message_id, title = c.get("chat_id"), c.get("message_id"), c["title"]
        STATE["collections"].pop(cid, None)
        STATE["payments"].pop(cid, None)
        for ids in STATE.get("hidden", {}).values():  # скрытый сбор удалён — чистим списки
            while cid in ids:
                ids.remove(cid)
        for k in [k for k in STATE["nags"] if k.startswith(cid + ":")]:
            STATE["nags"].pop(k, None)
        for code, pending in list(STATE.get("pendings", {}).items()):
            if pending.get("cid") == cid:
                STATE["pendings"].pop(code, None)
        save_state()
    cards = dict(c.get("cards") or {})
    if chat_id and message_id:
        cards.setdefault(str(chat_id), message_id)
    after_state(delete_cards, cards, title)
    return None


def delete_cards(cards, title):
    for card_chat, mid in cards.items():
        if not mid or not BOT_TOKEN:
            continue
        # телега не отдаёт deleteMessage для сообщений старше 48ч — тогда перепишем
        if not tg("deleteMessage", chat_id=int(card_chat), message_id=mid):
            tg("editMessageText", chat_id=int(card_chat), message_id=mid,
               text="%s — 🗑 сбор удалён организатором" % html.escape(title),
               parse_mode="HTML", reply_markup={"inline_keyboard": []}, disable_web_page_preview=True)
        CARD_CACHE.pop("%s:%s" % (card_chat, mid), None)


def valid_amount(v):
    """int > 0 или None -> None; мусор -> строка ошибки."""
    if v is None or v == "":
        return None
    if type(v) is not int and not (isinstance(v, str) and re.fullmatch(r"[0-9]{1,8}", v)):
        return "Сумма — просто число, например 400"
    v = int(v)
    if v <= 0 or v > 10_000_000:
        return "Странная сумма"
    return v


def valid_deadline(v):
    """Дедлайн «YYYY-MM-DD HH:MM» (можно с «±HHMM» — локальная зона юзера) или None/bad_date."""
    if v is None or v == "":
        return None
    if not isinstance(v, str):
        return "bad_date"
    v = v.strip()
    if len(v) > 16:
        try:
            datetime.strptime(v, "%Y-%m-%d %H:%M %z")
            return v
        except (TypeError, ValueError):
            return "bad_date"
    try:
        datetime.strptime(v, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "bad_date"
    return v


# ────────────────────────────────────────────────── напоминания (бывают и без дедлайна)

def nag(cid, code, stage, text):
    return after_state(_nag, cid, code, stage, text)


def _nag(cid, code, stage, text):
    key = "%s:%s:%s" % (cid, code, stage)
    with LOCK:
        if key in STATE["nags"]:
            return
    if BOT_TOKEN:
        sent = tg("sendMessage", chat_id=int(code), text=text,
           reply_markup={"inline_keyboard": [[{"text": "💸 Открыть сбор", "url": deep_link(cid)}]]},
           disable_web_page_preview=True)
        if not sent:
            return
    else:
        log.info("[демо] не отправляю юзеру %s: %s", code, text[:60])
    with LOCK:
        STATE["nags"][key] = time.time()
        save_state()


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


@state_locked
def tick_collection(cid):
    with LOCK:
        c = get_collection(cid)
        if not c or c["status"] != "active":
            return
        debtors = [u for u in unpaid_codes(cid) if u != c["created_by"]]
        left = None
        if c["deadline"]:
            dl = parse_deadline(c["deadline"])
            if dl:
                left = dl - datetime.now(dl.tzinfo)
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
            # организатору — разово: время вышло, сколько людей не отметилось
            nag(cid, c["created_by"], "timeup",
                "⏰ Время по «%s» вышло. Не скинулись: %d. Дай им шанс до финального подсчёта "
                "или продли дедлайн (/edit)" % (c["title"], len(unpaid_codes(cid))))
            for code in debtors:
                nag(cid, code, "t0",
                    "🏁 Дедлайн по «%s» прошёл. Если ещё не скинулся — жми «Я скинулся» до финального подсчёта 💀"
                    % c["title"])
            return
        if t <= -86400:
            close_collection(cid, c["created_by"], auto=True)
            return
    elif age_h > 24 * 14:
        nag(cid, c["created_by"], "stale", "👋 Сбор «%s» висит 2 недельки. Если он уже не нужен — закрой: /close" % c["title"])


def people_word(n):
    """1 человек / 2 человека / 5 человек."""
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return "человек"
    n %= 10
    if n == 1:
        return "человек"
    if 2 <= n <= 4:
        return "человека"
    return "человек"


def close_collection(cid, by=None, auto=False):
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
        names_missed = [user_of(m)["name"] for m in missed]
        total = total_collected(cid)
        chat = c["chat_id"]
        save_state()

    if chat and BOT_TOKEN:
        text = final_text(cid, c, len(paid), total, names_missed)
        tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True)
    elif not chat and BOT_TOKEN:
        # сбор без чата (создан в аппе) — чтобы люди знали, итог приходит в личку каждому
        text = final_text(cid, c, len(paid), total, names_missed)
        for m in set(paid.keys()) | {c["created_by"]}:
            tg("sendMessage", chat_id=int(m), text=text, parse_mode="HTML",
               disable_web_page_preview=True)
    update_card(cid)
    return True


def final_text(cid, c, n_paid, total, missed):
    title = html.escape(c["title"])
    # missed может прийти кодами — переводим в имена
    missed = [user_of(x)["name"] if str(x).isdigit() else str(x) for x in missed]
    if c["anonymous"]:
        missed = ["участников: %d" % len(missed)] if missed else []
    paid_names = []
    for code, p in sorted(payments_of(cid).items(), key=lambda kv: kv[1]["paid_ts"]):
        nm = "Участник %d" % (len(paid_names) + 1) if c["anonymous"] else user_of(code)["name"]
        paid_names.append("%s — %s" % (html.escape(nm), money(p["amount"], c["currency"])))
    if missed:
        text = ("🏁 <b>«%s» — итог</b>\n💰 Собрано %s от %d %s" % (
            title, money(total, c["currency"]), n_paid, people_word(n_paid)))
        if paid_names:
            text += "\n✅ " + ", ".join(paid_names)
        text += "\n⏳ Не отметились: " + html.escape(", ".join(missed))
    else:
        text = ("🎉 <b>«%s» — всё собрано!</b>\n💰 %s от %d %s" % (
            title, money(total, c["currency"]), n_paid, people_word(n_paid)))
        if paid_names:
            text += "\n✅ " + ", ".join(paid_names)
    return text


def finalize(cid):
    """mode=total — закрываем только когда набрана цель; иначе — когда все, кто не отказался, скинулись."""
    with LOCK:
        c = get_collection(cid)
        if not c or c["status"] != "active":
            return
        if not payments_of(cid):
            return
        if c["mode"] == "total" and c["goal_total"]:
            if total_collected(cid) < c["goal_total"]:
                return  # план не набран — сбор живёт, даже если все отметились
        elif unpaid_codes(cid):
            return  # остались должники — живём
        close_collection(cid, auto=True)


# ────────────────────────────────────────────────── команды бота

def cmd_start(msg, payload=""):
    if payload and payload in STATE["collections"]:
        # пришли по chat-ссылке вида t.me/skidemsya_bot?start=c_xxx —
        # показываем карточку сбора прямо в личном чате, аппа не нужна
        cid = payload
        c = STATE["collections"][cid]
        if c["status"] != "active":
            # закрытый сбор: deep-link открывает аппу с итогами; дублировать «Сбор
            # закрыт» в личку нельзя — у юзера карточка и так уже перед глазами
            return
        cards = c.setdefault("cards", {})
        if str(msg["chat"]["id"]) not in cards:
            cards[str(msg["chat"]["id"])] = None
        tag = "👋 Привет! Тебя позвали в сбор \u2014 отвечай прямо здесь, кнопками ниже"
        tg("sendMessage", chat_id=msg["chat"]["id"], text=tag, disable_web_page_preview=True)
        update_card(cid)
        tg("sendMessage", chat_id=msg["chat"]["id"],
           text="💡 Если аппа вдруг не открывается \u2014 всё то же самое можно делать здесь, кнопками на карточке.",
           disable_web_page_preview=True)
        return
    tg("sendMessage",
       chat_id=msg["chat"]["id"],
       text="Привет! 👋 Это <b>«Скинулись»</b> 💸 — честный учёт общих трат.\n\n"
            "Деньги вы скидываете друг другу сами, бот только ведёт список:\n\n"
            "1️⃣ Создай сбор — <code>/skor Пицца по 400</code> или кнопкой «💸 Создать сбор» внизу 👇\n"
            "2️⃣ Зови друзей — они заходят и жмут кнопки прямо в чате ✅\n"
            "3️⃣ Как все скинулись — бот объявит итог 🎉\n\n"
            "📲 Открывай <b>кнопку «📱 Открыть» слева от поля ввода</b> (☰) — "
            "там приложение: создание сборов, реквизиты, кто что скинул.\n"
            "Всё то же самое работает и прямо в чате — командами или кнопками внизу 👇",
       reply_markup=reply_keyboard(),
       parse_mode="HTML", disable_web_page_preview=True)


def chat_my_items(code):
    """Активные сборы юзера (везде), для кнопки «🗂 Мои сборы»."""
    out = []
    for c in STATE["collections"].values():
        if c["status"] != "active":
            continue
        if code == c["created_by"] or code in (c.get("joined") or []):
            out.append(c)
    out.sort(key=lambda x: x["created_ts"], reverse=True)
    return out


def send_create_manual(chat_id):
    tg("sendMessage", chat_id=chat_id,
       text="💸 <b>Создать сбор</b>\n\n"
            "1️⃣ В чате — напиши команду прямо сюда:\n"
            "<code>/skor Пицца по 400 до 25.10</code>\n"
            "   · <b>по 400</b> — с каждого\n"
            "   · <b>на 5000</b> — общая копилка\n"
            "   · <b>кто сколько хочет</b> — суммы у каждого свои\n"
            "   · <b>до 25.10</b> или <b>до 25.10 20:00</b> — дедлайн\n\n"
            "2️⃣ В боте — экран с полями: жми ☰ «📱 Открыть» слева от поля ввода 👇\n\n"
            "Карточка появится в чате сразу — зови друзей кнопкой «🔗 Позвать друзей».",
       reply_markup={"inline_keyboard": [[
           {"text": "📱 Создать в боте", "url": deep_link("new")},
       ]]},
       parse_mode="HTML", disable_web_page_preview=True)


def send_my_manual(chat_id, me_code):
    items = chat_my_items(me_code)
    if not items:
        tg("sendMessage", chat_id=chat_id,
           text="🗂 Активных сборов у тебя нет.\n\n"
                "Создай: <code>/skor Пицца по 400</code> — или ☰ «📱 Открыть» слева от поля ввода 👇",
           parse_mode="HTML", disable_web_page_preview=True)
        return
    expl = "🗂 <b>Твои активные сборы</b> — выложил карточки ниже, жми кнопки на них:"
    for c in items[:5]:
        dl, _ = deadline_left(c["deadline"])
        unpaid = len(unpaid_codes(c["id"]))
        expl += ("\n\n💸 <b>%s</b> · скинулись %d · ждём %d%s"
                 % (html.escape(c["title"]), len(payments_of(c["id"])), unpaid,
                    (" · " + dl.strip()) if dl.strip() else ""))
        # карточку в чат юзера — сразу с кнопками
        post_card(c["id"], chat_id)
    if len(items) > 5:
        expl += "\n\n… и ещё %d — /list показывает сборы чата" % (len(items) - 5)
    tg("sendMessage", chat_id=chat_id, text=expl,
       parse_mode="HTML", disable_web_page_preview=True)


def send_how_manual(chat_id):
    tg("sendMessage", chat_id=chat_id,
       text="❓ <b>Как пользоваться «Скинулись»</b>\n\n"
            "Деньги вы переводите друг другу сами — бот просто честно ведёт счёт 👀\n\n"
            "💬 <b>Вариант 1 — прямо в чате (проще всего)</b>\n"
            "1. Создай: <code>/skor Пицца по 400</code>\n"
            "2. В чате появится карточка с кнопками:\n"
            "   ✅ Я скинулся · 🙅 Не участвую · 🔁 Передумал · 🔗 Позвать друзей\n"
            "3. «🔗 Позвать друзей» — кинь ссылку, друзья зайдут и жмут те же кнопки\n"
            "4. Если «на 5000» или «кто сколько хочет» — после ✅ напиши мне сумму числом, например «350»\n"
            "5. Как все скинулись (или набралась сумма) — бот объявит итог 🎉\n\n"
            "📱 <b>Вариант 2 — приложение (мини-апп, большой экран)</b>\n"
            "Жми <b>☰ «📱 Открыть» слева от поля ввода</b> — то же самое, но с большим экраном: "
            "списки, реквизиты и итоги там заметнее.\n\n"
            "⌨️ <b>Команды</b> (список всегда под кнопкой «Меню» слева внизу):\n"
            "<code>/skor Название по 400</code> — создать\n"
            "<code>/list</code> — сборы этого чата\n"
            "<code>/close</code> — закрыть свой сбор\n"
            "<code>/delete код</code> — удалить\n\n"
            "Остались вопросы — это сообщение всегда рядом: <code>/help</code>",
       reply_markup=reply_keyboard(),
       parse_mode="HTML", disable_web_page_preview=True)


@state_locked
def handle_amount_text(msg, text):
    """Юзер написал боту без команды: кнопки внизу, сумма (если мы её ждали) или подсказка."""
    user_code = uid(msg["from"])
    chat_id = msg["chat"]["id"]
    t = text.strip()
    # кнопки reply-клавиатуры
    act = REPLY_ACTIONS.get(t)
    if act == "app":
        # «📱 Открыть в боте» из старой клавиатуры: в новой этой кнопки нет —
        # открывать аппу теперь нужно ☰ слева («📱 Открыть»). Направляем юзера
        # туда и на всякий случай даём deep-link (он открывает с initData).
        tg("sendMessage", chat_id=chat_id,
           text="Кнопку «Открыть в боте» я убрал 🙂 Теперь открывай аппу кнопкой <b>☰ «📱 Открыть»</b> слева от поля ввода — она открывает приложение сразу.",
           reply_markup={"inline_keyboard": [[
               {"text": "📱 Открыть приложение 💸", "url": deep_link("home")},
           ]]},
           parse_mode="HTML", disable_web_page_preview=True)
        return
    if act == "create":
        send_create_manual(chat_id)
        return
    if act == "my":
        send_my_manual(chat_id, user_code)
        return
    if act == "how":
        send_how_manual(chat_id)
        return
    if act in ("close", "edit", "delete"):
        # те же команды: /close, /edit, /delete — берут последний активный сбор
        fa = {"close": cmd_close, "edit": cmd_edit, "delete": cmd_delete}[act]
        fa({"from": msg["from"], "chat": msg["chat"]}, "")
        return
    p = (STATE.get("pendings") or {}).get(user_code)
    if p and p.get("chat") != chat_id:
        return  # A number in another chat must never pay for or delete this collection.
    if p and p.get("del") and time.time() - p.get("ts", 0) < 7200:
        nums = parse_nums(text)
        if nums is None:
            tg("sendMessage", chat_id=chat_id,
               text="Напиши номера через запятую — например «1, 2» 🙂", disable_web_page_preview=True)
            return
        snapshot = [STATE["collections"].get(cid) for cid in p.get("ids", [])]
        if not snapshot or any(c is None for c in snapshot):
            tg("sendMessage", chat_id=chat_id, text="Список изменился — открой /delete заново")
            return
        STATE["pendings"].pop(user_code, None)
        save_state()
        delete_pick(msg["chat"], user_code, snapshot, nums)
        return
    if p and time.time() - p.get("ts", 0) < 7200:
        cid = p.get("cid")
        c = STATE["collections"].get(cid)
        if not c or c["status"] != "active" or user_code in c.get("left", []):
            STATE["pendings"].pop(user_code, None)
            save_state()
            return
        amount = valid_amount(re.sub(r"[ \u00a0\u202f]", "", text.strip()))
        if amount is None or isinstance(amount, str):
            tg("sendMessage", chat_id=chat_id,
               text="Не понял 🙂 Напиши сумму просто числом — например «350», и я отмечу галочку.",
               disable_web_page_preview=True)
            return
        code = str(msg["from"]["id"])
        with LOCK:
            c = STATE["collections"][cid]
            paid = payments_of(cid)
            prev = paid.get(code)
            # total/free: повторная сумма — это довнесение, складываем
            if prev and c["mode"] != "per_person":
                amount += int(prev.get("amount") or 0)
            if isinstance(valid_amount(amount), str):
                tg("sendMessage", chat_id=chat_id, text="Общая сумма слишком большая")
                return
            STATE["pendings"].pop(user_code, None)
            paid[code] = {"amount": amount, "paid_ts": time.time()}
            if code not in c["joined"]:
                c["joined"].append(code)
            dec = c.setdefault("declined", [])
            if code in dec:
                dec.remove(code)
            save_state()
        finalize(cid)
        update_card(cid)
        tg("sendMessage", chat_id=chat_id,
           text="✅ Отметил: ты скинулся %s. Карточка обновлена 👆" % money(amount, c["currency"]),
           disable_web_page_preview=True)
        return
    tg("sendMessage", chat_id=chat_id,
       text="Я не отвечаю на сообщения 🙂 Вся работа — в боте или по кнопкам на карточке сбора.\n"
            "Команды: /help",
       reply_markup={"inline_keyboard": [[
           {"text": "＋ Создать сбор 💸", "url": deep_link("new")},
       ]]},
       disable_web_page_preview=True)


def cmd_skor(msg, args):
    chat = msg["chat"]
    title, o = parse_skor(args)
    if not args.strip():
        return send_create_manual(chat["id"])
    if o.get("error"):
        return tg("sendMessage", chat_id=chat["id"], text=o["error"])
    with LOCK:
        try:
            c = create_collection(
                title=title, chat_id=chat["id"], created_by=uid(msg["from"]),
                per=o["per"], goal=o["goal"], until=o["until"], anon=o["anon"], cur=o["cur"],
            )
        except ValueError as e:
            return tg("sendMessage", chat_id=chat["id"], text=str(e))
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
    targets, bad = None, None
    with LOCK:
        if cid in STATE["collections"]:
            targets = [cid]
        else:
            mine = [c for c in STATE["collections"].values()
                    if c.get("chat_id") == chat["id"] and c["status"] == "active"
                    and c["created_by"] == who]
            mine.sort(key=lambda x: x["created_ts"])
            nums = parse_nums(cid)
            if nums:
                if max(nums) > len(mine):
                    bad = len(mine)
                else:
                    targets = [mine[n - 1]["id"] for n in nums]
            elif mine:
                targets = [mine[-1]["id"]]
    if targets is None or bad is not None:
        if bad is not None:
            tg("sendMessage", chat_id=chat["id"],
               text="Тут номера от 1 до %d 🙂" % bad, disable_web_page_preview=True)
        else:
            tg("sendMessage", chat_id=chat["id"], text="Твоих активных сборов в этом чате нет.", disable_web_page_preview=True)
        return
    for t in targets:
        close_collection(t, who)


def cmd_help(msg):
    send_how_manual(msg["chat"]["id"])


def cmd_create(msg):
    tg("sendMessage", chat_id=msg["chat"]["id"],
       text="Создать сбор можно прямо в боте — жми кнопку 👇\n\n"
            "<i>Если сбор нужен именно в этом чате (с карточкой для всех) — "
            "<code>/skor Пицца по 400 до пт</code></i>",
       parse_mode="HTML",
       reply_markup={"inline_keyboard": [[{"text": "＋ Создать сбор", "url": deep_link("new")}]]},
       disable_web_page_preview=True)


def deletables(chat, who):
    """Сборы организатора для /delete: активные и закрытые (в личке — все). Порядок — как в /list и /edit."""
    private = chat["type"] == "private"
    with LOCK:
        mine = [c for c in STATE["collections"].values()
                if c["created_by"] == str(who)
                and (private or c.get("chat_id") == chat["id"])]
        mine.sort(key=lambda c: c["created_ts"])
    return mine


def cmd_delete(msg, args):
    chat = msg["chat"]
    who = uid(msg["from"])
    mine = deletables(chat, who)
    if not mine:
        tg("sendMessage", chat_id=chat["id"],
           text="🗑 У тебя пока нет своих сборов. Создать: /skor Пицца по 400", disable_web_page_preview=True)
        return
    nums = parse_nums(args)
    if nums is None:
        lines = ["🗑 <b>Что снести?</b> Номер или несколько: /delete 1, 2, 3…\n"]
        for i, c in enumerate(mine, 1):
            if c["status"] == "closed":
                lines.append("%d) 🏁 %s (закрыт)" % (i, html.escape(c["title"])))
            else:
                lines.append("%d) 💸 %s" % (i, html.escape(c["title"])))
        # ждём ответ «1,2» текстом без префикса тоже
        STATE.setdefault("pendings", {})[who] = {"del": True, "chat": chat["id"],
                                                "ids": [c["id"] for c in mine], "ts": time.time()}
        save_state()
        tg("sendMessage", chat_id=chat["id"], text="\n".join(lines), parse_mode="HTML",
           disable_web_page_preview=True)
        return
    delete_pick(chat, who, mine, nums)


def cmd_edit(msg, args):
    chat = msg["chat"]
    who = uid(msg["from"])
    mine = collections_of(chat, who)
    if not mine:
        tg("sendMessage", chat_id=chat["id"],
           text="Твоих активных сборов тут нет. Создать: /skor Пицца по 400", disable_web_page_preview=True)
        return
    n = parse_num(args)
    if n is None:
        if len(mine) == 1:
            n = 1
        else:
            lines = ["✏️ <b>Что меняем?</b> Пиши номер: <code>/edit 2</code>\n"]
            lines += ["%d) 💸 %s" % (i, html.escape(c["title"])) for i, c in enumerate(mine, 1)]
            tg("sendMessage", chat_id=chat["id"], text="\n".join(lines), parse_mode="HTML",
               disable_web_page_preview=True)
            return
    if n < 1 or n > len(mine):
        tg("sendMessage", chat_id=chat["id"],
           text="Тут номера от 1 до %d 🙂" % len(mine), disable_web_page_preview=True)
        return
    c = mine[n - 1]
    tg("sendMessage", chat_id=chat["id"],
       text="✏️ <b>%s</b> — название, суммы, дедлайн и реквизиты меняются в аппе:"
            % html.escape(c["title"]),
       parse_mode="HTML",
       reply_markup={"inline_keyboard": [[{"text": "Изменить в аппе", "url": deep_link(c["id"])}]]},
       disable_web_page_preview=True)


def collections_of(chat, who):
    """Активные сборы, где who организатор: в этом чате или все (личка)."""
    private = chat["type"] == "private"
    with LOCK:
        mine = [c for c in STATE["collections"].values()
                if c["created_by"] == str(who) and c["status"] == "active"
                and (private or c.get("chat_id") == chat["id"])]
        mine.sort(key=lambda c: c["created_ts"])
    return mine


def parse_num(s):
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return None


def parse_nums(s, cap=20):
    """«1,2» / «1 2» / «1 и 2» -> [1, 2]; без цифр -> None. До cap номеров."""
    if not isinstance(s, str) or not re.fullmatch(r"\s*[0-9]{1,6}(?:[\s,и]+[0-9]{1,6})*\s*", s):
        return None
    nums = [int(x) for x in re.findall(r"\d+", s)]
    if not nums:
        return None
    if any(n < 1 for n in nums) or len(nums) > cap:
        return None
    nums = sorted(set(nums))
    return nums or None


def delete_pick(chat, who, mine, nums):
    """Удалить выбранные номера (список mine — снапшот; индексы 1-based)."""
    if max(nums) > len(mine):
        tg("sendMessage", chat_id=chat["id"],
           text="Тут номера от 1 до %d 🙂" % len(mine), disable_web_page_preview=True)
        return
    done, errs = [], []
    for n in nums:
        c = mine[n - 1]
        err = delete_collection(c["id"], by=who)
        if err:
            errs.append("«%s»: %s" % (html.escape(c["title"]), err))
        else:
            done.append(html.escape(c["title"]))
    lines = []
    if len(done) == 1:
        lines.append("🗑 «%s» удалён." % done[0])
    elif done:
        lines.append("🗑 Удалено %d: %s." % (len(done), ", ".join("«%s»" % t for t in done)))
    if errs:
        lines.append("Не вышло: " + "; ".join(errs))
    tg("sendMessage", chat_id=chat["id"], text="\n".join(lines), parse_mode="HTML",
       disable_web_page_preview=True)


@state_locked
def handle_update(upd):
    if "callback_query" in upd:
        handle_callback(upd)
        save()
        return
    msg = upd.get("message")
    if not msg or "from" not in msg:
        return
    upsert_user(msg.get("from"))
    chat_type = msg["chat"]["type"]
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        # приложение бота работает в своём окне — лично боту пишут только для сумм
        if text and (chat_type == "private" or
                     STATE.get("pendings", {}).get(uid(msg["from"]), {}).get("chat") == msg["chat"]["id"]):
            handle_amount_text(msg, text)
        return
    parts = text.split(" ", 1)
    cmd = parts[0].lower().split("@")[0]
    if "@" in parts[0] and parts[0].split("@", 1)[1].lower() != bot_username.lower():
        return
    rest = parts[1] if len(parts) > 1 else ""
    if cmd == "/start":
        cmd_start(msg, rest.strip())
    elif cmd == "/help":
        cmd_help(msg)
    elif cmd == "/skor":
        cmd_skor(msg, args=parts[1] if len(parts) > 1 else "")
    elif cmd == "/create":
        cmd_create(msg)
    elif cmd == "/list":
        cmd_list(msg)
    elif cmd == "/close":
        cmd_close(msg, args=rest)
    elif cmd == "/delete":
        cmd_delete(msg, args=rest)
    elif cmd == "/edit":
        cmd_edit(msg, args=rest)
    elif cmd == "/stat":
        cmd_stat(msg)
    else:
        pass  # неизвестная команда — молчим
    save()


# ────────────────────────────────────────────────── HTTP сервер


class HTTPServer(ThreadingHTTPServer):
    request_queue_size = 64
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "SkinulisBot/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

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
        if hdr.startswith("tma ") or hdr:
            rd = {}
            u = validate_init_data(hdr[4:] if hdr.startswith("tma ") else hdr, rd)
            if u:
                with LOCK:
                    previous = STATE["users"].get(uid(u))
                    upsert_user(u)
                    if previous != STATE["users"].get(uid(u)):
                        save_state()
                return u
            if not DEV_MODE:
                log.warning("initData отклонён: заголовок=%dб причина=%s",
                            len(hdr), str(rd.get("r"))[:600])
        if DEV_MODE:
            with LOCK:
                return {"id": 900000, "first_name": "Ты (демо)", "username": "tester"}
        return None

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n < 0 or n > 65536:
                raise ValueError("invalid body length")
            data = self.rfile.read(n)
            if len(data) != n:
                raise ValueError("incomplete body")
            def reject_constant(value):
                raise ValueError("invalid JSON number: " + value)
            obj = json.loads(data.decode() or "{}", parse_constant=reject_constant)
            if not isinstance(obj, dict):
                raise ValueError("JSON object required")
            for key in ("title", "pay_hint", "deadline", "hint", "start_param"):
                if key in obj and obj[key] is not None and not isinstance(obj[key], str):
                    raise ValueError("%s must be a string" % key)
            return obj
        except (ValueError, UnicodeError, TimeoutError) as e:
            raise ValueError("Некорректный JSON-запрос") from e

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
        full = os.path.normpath(os.path.join(PUBLIC_DIR, fname))
        if not full.startswith(PUBLIC_DIR + os.sep) or not os.path.isfile(full):
            self.send_json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(full)[1].lower()
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            self.send_json({"error": "not_found"}, 404)
            return
        if fname == "index.html":
            # всё в один запрос: CSS и JS инлайнятся — нахрени слабому LTE качать 3 файла
            try:
                with open(os.path.join(PUBLIC_DIR, "style.css"), "rb") as f:
                    css = f.read()
                with open(os.path.join(PUBLIC_DIR, "app.js"), "rb") as f:
                    js = f.read()
                body = body.replace(
                    b'<link rel="stylesheet" href="/app/style.css">',
                    b'<style>' + css + b'</style>')
                # ВАЖНО: не re.sub! в замене (JS-код) бэкслеши \d \n \s — re.sub читает их
                # как свои esc-правила и портит код: у юзеров это давало
                # «SyntaxError: Unexpected EOF / Invalid token»
                start = body.find(b'<script src="/app/app.js')
                if start != -1:
                    end = body.find(b'</script>', start) + len(b'</script>')
                    body = body[:start] + b'<script>' + js + b'</script>' + body[end:]
            except OSError:
                pass
        self.send_response(200)
        body = self.maybe_gzip(body)
        self.send_header("Content-Type", self.MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        cc = "public, max-age=604800" if fname == "telegram-web-app.js" else "no-store"
        self.send_header("Cache-Control", cc)
        self.end_headers()
        self.wfile.write(body)

    def send_stats_page(self):
        s = build_stats()
        def esc(x): return html.escape(str(x))
        def row(label, value):
            return "<tr><td>%s</td><td><b>%s</b></td></tr>" % (esc(label), esc(value))
        top_html = "".join(
            "<tr><td>%s</td><td>%d ₽</td><td>%s</td></tr>" % (esc(c.get("title", "?")), amt, esc(c.get("status", "?")))
            for amt, c in s["top"] if amt > 0)
        recent_html = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%d</td></tr>" % (
                esc(c.get("title", "?")), esc(c.get("status", "?")),
                datetime.fromtimestamp(c.get("created_ts") or 0).strftime("%d.%m %H:%M"),
                len(c.get("joined") or []))
            for c in s["recent"])
        users_html = "".join(
            "<tr><td>%s</td><td>%s</td></tr>" % (esc(uid), esc(u.get("username") or u.get("name") or "—"))
            for uid, u in sorted(s["real_users"].items()))
        body = ("<!doctype html><html lang=ru><head><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                "<meta http-equiv=refresh content=60>"
                "<title>Скинулись — статистика</title>"
                "<style>body{font:14px/1.5 -apple-system,system-ui,sans-serif;background:#0f1117;color:#eaeaf0;margin:0;padding:20px}"
                "h1{font-size:20px;margin:0 0 14px}h2{font-size:15px;margin:18px 0 6px;color:#8b8b97}"
                "table{width:100%%;border-collapse:collapse;margin:6px 0 14px}td,th{padding:6px 8px;border-bottom:1px solid #2b2b37;text-align:left}"
                "td b{color:#4dc987}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:10px 0 18px}"
                ".card{background:#1a1d26;border:1px solid #2b2b37;border-radius:10px;padding:12px}"
                ".card .v{font-size:22px;font-weight:700;color:#4dc987}.card .l{font-size:12px;color:#8b8b97;margin-top:2px}"
                ".muted{color:#8b8b97;font-size:12px}</style></head><body>"
                "<h1>📊 Скинулись — статистика</h1>"
                "<div class=muted>Обновляется каждые 60 с. Время: %s</div>"
                "<div class=cards>"
                "<div class=card><div class=v>%d</div><div class=l>Пользователей (всего)</div></div>"
                "<div class=card><div class=v>%d</div><div class=l>Реальных (без тестовых)</div></div>"
                "<div class=card><div class=v>%d</div><div class=l>Сборов (активных)</div></div>"
                "<div class=card><div class=v>%d</div><div class=l>Сборов (закрытых)</div></div>"
                "<div class=card><div class=v>%d</div><div class=l>Оплат (шт.)</div></div>"
                "<div class=card><div class=v>%d ₽</div><div class=l>Сумма оплат</div></div>"
                "<div class=card><div class=v>%d</div><div class=l>Уникальных участников</div></div>"
                "<div class=card><div class=v>%d / %d</div><div class=l>За 7 дней: сборов / оплат</div></div>"
                "</div>"
                "<h2>🏆 Топ сборов по сумме</h2>"
                "<table><tr><th>Название</th><th>Сумма</th><th>Статус</th></tr>%s</table>"
                "<h2>🗂 Последние сборы</h2>"
                "<table><tr><th>Название</th><th>Статус</th><th>Создан</th><th>Участников</th></tr>%s</table>"
                "<h2>👥 Пользователи</h2>"
                "<table><tr><th>ID</th><th>Имя / @username</th></tr>%s</table>"
                "</body></html>" % (
            datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            s["users_total"], s["users_real"],
            s["collections_active"], s["collections_closed"],
            s["payments_count"], s["payments_sum"], s["participants_unique"],
            s["colls_7"], s["pays_7"],
            top_html or "<tr><td colspan=3 class=muted>пока нет оплат</td></tr>",
            recent_html or "<tr><td colspan=4 class=muted>пока нет сборов</td></tr>",
            users_html or "<tr><td colspan=2 class=muted>—</td></tr>"))
        body_b = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body_b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body_b)

    def maybe_gzip(self, body):
        """На слабом LTE обрыв 45-килобайтного ответа давал SyntaxError: Unexpected EOF.
        Сжимаем до ~12 КБ — грузится быстрее и меньше поверхность обрыва."""
        if "gzip" not in (self.headers.get("Accept-Encoding") or ""):
            return body
        g = gzip.compress(body, 6)
        if len(g) >= len(body):
            return body
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        return g

    # ---------- API

    def do_POST(self):
        try:
            body = self.read_json()
        except ValueError as e:
            self.close_connection = True
            return self.send_json({"error": str(e)}, 400)
        return self.post_json(body)

    @state_locked
    def post_json(self, body):
        path = self.path.split("?")[0]
        # диагностика клиентов без initData (App Store / Web): что именно пришло в URL и есть ли мост
        if path == "/api/clientdiag":
            # No unauthenticated disk writes or storage of launch URLs/credentials.
            return self.send_json({"ok": True})
        # «визитка»: один пинг на загрузку страницы (без auth). Показывает, что
        # страница вообще дошла до клиента, в каком окружении и на каком этапе
        # умерла, если JS упал (stage=load/appjs/js-error).
        if path == "/api/visit":
            return self.send_json({"ok": True})
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
                        # участие теперь явное: открыл ссылку — смотри, «В деле» нажимаешь — в списке
                        view = build_view(start, code)
                if c:
                    update_card(start)  # на всякий случай освежить карточку
            me_view = {"id": code, "name": nice_name(me), "username": me.get("username")}
            return self.send_json({"user": me_view, "collection": view,
                                   "bot_username": bot_username or "skinulis_bot"})

        if path == "/api/create":
            title = (body.get("title") or "").strip()
            hint = (body.get("pay_hint") or "")[:200]
            if not title:
                return self.send_json({"error": "Название надо придумать 🙂"}, 400)
            if len(title) > 64:
                return self.send_json({"error": "Название длинновато — до 64 символов"}, 400)
            per = valid_amount(body.get("per_person"))
            goal = valid_amount(body.get("goal_total"))
            if isinstance(per, str):
                return self.send_json({"error": per}, 400)
            if isinstance(goal, str):
                return self.send_json({"error": goal}, 400)
            if per and goal:
                return self.send_json({"error": "Выбери один режим сбора"}, 400)
            until = valid_deadline((body.get("deadline") or "").strip())
            if until == "bad_date":
                return self.send_json({"error": "Дедлайн сломался, попробуй ещё раз"}, 400)
            with LOCK:
                c = create_collection(title=title, chat_id=None, created_by=code,
                                      per=per, goal=goal, until=until, cur="₽")
                c["pay_hint"] = hint
                cid = c["id"]
                save_state()
            return self.send_json({"view": build_view(cid, code)})

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
                if code in c.get("left", []):
                    return self.send_json({"error": "Сначала вернись в сбор: «В деле»"}, 409)
                if amount is None and c["mode"] == "per_person" and c["per_person"]:
                    amount = c["per_person"]
                amount = valid_amount(amount)
                if amount is None or isinstance(amount, str):
                    return self.send_json({"error": "Странная сумма"}, 400)
                if c["mode"] == "per_person" and amount != c["per_person"]:
                    return self.send_json({"error": "Сумма должна совпадать со взносом"}, 400)
                if code not in c["joined"]:
                    c["joined"].append(code)
                payments_of(cid)[code] = {"amount": amount, "paid_ts": time.time()}
                if code in c.get("declined", []):
                    c["declined"].remove(code)
                save_state()
            update_card(cid)
            finalize(cid)
            return self.send_json({"view": build_view(cid, code)})

        if action == "unpay":
            if c["status"] != "active":
                return self.send_json({"error": "Сбор закрыт"}, 409)
            with LOCK:
                p = payments_of(cid)
                had = p.pop(code, None)
                if had:
                    save_state()
            update_card(cid)
            return self.send_json({"view": build_view(cid, code)})

        if action == "remove":
            if code != c["created_by"]:
                return self.send_json({"error": "только организатор"}, 403)
            if c["status"] != "active":
                return self.send_json({"error": "Сбор закрыт"}, 409)
            target = str(body.get("code") or "")
            if not target or target == code:
                return self.send_json({"error": "Себя remove — закры сбор 👌"}, 400)
            if target in payments_of(cid):
                return self.send_json({"error": "Уже скинулся — галочку убирать не буду"}, 409)
            with LOCK:
                cut = False
                if target in c["joined"]:
                    c["joined"].remove(target)
                    cut = True
                dec = c.get("declined") or []
                if target in dec:
                    dec.remove(target)
                    save_state()
                if cut:
                    save_state()
            update_card(cid)
            finalize(cid)
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
            close_collection(cid, by=code)
            return self.send_json({"view": build_view(cid, code)})

        if action == "edit":
            if code != c["created_by"]:
                return self.send_json({"error": "только организатор"}, 403)
            if c["status"] != "active":
                return self.send_json({"error": "Сбор закрыт — создание новенького решит всё"}, 409)
            original = c
            c = copy.deepcopy(c)
            title = (body.get("title") or "").strip()
            if "title" in body and not title:
                return self.send_json({"error": "Название надо придумать"}, 400)
            if title:
                if len(title) > 64:
                    return self.send_json({"error": "Название длинновато — до 64 символов"}, 400)
                c["title"] = title
            if "per_person" in body:
                per = valid_amount(body.get("per_person"))
                if isinstance(per, str):
                    return self.send_json({"error": per}, 400)
                c["per_person"] = per
                if "goal_total" not in body and per:
                    c["goal_total"] = None
            if "goal_total" in body:
                goal = valid_amount(body.get("goal_total"))
                if isinstance(goal, str):
                    return self.send_json({"error": goal}, 400)
                c["goal_total"] = goal
                if "per_person" not in body and goal:
                    c["per_person"] = None
            c["mode"] = "per_person" if c["per_person"] else ("total" if c["goal_total"] else "free")
            if c["per_person"] and c["goal_total"]:
                return self.send_json({"error": "Выбери один режим сбора"}, 400)
            if "deadline" in body:
                until = valid_deadline((body.get("deadline") or "").strip())
                if until == "bad_date":
                    return self.send_json({"error": "Дедлайн сломался, попробуй ещё раз"}, 400)
                c["deadline"] = until
            if "pay_hint" in body:
                c["pay_hint"] = (body.get("pay_hint") or "")[:200]
            with LOCK:
                original.update(c)
                save_state()
            update_card(cid)
            finalize(cid)
            return self.send_json({"view": build_view(cid, code)})

        if action == "join":
            if c["status"] != "active":
                return self.send_json({"error": "Сбор закрыт"}, 409)
            with LOCK:
                if code in c.get("left", []):
                    c["left"].remove(code)
                dec = c.get("declined") or []
                if code in dec:
                    dec.remove(code)
                    save_state()
                if code not in c["joined"]:
                    c["joined"].append(code)
                save_state()
            update_card(cid)
            return self.send_json({"view": build_view(cid, code)})

        if action == "decline":
            if c["status"] != "active":
                return self.send_json({"error": "Сбор закрыт"}, 409)
            if code in (c.get("left") or []):
                return self.send_json({"error": "Ты вышел из сбора — вернуться можно «⭕ В деле» 👆"}, 409)
            if code in payments_of(cid):
                return self.send_json({"error": "Ты уже скинулся — сначала убери галочку ↩️"}, 409)
            with LOCK:
                if code not in c["joined"]:
                    c["joined"].append(code)
                dec = c.setdefault("declined", [])
                if code not in dec:
                    dec.append(code)
                save_state()
            update_card(cid)
            finalize(cid)
            return self.send_json({"view": build_view(cid, code)})

        if action == "add":
            return self.send_json({"error": "звать можно только ссылкой"}, 404)

        if action == "delete":
            if code == c["created_by"]:
                err = delete_collection(cid, by=code)
                if err:
                    return self.send_json({"error": err}, err == "только организатор" and 403 or 404)
                return self.send_json({"deleted": True, "id": cid})
            # не организатор: убрать только у себя — у организатора и остальных сбор остаётся
            with LOCK:
                STATE.setdefault("hidden", {}).setdefault(code, [])
                if cid not in STATE["hidden"][code]:
                    STATE["hidden"][code].append(cid)
                save_state()
            return self.send_json({"hidden": True, "id": cid})

        if action == "leave":
            # участник выходит: пропадает из списка участников, сбор остаётся у остальных
            if c["status"] != "active":
                return self.send_json({"error": "Сбор закрыт"}, 409)
            if code == c["created_by"]:
                return self.send_json({"error": "Ты организатор — сбор можно завершить или удалить"}, 400)
            with LOCK:
                payments_of(cid)
                if code in payments_of(cid):
                    return self.send_json({"error": "Ты скинулся — сначала «Передумал» 🔄"}, 409)
                changed = False
                if code in c["joined"]:
                    c["joined"].remove(code)
                    changed = True
                dec = c.get("declined") or []
                if code in dec:
                    dec.remove(code)
                    changed = True
                if not changed:
                    return self.send_json({"error": "Тебя и так нет в этом сборе"}, 400)
                c.setdefault("left", [])
                if code not in c["left"]:
                    c["left"].append(code)
                save_state()
            update_card(cid)
            # личная карточка вышедшего: снять кнопки и честно сказать, что он вышел
            with LOCK:
                cards = c.setdefault("cards", {})
                mid = cards.pop(str(code), None)
                if mid:
                    save_state()
            if mid and BOT_TOKEN:
                tg("editMessageText", chat_id=int(code), message_id=mid,
                   text="🚪 <b>Ты вышел из сбора «%s»</b>.\nУ остальных он остался. Вернуться можно в приложении — «⭕ В деле» по ссылке. 🙃"
                        % html.escape(c["title"]),
                   parse_mode="HTML", reply_markup={"inline_keyboard": []},
                   disable_web_page_preview=True)
            finalize(cid)
            return self.send_json({"left": True, "view": build_view(cid, code)})

        return self.send_json({"error": "не знаю такое"}, 404)

    def do_GET(self):
        path = self.path.split("?")[0]
        me = self.authorize() if path.startswith("/api") else None
        if path == "/health":  # пульс для мониторинга bothost
            return self.send_json({"ok": True, "port": PORT, "bot": bool(BOT_TOKEN)})
        if path == "/stats":
            # статистика только для владельца: по секретному ключу (из URL) или по initData владельца
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            key = (qs.get("key") or [""])[0]
            owner_by_key = bool(key and STATS_KEY) and secrets.compare_digest(key.encode(), STATS_KEY.encode())
            owner_by_init = False
            if not owner_by_key:
                try:
                    init_hdr = self.headers.get("Authorization", "")
                    rd = {}
                    u = validate_init_data(init_hdr[4:] if init_hdr.startswith("tma ") else init_hdr, rd)
                    owner_by_init = bool(u) and str(uid(u)) == str(OWNER_ID)
                except Exception:
                    owner_by_init = False
            if not (owner_by_key or owner_by_init):
                return self.send_json({"error": "not_found"}, 404)
            return self.send_stats_page()
        if path.startswith("/api"):
            if not me:
                return self.send_json({"error": "нужен Telegram initData"}, 401)
            code = uid(me)
            if path == "/api/my":
                with LOCK:
                    items = my_items(code)
                return self.send_json({"items": items, "bot_username": bot_username or "skinulis_bot"})
            mu = re.match(r"^/api/users(?:/([a-z0-9_]+))?$", path)
            if mu:
                # приватность: пул «кого позвать» вырезан — звать можно только ссылкой
                return self.send_json({"error": "звать можно только ссылкой"}, 404)
            m = re.match(r"^/api/c/([a-z0-9_]+)$", path)
            if m:
                with LOCK:
                    cid = m.group(1)
                    c = get_collection(cid)
                    if not c:
                        return self.send_json({"error": "нет такого сбора"}, 404)
                    dec = set(c.get("declined") or [])
                    if code != c["created_by"] and code not in members_of(cid) \
                            and code not in dec:
                        # приватность: без ссылки от организатора чужие сборы не видны
                        return self.send_json(
                            {"error": "Нет доступа — открой сбор по ссылке от организатора"}, 403)
                    v = build_view(cid, code)
                return self.send_json(v)
            return self.send_json({"error": "not_found"}, 404)
        return self.serve_static(path)

    def log_message(self, fmt, *args):
        message = re.sub(r'([?&]key=)[^ &"]*', r'\1[redacted]', fmt % args)
        log.info(message)


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
        r = tg("getUpdates", offset=offset, timeout=25,
               allowed_updates=["message", "callback_query"])
        if r is None:
            time.sleep(3)
            continue
        for upd in r:
            offset = upd.get("update_id", offset) + 1
            try:
                handle_update(upd)
            except Exception as e:
                log.warning("update обработка: %s", e)


BOT_COMMANDS = [
    ("skor",   "Создать сбор: /skor Пицца по 400"),
    ("list",   "Сборы этого чата"),
    ("close",  "Закрыть свой последний сбор"),
    ("delete", "Удалить свой сбор"),
    ("edit",   "Изменить название, сумму, дедлайн"),
    ("help",   "Как пользоваться ботом"),
]


WEBAPP_URL = os.environ.get(
    "WEBAPP_URL", "https://bot-1792527651-5464-faslor.bothost.tech/app")

# ── статистика (только для владельца) ──
OWNER_ID = int(os.environ.get("OWNER_ID", "170269038"))
STATS_KEY = os.environ.get("STATS_KEY", "")  # Optional; use a random secret, at least 32 characters.
if len(STATS_KEY) < 32:
    STATS_KEY = ""
TEST_USER_IDS = {"555", "777222", "777001", "777002", "777003", "777004"}


def reply_keyboard():
    """Постоянные кнопки внизу: только команды. Открывание аппы — через ☰
    (меню-кнопка web_app, открывает приложение сразу с initData)."""
    return {"keyboard": [
        [{"text": "💸 Создать сбор"},
         {"text": "🗂 Мои сборы"}],
        [{"text": "🔒 Закрыть сбор"},
         {"text": "✏️ Изменить сбор"}],
        [{"text": "🗑 Удалить сбор"},
         {"text": "❓ Как пользоваться"}],
    ], "resize_keyboard": True, "is_persistent": True,
        "input_field_placeholder": "Сумма — числом, например «350»"}


REPLY_ACTIONS = {
    "📱 Открыть в боте": "app",  # текст из СТАРОЙ клавиатуры (до обновления) — обрабатываем ниже
    "💸 Создать сбор": "create",
    "🗂 Мои сборы": "my",
    "🔒 Закрыть сбор": "close",
    "✏️ Изменить сбор": "edit",
    "🗑 Удалить сбор": "delete",
    "❓ Как пользоваться": "how",
}


def register_commands():
    if BOT_TOKEN:
        tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in BOT_COMMANDS])
        log.info("Меню команд обновлено: %s", ", ".join("/" + c for c, _ in BOT_COMMANDS))
        ok = tg("setChatMenuButton", menu_button={"type": "web_app", "text": "📱 Открыть", "web_app": {"url": WEBAPP_URL}})
        if ok:
            log.info("Кнопка меню — мини-апп: ☰ слева открывает приложение сразу (с initData)")
        else:
            log.warning("setChatMenuButton не сработал")


def start_telegram():
    global bot_username
    offline = os.environ.get("BOT_OFFLINE", "") == "1"  # тесты: токен есть (для валидации initData), но в Telegram не ходим
    if BOT_TOKEN:
        me = None if offline else tg("getMe")
        if me and me.get("username"):
            bot_username = me["username"]
        if not offline:
            register_commands()
            threading.Thread(target=polling_loop, daemon=True).start()
    else:
        log.info("BOT_TOKEN не задан — запускается только веб-часть (демо-режим)")


def main():
    load_state()

    httpd = HTTPServer(("0.0.0.0", PORT), Handler)
    # Bind first: slow Telegram calls must not leave BotHost's proxy without a listener.
    threading.Thread(target=start_telegram, daemon=True).start()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    log.info("Бот: http://localhost:%d/app  (DEV_MODE=%s)", PORT, DEV_MODE)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("пока 💀")


if __name__ == "__main__":
    main()
