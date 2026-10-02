"""Задания и очки сезона: начисление, сброс дня, постоянное задание, границы сезона по МСК."""
import asyncio
import os
import sys
import types
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for name in ("aiohttp", "sqlalchemy", "db", "models", "metrics"):
    try:
        __import__(name)
    except Exception:
        m = types.ModuleType(name); sys.modules[name] = m
        m.web = None; m.select = None; m.SessionLocal = None; m.SeasonPts = None
        m.inc = m.gauge = m.observe = lambda *a, **k: None

import seasonpts as sp  # noqa: E402


class SeasonPtsTest(unittest.TestCase):
    def setUp(self):
        sp._st.clear(); sp._dirty.clear()
        self.sent = []

        async def push(uid, msg):
            self.sent.append(msg)
        sp._push = push

        async def fake_load(uid):
            st = sp._st.get(uid)
            if not st:
                st = sp._st[uid] = {"season": sp.season_key(), "pts": 0, "data": {}}
            return st
        self._orig = sp._load
        sp._load = fake_load

    def tearDown(self):
        sp._load = self._orig

    def run_(self, coro):
        return asyncio.run(coro)

    def test_chat_task(self):
        for _ in range(9):
            self.run_(sp.add(1, "chat"))
        self.assertEqual(sp._st[1]["pts"], 0)
        self.run_(sp.add(1, "chat"))
        self.assertEqual(sp._st[1]["pts"], 30)
        self.run_(sp.add(1, "chat"))                    # повторно за день — не начисляется
        self.assertEqual(sp._st[1]["pts"], 30)
        self.assertIn("сообщений", self.sent[-1]["text"])

    def test_market_buy_daily_and_permanent(self):
        self.run_(sp.add(1, "mbuy", 1.5))
        self.assertEqual(sp._st[1]["pts"], 10)          # +10 за целый GRAM
        self.run_(sp.add(1, "mbuy", 1.6))               # всего 3,1 GRAM: ещё +20 постоянных и +100 дневных
        self.assertEqual(sp._st[1]["pts"], 130)

    def test_day_reset(self):
        for _ in range(10):
            self.run_(sp.add(1, "chat"))
        sp._st[1]["data"]["day"]["key"] = "2000-01-01"  # наступил новый день
        for _ in range(10):
            self.run_(sp.add(1, "chat"))
        self.assertEqual(sp._st[1]["pts"], 60)

    def test_kills_counters(self):
        self.assertEqual(sp.kill_counter("sector1"), "kill_f1")
        self.assertEqual(sp.kill_counter("iron_canyon"), "kill_farm")
        self.assertIsNone(sp.kill_counter("arena_fear"))
        self.run_(sp.add(1, "kill_f1", 99999))
        self.assertEqual(sp._st[1]["pts"], 0)
        self.run_(sp.add(1, "kill_f1", 1))
        self.assertEqual(sp._st[1]["pts"], 300)

    def test_soon_tasks_never_pay(self):
        self.run_(sp.add(1, "tower", 5))
        self.assertEqual(sp._st[1]["pts"], 0)

    def test_season_bounds_msk(self):
        self.assertEqual(sp.season_key(datetime(2026, 11, 1, 17, 59, tzinfo=sp.MSK)), "2026-10")
        self.assertEqual(sp.season_key(datetime(2026, 11, 1, 18, 0, tzinfo=sp.MSK)), "2026-11")
        self.assertEqual(sp.season_end("2026-10"), int(datetime(2026, 11, 1, 18, tzinfo=sp.MSK).timestamp()))
        self.assertEqual(sp.season_end("2026-12"), int(datetime(2027, 1, 1, 18, tzinfo=sp.MSK).timestamp()))


if __name__ == "__main__":
    unittest.main()
