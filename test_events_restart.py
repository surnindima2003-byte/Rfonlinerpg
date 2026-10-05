"""События по расписанию: мировой босс после ручного вызова, Chip War после перезапуска, потолок шанса лута.

Запуск: python -m unittest test_events_restart
"""
import os
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if "metrics" not in sys.modules:
    m = types.ModuleType("metrics"); m.inc = m.gauge = m.observe = lambda *a, **k: None
    sys.modules["metrics"] = m

import chipwar     # noqa: E402
import worldboss   # noqa: E402

ROOT = Path(__file__).resolve().parent


class Hub:
    def __init__(self):
        self.all, self.loc, self.by_loc = [], [], {}

    def to_all(self, p, only=None):
        self.all.append(p)

    def to_loc(self, loc, p, skip_uid=None):
        self.loc.append(p)


class WorldBossSchedule(unittest.TestCase):
    def setUp(self):
        worldboss.st.update(active=False, hp=0, until=0, loot={}, loot_until=0, slot=None, done_slot=None, ended=0)
        self.hub = Hub()
        # понедельник 20:00 МСК
        self.slot = int(datetime(2026, 10, 5, 20, 0, tzinfo=worldboss.MSK).timestamp())

    def test_manual_kill_before_schedule_does_not_block_it(self):
        worldboss.start(manual=True)
        worldboss._ended(self.slot - 300)                       # админ убил босса в 19:55
        orig = worldboss.time.time
        worldboss.time.time = lambda: self.slot + 60
        try:
            worldboss.tick(self.hub)
        finally:
            worldboss.time.time = orig
        self.assertTrue(worldboss.st["active"])
        self.assertEqual(worldboss.st["slot"], self.slot)

    def test_scheduled_boss_not_restarted_in_same_window(self):
        worldboss.start(slot=self.slot)
        worldboss._ended(self.slot + 120)                       # убили в 20:02
        orig = worldboss.time.time
        worldboss.time.time = lambda: self.slot + 300
        try:
            worldboss.tick(self.hub)
        finally:
            worldboss.time.time = orig
        self.assertFalse(worldboss.st["active"])

    def test_loot_expiry_broadcast(self):
        worldboss.st.update(loot={"1": {"id": "1"}}, loot_until=0)
        worldboss.tick(self.hub)
        self.assertIn({"t": "wbloot", "items": []}, self.hub.loc)


class ChipWarResume(unittest.TestCase):
    def test_current_slot(self):
        sched = "tue 19:00"
        start = int(datetime(2026, 10, 6, 19, 0, tzinfo=worldboss.MSK).timestamp())      # вторник 19:00 МСК
        self.assertEqual(chipwar.current_slot(start + 300, sched, 3, 1200), start)
        self.assertIsNone(chipwar.current_slot(start + 1300, sched, 3, 1200))
        self.assertIsNone(chipwar.current_slot(start - 10, sched, 3, 1200))

    def test_slot_across_midnight(self):
        start = int(datetime(2026, 10, 6, 23, 50, tzinfo=worldboss.MSK).timestamp())
        self.assertEqual(chipwar.current_slot(start + 900, "tue 23:50", 3, 1200), start)


class LootCap(unittest.TestCase):
    def test_cap_applies_to_final_chance(self):
        src = (ROOT / "items.py").read_text(encoding="utf-8")
        self.assertIn("min(0.5, per * mult * len(pool))", src)
        self.assertNotIn("min(0.5, per * mult) * len(pool)", src)


if __name__ == "__main__":
    unittest.main()
