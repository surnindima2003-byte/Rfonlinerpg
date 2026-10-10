"""Чат гильдии на настоящем коде webserver.py (handle_chat, apply_guild), без aiohttp и базы.

Раньше чата гильдии на сервере не было: сообщение оставалось на телефоне отправителя.
Теперь его получают только участники гильдии в сети, а гильдию сервер берёт из своих записей (info["gid"]).
Запуск: python -m unittest test_guild_chat
"""
import asyncio
import json
import logging
import time
import types
import unittest
from collections import deque
from pathlib import Path

SRC = (Path(__file__).resolve().parent / "webserver.py").read_text(encoding="utf-8")


def _slice(start, end):
    a = SRC.index(start)
    return SRC[a:SRC.index(end, a)]


class Conn:
    def __init__(self, info):
        self.info = info
        self.got = []

    @property
    def uid(self):
        return self.info["id"]

    def push(self, payload):
        self.got.append(payload)
        return True


def load(players):
    sent_uid, sent_all = [], []
    conns = {object(): Conn(p) for p in players}
    by_uid = {}
    for c in conns.values():
        by_uid.setdefault(c.uid, []).append(c)

    async def season_add(*a, **k):
        return None

    hub = types.SimpleNamespace(conns=conns, by_uid=by_uid,
                                to_uid=lambda uid, p: sent_uid.append((uid, p)) or True,
                                to_all=lambda p: sent_all.append(p))
    ns = {"time": time, "json": json, "deque": deque, "asyncio": asyncio, "log": logging.getLogger("t"),
          "hub": hub, "realtime": types.SimpleNamespace(encode=lambda p: p),
          "metrics": types.SimpleNamespace(inc=lambda *a, **k: None),
          "seasonpts": types.SimpleNamespace(add=season_add),
          "chat_history": deque(maxlen=60), "chat_seq": [0], "guild_history": {}, "GUILD_HISTORY": 50,
          "online": lambda uid: (by_uid.get(uid) or [None])[0] and by_uid[uid][0].info,
          "admin_ids": lambda: set(), "mod_ids": lambda: set()}
    exec(_slice("class Bucket:", "async def handle_pvp"), ns)
    exec(_slice("def apply_guild", "async def refresh_guild"), ns)
    return ns, {c.uid: c for c in conns.values()}, sent_uid, sent_all


def player(uid, gid):
    return {"id": uid, "nick": f"p{uid}", "fac": "aegis", "lvl": 20, "admin": False, "mod": False, "gid": gid}


class GuildChatTest(unittest.TestCase):
    def setUp(self):
        self.ns, self.c, self.sent_uid, self.sent_all = load(
            [player(1, "g1"), player(2, "g1"), player(3, "g2"), player(4, "")])

    def say(self, uid, text, ch="guild", **extra):
        asyncio.run(self.ns["handle_chat"]({"t": "chat", "ch": ch, "text": text, **extra}, self.c[uid].info))

    def chats(self, uid):
        return [p["m"] for p in self.c[uid].got if isinstance(p, dict) and p.get("t") == "chat"]

    def test_only_members_of_the_same_guild_receive(self):
        self.say(1, "сбор у портала")
        for uid in (1, 2):
            self.assertEqual([m["text"] for m in self.chats(uid)], ["сбор у портала"])
            self.assertEqual(self.chats(uid)[0]["ch"], "guild")
            self.assertEqual(self.chats(uid)[0]["g"], "g1")
        self.assertEqual(self.chats(3), [])
        self.assertEqual(self.chats(4), [])
        self.assertEqual(self.sent_all, [])                       # в мировой чат не утекает

    def test_guild_comes_from_server_not_from_message(self):
        self.say(1, "тайна", g="g2")                              # телефон «подставил» чужую гильдию
        self.assertEqual(self.chats(3), [])
        self.assertEqual(self.chats(2)[0]["g"], "g1")

    def test_not_in_guild_is_told_and_nothing_sent(self):
        self.say(4, "есть кто?")
        self.assertTrue(all(c.got == [] for c in self.c.values()))
        self.assertEqual(self.sent_uid[-1][0], 4)
        self.assertEqual(self.sent_uid[-1][1]["t"], "pinfo")

    def test_history_is_kept_and_capped(self):
        for i in range(60):
            self.ns["chat_limits"].clear()                        # частоту проверяет отдельный тест ниже
            self.say(1, f"m{i}")
        hist = self.ns["guild_history"]["g1"]
        self.assertEqual(len(hist), 50)
        self.assertEqual(hist[-1]["text"], "m59")
        self.assertNotIn("g2", self.ns["guild_history"])

    def test_rate_limit_applies_to_guild(self):
        for i in range(10):
            self.say(1, f"флуд {i}")
        self.assertLess(len(self.chats(2)), 10)

    def test_new_member_gets_recent_history_once(self):
        self.say(1, "привет новичкам")
        g = ("g1", "TAG", "Гильдия", "gear", "#F2A93B")
        self.ns["apply_guild"](4, g)
        hist = [p for p in self.c[4].got if p.get("t") == "chat_hist"]
        self.assertEqual(len(hist), 1)
        self.assertEqual([m["text"] for m in hist[0]["list"]], ["привет новичкам"])
        self.ns["apply_guild"](4, g)                              # повторное обновление той же гильдии — без повтора
        self.assertEqual(len([p for p in self.c[4].got if p.get("t") == "chat_hist"]), 1)
        self.assertEqual(self.c[4].info["gid"], "g1")
        self.say(1, "уже видишь?")
        self.assertEqual(self.chats(4)[-1]["text"], "уже видишь?")

    def test_left_guild_stops_receiving(self):
        self.ns["apply_guild"](2, None)
        self.say(1, "после выхода")
        self.assertEqual(self.chats(2), [])

    def test_world_chat_unchanged(self):
        self.say(1, "всем привет", ch="world")
        self.assertEqual(self.sent_all[-1]["m"]["text"], "всем привет")
        self.assertEqual(self.ns["guild_history"], {})


class ClientSendsGuildToServer(unittest.TestCase):
    def test_send_chat_no_longer_skips_guild(self):
        game = (Path(__file__).resolve().parent / "game.html").read_text(encoding="utf-8")
        fn = game[game.index("async function sendChat()"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertNotIn('chatCh !== "guild"', fn)                # раньше гильдия шла мимо сервера
        self.assertIn('NET.ws.send(JSON.stringify({t:"chat"', fn)
        self.assertIn('d.t === "chat_hist"', game)


if __name__ == "__main__":
    unittest.main()
