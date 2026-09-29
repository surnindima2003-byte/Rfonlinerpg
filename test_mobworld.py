"""Общие мобы: урон складывается от разных игроков, лут получает нанёсший больше урона, моб возрождается."""
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if "metrics" not in sys.modules:
    m = types.ModuleType("metrics"); m.inc = m.gauge = m.observe = lambda *a, **k: None
    sys.modules["metrics"] = m

import mobguard  # noqa: E402
import mobworld  # noqa: E402


class Conn:
    def __init__(self, uid):
        self.uid = uid


class Hub:
    def __init__(self):
        self.sent, self.by_loc = [], {"iron_canyon": {Conn(1), Conn(2)}}

    def to_loc(self, loc, p, skip_uid=None):
        self.sent.append(p)


class MobWorldTest(unittest.TestCase):
    def setUp(self):
        mobworld._mobs.clear(); mobworld._dirty.clear(); mobworld._own.clear(); mobworld._moved.clear()
        self.hub = Hub()
        self.a, self.b = {"id": 1, "loc": "iron_canyon"}, {"id": 2, "loc": "iron_canyon"}

    def hit(self, who, i, dmg, typ="sentry_bot", loc="iron_canyon"):
        mobworld.on_hits(who, {"loc": loc, "h": [[i, typ, dmg]]}, self.hub)

    def test_top_damage_wins_and_passes_mobguard(self):
        self.hit(self.b, 5, 40); self.hit(self.a, 5, 30)          # добил A, но больше урона у B
        die = [p for p in self.hub.sent if p["t"] == "mdie"][0]
        self.assertEqual(die["by"], 2)
        self.assertIsNone(mobguard.check_kill(2, die["iid"], "sentry_bot"))

    def test_hp_broadcast_and_state(self):
        self.hit(self.a, 5, 20); mobworld.tick(self.hub)
        self.assertEqual(self.hub.sent[-1], {"t": "mhp", "loc": "iron_canyon", "u": [[5, 35, 55]]})
        self.assertEqual(mobworld.state("iron_canyon")["hp"], [[5, 35, 55]])

    def test_dead_mob_ignores_hits_then_respawns(self):
        self.hit(self.a, 5, 100)
        self.hit(self.b, 5, 100)
        self.assertEqual(sum(p["t"] == "mdie" for p in self.hub.sent), 1)
        mobworld._mobs["iron_canyon"][5]["dead_until"] = time.time() - 1
        mobworld.tick(self.hub)
        self.assertEqual(self.hub.sent[-1]["t"], "mres")

    def test_other_location_ignored(self):
        self.hit(self.a, 5, 20, loc="sector1")
        self.assertNotIn("sector1", mobworld._mobs)

    def test_unknown_mob_ignored(self):
        self.hit(self.a, 5, 20, typ="dragon")
        self.assertEqual(self.hub.sent, [])


class OwnershipTest(MobWorldTest):
    def test_first_claim_wins_and_positions_relay(self):
        mobworld.on_claim(self.a, {"loc": "iron_canyon", "i": [3]}, self.hub)
        mobworld.on_claim(self.b, {"loc": "iron_canyon", "i": [3]}, self.hub)      # уже ведёт A
        owns = [p for p in self.hub.sent if p["t"] == "mown"]
        self.assertEqual([(p["i"], p["uid"]) for p in owns], [(3, 1)])
        mobworld.on_pos(self.b, {"loc": "iron_canyon", "m": [[3, 900, 900, 0, 0]]})  # чужого двигать нельзя
        mobworld.on_pos(self.a, {"loc": "iron_canyon", "m": [[3, 500, 600, 1.5, 2]]})
        mobworld.tick(self.hub)
        self.assertEqual(self.hub.sent[-1], {"t": "mmv", "loc": "iron_canyon", "m": [[3, 500.0, 600.0, 1.5, 2, 1]]})

    def test_stale_owner_released(self):
        mobworld.on_claim(self.a, {"loc": "iron_canyon", "i": [3]}, self.hub)
        mobworld._own["iron_canyon"][3]["t"] -= mobworld.OWNER_STALE + 1
        mobworld.tick(self.hub)
        self.assertIn({"t": "mown", "loc": "iron_canyon", "i": 3, "uid": 0}, self.hub.sent)
        mobworld.on_claim(self.b, {"loc": "iron_canyon", "i": [3]}, self.hub)
        self.assertEqual(self.hub.sent[-1]["uid"], 2)

    def test_death_clears_owner(self):
        mobworld.on_claim(self.a, {"loc": "iron_canyon", "i": [5]}, self.hub)
        self.hit(self.a, 5, 100)
        self.assertNotIn(5, mobworld._own.get("iron_canyon", {}))


if __name__ == "__main__":
    unittest.main()
