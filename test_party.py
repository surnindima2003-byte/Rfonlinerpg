"""Пати (handle_party в webserver.py) на настоящем коде, без aiohttp: встречные приглашения, выход, кик.

Раньше встречные приглашения (A позвал B, B позвал A, оба приняли) роняли обработчик с KeyError
и рвали соединение игрока. Запуск: python -m unittest test_party
"""
import asyncio
import time
import types
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parent / "webserver.py").read_text(encoding="utf-8")


def load():
    ns = {"time": time, "asyncio": asyncio,
          "metrics": types.SimpleNamespace(inc=lambda *a, **k: None),
          "pvpguard": types.SimpleNamespace(in_combat=lambda *a: False, heal=lambda *a: None, combat_left=lambda *a: 0)}
    a = SRC.index("PARTY_MAX = 4")
    b = SRC.index("CARD_NUM = {")
    exec(SRC[a:b], ns)
    online_ = {}
    sent = []

    async def push(uid, payload):
        sent.append((uid, payload))
        return True

    hub = types.SimpleNamespace(to_uid=lambda uid, p: sent.append((uid, p)), info_of=lambda uid: online_.get(uid),
                                by_uid={}, conns={})
    ns.update(hub=hub, push_to_player=push, online=lambda uid: online_.get(uid),
              realtime=types.SimpleNamespace(encode=lambda p: p))
    for uid in (1, 2, 3, 4, 5, 6):
        online_[uid] = {"id": uid, "nick": f"p{uid}", "lvl": 20, "cls": "", "loc": "lobby", "x": 0, "y": 0}
    return ns, online_, sent


class PartyTest(unittest.TestCase):
    def setUp(self):
        self.ns, self.on, self.sent = load()

    def act(self, t, uid, other):
        asyncio.run(self.ns["handle_party"]({"t": t, "to": other}, self.on[uid]))

    def test_cross_invites_do_not_crash(self):
        self.act("pinv", 1, 2)          # A зовёт B
        self.act("pinv", 2, 1)          # B зовёт A
        self.act("pacc", 1, 2)          # A принял приглашение B: пати {B, A}
        self.act("pacc", 2, 1)          # B принимает старое приглашение A — раньше KeyError
        mp, pt = self.ns["member_party"], self.ns["parties"]
        self.assertEqual(mp.get(1), mp.get(2))
        self.assertEqual(sorted(pt[mp[1]]["members"]), [1, 2])

    def test_inviter_lost_leadership(self):
        self.act("pinv", 1, 3)          # A зовёт C
        self.act("pinv", 2, 1)          # B зовёт A
        self.act("pacc", 1, 2)          # A вступил к B (лидер — B)
        self.act("pacc", 3, 1)          # C принимает приглашение A, который уже не лидер
        self.assertNotIn(3, self.ns["member_party"])

    def test_join_after_leaving_own_party(self):
        self.act("pinv", 1, 2); self.act("pacc", 2, 1)       # пати {1, 2}
        self.act("pinv", 3, 2)                               # 3 зовёт 2 (2 уже в пати — приглашение не уйдёт)
        self.act("pinv", 3, 4); self.act("pacc", 4, 3)       # пати {3, 4}
        self.act("pinv", 1, 5); self.act("pacc", 5, 1)       # {1, 2, 5}
        self.act("pleave", 2, 0)
        mp = self.ns["member_party"]
        self.assertNotIn(2, mp)
        self.assertEqual(sorted(self.ns["parties"][mp[1]]["members"]), [1, 5])

    def test_full_party(self):
        for u in (2, 3, 4):
            self.act("pinv", 1, u); self.act("pacc", u, 1)
        self.act("pinv", 1, 5)
        self.assertNotIn(5, self.ns["member_party"])
        self.assertEqual(len(self.ns["parties"][self.ns["member_party"][1]]["members"]), 4)

    def test_kick_stale_party(self):
        self.ns["member_party"][1] = "gone"                  # пати уже нет — кик не падает
        self.act("pkick", 1, 2)


if __name__ == "__main__":
    unittest.main()
