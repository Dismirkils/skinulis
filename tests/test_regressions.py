"""Offline integration tests: no real users, Telegram messages or production state."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import http.client
import io
import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("skinulis_under_test", os.environ.get("SKINULIS_MODULE", str(ROOT / "bot.py")))
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
REAL_TG = bot.tg


def signed(user=101, **fields):
    data = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user, "first_name": "Person %s" % user})}
    data.update(fields)
    secret = hmac.new(b"WebAppData", bot.BOT_TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, "\n".join("%s=%s" % item for item in sorted(data.items())).encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


class Regressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bot.BOT_TOKEN = "123456:test-token"
        bot.DEV_MODE = False
        bot.bot_username = "test_bot"
        cls.server = getattr(bot, "HTTPServer", bot.ThreadingHTTPServer)(("127.0.0.1", 0), bot.Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bot.DATA_DIR = self.tmp.name
        bot.STATE_PATH = str(Path(self.tmp.name) / "state.json")
        bot.PUBLIC_DIR = str(ROOT / "public")
        bot.STATE = {k: {} for k in ("users", "collections", "payments", "nags", "hidden", "pendings")}
        bot.CARD_CACHE.clear()
        self.tg = patch.object(bot, "tg", Mock(return_value={"message_id": 11})).start()
        patch.object(bot.Handler, "log_message", lambda *args: None).start()
        for code in (101, 102, 103):
            bot.upsert_user({"id": code, "first_name": "Person %s" % code, "username": "person%s" % code})

    def tearDown(self):
        patch.stopall()
        self.tmp.cleanup()

    def req(self, path, body=None, user=101, method=None, raw=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        hs = {"Authorization": "tma " + signed(user)} if user is not None else {}
        hs.update(headers or {})
        data = raw if raw is not None else (json.dumps(body) if body is not None else None)
        conn.request(method or ("POST" if data is not None else "GET"), path, data, hs)
        r = conn.getresponse()
        content, status, rh = r.read(), r.status, dict(r.getheaders())
        conn.close()
        if "application/json" in rh.get("Content-Type", ""):
            content = json.loads(content)
        return status, content, rh

    def collection(self, **kwargs):
        return bot.create_collection("Pizza", None, "101", **kwargs)

    def action(self, c, action, body=None, user=101):
        return self.req("/api/c/%s/%s" % (c["id"], action), body if body is not None else {}, user=user)

    def message(self, text, user=101, chat=None):
        return {"message": {"from": {"id": user, "first_name": "Person %s" % user},
                            "chat": {"id": chat if chat is not None else user, "type": "private" if chat is None else "group"}, "text": text}}

    def test_authentication(self):
        self.assertEqual(bot.validate_init_data(signed())["id"], 101)
        self.assertEqual(bot.validate_init_data(signed(signature="sig"))["id"], 101)
        for stamp in ("0", "", "not-date", str(int(time.time()) - 86401), str(int(time.time()) + 600)):
            with self.subTest(stamp=stamp):
                self.assertIsNone(bot.validate_init_data(signed(auth_date=stamp)))
        self.assertIsNone(bot.validate_init_data(signed() + "&auth_date=0"))
        for user in (True, -1, "101", None):
            with self.subTest(user=user):
                self.assertIsNone(bot.validate_init_data(signed(user)))
        self.assertIsNone(bot.validate_init_data("user=x&hash=no"))

    def test_auth_required_all_routes(self):
        c = self.collection()
        for action in ("paid", "unpay", "join", "decline", "remove", "hint", "edit", "add", "leave", "close", "delete"):
            self.assertEqual(self.req("/api/c/%s/%s" % (c["id"], action), {}, user=None)[0], 401)
        for path in ("/api/my", "/api/c/" + c["id"]):
            self.assertEqual(self.req(path, user=None)[0], 401)

    def test_statistics_no_public_default_secret(self):
        self.assertEqual(self.req("/stats?key=skidulis-stats-2026", user=None)[0], 404)
        self.assertEqual(self.req("/stats?key=%D1%8F", user=None)[0], 404)
        self.assertEqual(self.req("/stats", user=102)[0], 404)
        self.assertEqual(self.req("/stats", user=bot.OWNER_ID)[0], 200)
        with patch.object(bot, "STATS_KEY", "a" * 32):
            self.assertEqual(self.req("/stats?key=" + "a" * 32, user=None)[0], 200)

    def test_corrupt_state_fails_closed(self):
        path = Path(bot.STATE_PATH)
        for content in ('{"users":', '{"users": [], "collections": {}}', '{"collections": {"x": {}}}'):
            with self.subTest(content=content):
                path.write_text(content)
                before = copy.deepcopy(bot.STATE)
                with self.assertRaises(RuntimeError):
                    bot.load_state()
                self.assertEqual(path.read_text(), content)
                self.assertEqual(bot.STATE, before)

    def test_save_failure_preserves_previous_file(self):
        bot.save_state()
        before = Path(bot.STATE_PATH).read_bytes()
        with patch.object(bot.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                bot.save_state()
        self.assertEqual(Path(bot.STATE_PATH).read_bytes(), before)
        self.assertFalse(list(Path(self.tmp.name).glob("*.tmp")))

    def test_concurrent_saves(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: bot.save(), range(40)))
        self.assertEqual(json.loads(Path(bot.STATE_PATH).read_text()), bot.STATE)

    def test_edit_is_atomic(self):
        c = self.collection(per=400)
        for fields in ({"title": "Changed", "per_person": -5}, {"title": "Changed", "deadline": "bad"},
                       {"title": "Changed", "per_person": 400, "goal_total": 900}):
            before = copy.deepcopy(c)
            self.assertEqual(self.action(c, "edit", fields)[0], 400)
            self.assertEqual(c, before)

    def test_edit_modes_hint_and_deadline(self):
        c = self.collection(per=400)
        status, data, _ = self.action(c, "edit", {"title": "Gift", "goal_total": 5000, "pay_hint": "SBP", "deadline": "2030-12-28 20:00 +0300"})
        self.assertEqual(status, 200)
        self.assertEqual(data["view"]["mode"], "total")
        self.assertIsNone(c["per_person"])
        self.assertEqual(c["pay_hint"], "SBP")
        self.assertEqual(self.action(c, "hint", {"hint": "Card"})[0], 200)

    def test_amounts_reject_coercion(self):
        for value in (True, False, 1.5, float("inf"), {}, [], "-5", "0", "1.5", "5abc", "10000001", 0, -1):
            with self.subTest(value=value):
                self.assertIsInstance(bot.valid_amount(value), str)
                self.assertEqual(self.req("/api/create", {"title": "T", "per_person": value})[0], 400)
        self.assertEqual(bot.valid_amount("400"), 400)
        self.assertEqual(bot.valid_amount(10000000), 10000000)

    def test_per_person_fixed_amount(self):
        c = self.collection(per=400)
        self.assertEqual(self.action(c, "paid", {"amount": 1}, user=102)[0], 400)
        self.assertEqual(self.action(c, "paid", user=102)[0], 200)
        self.assertEqual(bot.payments_of(c["id"])["102"]["amount"], 400)

    def test_malformed_http_requests(self):
        for data in ("[]", "null", "true", '"text"', "{", '{"title":12}', '{"pay_hint":[]}', '{"deadline":true}', '{"start_param":{}}'):
            with self.subTest(data=data):
                self.assertEqual(self.req("/api/create", raw=data)[0], 400)
        self.assertEqual(self.req("/api/create", raw="x" * 65537)[0], 400)
        self.assertEqual(self.req("/health", user=None)[0], 200)

    def test_creation_modes_and_validation(self):
        for fields, mode in (({"per_person": 400}, "per_person"), ({"goal_total": 5000}, "total"), ({}, "free")):
            status, data, _ = self.req("/api/create", dict(title="Gift", **fields))
            self.assertEqual(status, 200)
            self.assertEqual(data["view"]["mode"], mode)
            self.assertEqual(len(data["view"]["id"]), 34)
        for fields in ({"title": ""}, {"title": "x" * 65}, {"title": "T", "per_person": 5, "goal_total": 5}, {"title": "T", "deadline": "bad"}):
            self.assertEqual(self.req("/api/create", fields)[0], 400)

    def test_privileged_actions(self):
        c = self.collection(per=400)
        for action in ("remove", "hint", "close", "edit"):
            self.assertEqual(self.action(c, action, {"title": "hack", "code": "101"}, user=102)[0], 403)
        self.assertEqual(self.action(c, "add", {"codes": ["103"]})[0], 404)
        self.assertEqual(self.req("/api/users")[0], 404)
        self.assertEqual(self.req("/api/c/" + c["id"], user=102)[0], 403)
        self.assertEqual(self.req("/api/init", {"start_param": c["id"]}, user=102)[1]["collection"]["id"], c["id"])
        self.assertNotIn("102", c["joined"])

    def test_decline_then_pay_consistent(self):
        c = self.collection(per=400)
        self.assertEqual(self.action(c, "decline", user=102)[0], 200)
        self.assertIn(c["id"], [x["id"] for x in bot.my_items("102")])
        self.assertEqual(self.action(c, "paid", user=102)[0], 200)
        self.assertNotIn("102", c["declined"])
        self.assertEqual(bot.build_view(c["id"], "102")["me_state"], "paid")

    def test_leave_requires_explicit_rejoin(self):
        c = self.collection(per=400)
        self.action(c, "join", user=102)
        self.assertEqual(self.action(c, "leave", user=102)[0], 200)
        self.assertEqual(self.action(c, "paid", user=102)[0], 409)
        self.assertEqual(bot.chat_paid(c["id"], {"id": 102})[1] is not None, True)
        self.assertEqual(self.action(c, "join", user=102)[0], 200)
        self.assertNotIn("102", json.loads(Path(bot.STATE_PATH).read_text())["collections"][c["id"]]["left"])
        self.assertEqual(self.action(c, "paid", user=102)[0], 200)
        self.assertEqual(self.action(c, "leave", user=102)[0], 409)

    def test_unpay_and_remove(self):
        c = self.collection(per=400)
        self.action(c, "paid", user=102)
        self.assertEqual(self.action(c, "remove", {"code": "102"})[0], 409)
        self.assertEqual(self.action(c, "unpay", user=102)[0], 200)
        self.assertEqual(self.action(c, "remove", {"code": "102"})[0], 200)
        self.assertNotIn("102", c["joined"])
        self.assertEqual(self.action(c, "remove", {"code": "101"})[0], 400)

    def test_hide_and_delete(self):
        c = self.collection()
        self.action(c, "join", user=102)
        self.assertEqual(self.action(c, "delete", user=102)[1]["hidden"], True)
        self.assertEqual(bot.my_items("102"), [])
        self.assertIsNotNone(bot.get_collection(c["id"]))
        self.assertEqual(self.action(c, "delete")[1]["deleted"], True)
        self.assertIsNone(bot.get_collection(c["id"]))
        self.assertNotIn(c["id"], bot.STATE["hidden"]["102"])

    def test_anonymous_views_and_cards(self):
        c = self.collection(per=400, anon=True)
        c["joined"].append("102")
        bot.payments_of(c["id"])["102"] = {"amount": 400, "paid_ts": 1}
        view = bot.build_view(c["id"], "103")
        self.assertNotIn("person102", json.dumps(view))
        self.assertNotIn("Person 102", json.dumps(view))
        self.assertNotIn('"id": "102"', json.dumps(view))
        self.assertIn("Person 102", json.dumps(bot.build_view(c["id"], "101")))
        self.assertNotIn("Person 102", bot.card_html(c["id"]))
        self.assertNotIn("Person 102", bot.final_text(c["id"], c, 1, 400, ["102"]))

    def test_pending_amounts_strict_and_chat_bound(self):
        c = self.collection(goal=100000)
        bot.chat_paid(c["id"], {"id": 102})
        for text in ("-50", "12.50", "I paid 500", "1,5", "100000000"):
            bot.handle_update(self.message(text, user=102))
            self.assertNotIn("102", bot.payments_of(c["id"]))
        bot.handle_update(self.message("500", user=102, chat=-999))
        self.assertNotIn("102", bot.payments_of(c["id"]))
        bot.handle_update(self.message("1 500", user=102))
        self.assertEqual(bot.payments_of(c["id"])["102"]["amount"], 1500)
        bot.chat_paid(c["id"], {"id": 102})
        bot.handle_update(self.message("250", user=102))
        self.assertEqual(bot.payments_of(c["id"])["102"]["amount"], 1750)

    def test_pending_payment_after_leave(self):
        c = self.collection(goal=1000)
        self.action(c, "join", user=102)
        bot.chat_paid(c["id"], {"id": 102})
        self.action(c, "leave", user=102)
        bot.handle_update(self.message("100", user=102))
        self.assertNotIn("102", bot.payments_of(c["id"]))

    def test_pending_delete_snapshot(self):
        first = self.collection()
        second = self.collection()
        bot.handle_update(self.message("/delete"))
        bot.delete_collection(first["id"], "101")
        bot.handle_update(self.message("1"))
        self.assertIsNotNone(bot.get_collection(second["id"]))

    def test_chat_group_amount_flow(self):
        c = self.collection(goal=5000)
        callback = {"callback_query": {"id": "cb1", "from": {"id": 102}, "data": "p:" + c["id"], "message": {"chat": {"id": -999}}}}
        bot.handle_update(callback)
        bot.handle_update(self.message("350", user=102, chat=-999))
        self.assertEqual(bot.payments_of(c["id"])["102"]["amount"], 350)

    def test_commands_validation_and_addressing(self):
        for command in ("/skor", "/skor X по -5", "/skor X по 0", "/skor X по 10000001", "/skor X до 31.02", "/skor X по 5 на 10", "/skor " + "x" * 65, "/skor@other_bot X по 5"):
            bot.handle_update(self.message(command))
            self.assertEqual(bot.STATE["collections"], {}, command)
        bot.handle_update(self.message("/skor Gift по 400 анонимно"))
        c = next(iter(bot.STATE["collections"].values()))
        self.assertTrue(c["anonymous"])
        self.assertEqual(c["per_person"], 400)

    def test_remaining_commands_and_reply_buttons(self):
        c = self.collection(per=400)
        c["chat_id"] = 101
        for text in ("/start", "/help", "/create", "/list", "/edit", "/stat", "/unknown", "💸 Создать сбор", "🗂 Мои сборы", "❓ Как пользоваться"):
            bot.handle_update(self.message(text))
        self.assertTrue(self.tg.called)
        bot.handle_update(self.message("/close", user=102))
        self.assertEqual(c["status"], "active")
        bot.handle_update(self.message("/close"))
        self.assertEqual(c["status"], "closed")
        bot.handle_update(self.message("/delete 1"))
        self.assertIsNone(bot.get_collection(c["id"]))
        for text in ("-1", "0", "abc1", "1,0"):
            self.assertIsNone(bot.parse_nums(text))

    def test_all_callback_actions(self):
        c = self.collection(per=400)
        for act in ("d", "d", "p", "p", "j", "hint", "garbage"):
            bot.handle_update({"callback_query": {"id": act, "from": {"id": 102}, "data": act + ":" + c["id"], "message": {"chat": {"id": 102}}}})
        self.assertEqual(c["status"], "active")
        self.assertNotIn("102", bot.payments_of(c["id"]))
        self.assertNotIn("102", c["declined"])

    def test_cards_do_not_duplicate_on_transient_failure(self):
        c = self.collection()
        c["cards"] = {"101": 11}
        self.tg.return_value = None
        bot.update_card(c["id"])
        self.assertEqual([call.args[0] for call in self.tg.call_args_list], ["editMessageText"])
        self.assertFalse(bot.post_card(c["id"], 101))
        self.assertNotIn("101:11", bot.CARD_CACHE)

    def test_deletion_cleans_all_cards(self):
        c = self.collection()
        c["cards"] = {"101": 11, "102": 22, "-999": 33}
        bot.STATE["pendings"]["102"] = {"cid": c["id"]}
        bot.delete_collection(c["id"], "101")
        self.assertEqual(len([call for call in self.tg.call_args_list if call.args[0] == "deleteMessage"]), 3)
        self.assertNotIn("102", bot.STATE["pendings"])

    def test_finalize_goals_and_closed_routes(self):
        c = self.collection(goal=1000)
        self.action(c, "paid", {"amount": 400}, user=102)
        self.assertEqual(c["status"], "active")
        self.action(c, "paid", {"amount": 600}, user=103)
        self.assertEqual(c["status"], "closed")
        for action in ("join", "decline", "leave", "paid", "unpay", "edit", "remove"):
            self.assertEqual(self.action(c, action)[0], 409)

    def test_deadlines_scheduler_and_retry(self):
        c = self.collection(until="2000-01-01 20:00 +0300")
        bot.tick_collection(c["id"])
        self.assertEqual(c["status"], "closed")
        self.assertIsNone(bot.parse_deadline("invalid"))
        self.assertEqual(bot.parse_deadline("2030-01-01 12:00 +0300").utcoffset().total_seconds(), 10800)
        c = self.collection()
        self.tg.return_value = None
        bot.nag(c["id"], "102", "t24", "test")
        self.assertEqual(bot.STATE["nags"], {})
        self.tg.return_value = {"message_id": 99}
        bot.nag(c["id"], "102", "t24", "test")
        self.assertEqual(len(bot.STATE["nags"]), 1)

    def test_static_health_diagnostics(self):
        for path in ("/health", "/app", "/app/", "/app/telegram-web-app.js"):
            self.assertEqual(self.req(path, user=None)[0], 200)
        self.assertEqual(self.req("/", user=None)[0], 302)
        self.assertEqual(self.req("/app/../../bot.py", user=None)[0], 404)
        status, content, headers = self.req("/app", user=None, headers={"Accept-Encoding": "gzip"})
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        for path in ("/api/clientdiag", "/api/visit"):
            self.assertEqual(self.req(path, {"href": "SECRET", "hash": "SECRET"}, user=None)[0], 200)
        self.assertFalse(list(Path(self.tmp.name).glob("*.log")))

    def test_concurrent_join_pay_and_close(self):
        c = self.collection(goal=100000)
        def pay(user):
            self.action(c, "join", user=user)
            return self.action(c, "paid", {"amount": 100}, user=user)[0]
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(pay, range(201, 217))), [200] * 16)
        self.assertEqual(bot.total_collected(c["id"]), 1600)
        self.action(c, "close")
        self.assertEqual(self.action(c, "paid", {"amount": 100}, user=217)[0], 409)
        self.assertEqual(json.loads(Path(bot.STATE_PATH).read_text()), bot.STATE)

    def test_telegram_network_does_not_hold_state_lock(self):
        calls = []
        def response(*args, **kwargs):
            acquired = threading.Event()
            def take_lock():
                with bot.LOCK:
                    acquired.set()
            worker = threading.Thread(target=take_lock, daemon=True)
            worker.start()
            self.assertTrue(acquired.wait(1), "Telegram request holds the global state lock")
            calls.append(True)
            return io.BytesIO(b'{"ok":true,"result":{"message_id":1}}')
        with patch.object(bot, "tg", REAL_TG), patch.object(bot.urllib.request, "urlopen", response), patch.dict(os.environ, {"BOT_OFFLINE": "0"}):
            bot.handle_update(self.message("/start"))
        self.assertTrue(calls)

    def test_http_starts_before_slow_telegram(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        code = "import sys,time;sys.path.insert(0,sys.argv[1]);import bot;bot.start_telegram=lambda:time.sleep(10);bot.main()"
        process = subprocess.Popen([sys.executable, "-c", code, str(ROOT)], env=dict(os.environ,
            PORT=str(port), DATA_DIR=self.tmp.name, BOT_TOKEN="123:test", BOT_OFFLINE="1"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for attempt in range(30):
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.1)
                    conn.request("GET", "/health")
                    self.assertEqual(conn.getresponse().status, 200)
                    conn.close()
                    break
                except OSError:
                    time.sleep(0.03)
            else:
                self.fail("HTTP did not start while Telegram initialization was sleeping")
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
