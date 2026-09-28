"""Проверка сохранений: честная игра проходит, подделка замечается."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if "metrics" not in sys.modules:
    try:
        import metrics  # noqa: F401
    except Exception:
        m = types.ModuleType("metrics"); m.inc = lambda *a, **k: None; m.gauge = lambda *a, **k: None
        sys.modules["metrics"] = m

import saveguard  # noqa: E402


def save(scrap=1000, cores=10, inv=None, eq=None):
    return {"S": {"scrap": scrap, "cores": cores, "exp": 5, "inv": inv or [{"id": "kit_s", "n": 5}],
                  "store": [], "eq": eq or {"weapon": {"id": "g_weapon_1", "g": 1, "e": 3}}}, "bm": 100}


class SaveGuardTest(unittest.TestCase):
    def setUp(self):
        saveguard.MODE = "on"
        saveguard.recent.clear()

    def test_honest_play_passes(self):
        data, notes = saveguard.check(1, "a", save(), save(scrap=4000, cores=14), 10, {})
        self.assertEqual(notes, [])
        self.assertIsNotNone(data)

    def test_grants_raise_limit(self):
        data, notes = saveguard.check(1, "a", save(), save(scrap=200_000), 10, {"scrap": 200_000})
        self.assertEqual(notes, [])

    def test_scrap_jump_rejected_in_on_mode(self):
        data, notes = saveguard.check(1, "a", save(), save(scrap=5_000_000), 10, {})
        self.assertIsNone(data)
        self.assertTrue(any(a == "скачок" for a, _ in notes))

    def test_shadow_mode_only_logs(self):
        saveguard.MODE = "shadow"
        data, notes = saveguard.check(1, "a", save(), save(scrap=5_000_000), 10, {})
        self.assertIsNotNone(data)
        self.assertTrue(notes)
        self.assertEqual(len(saveguard.recent), 1)

    def test_long_offline_allows_more(self):
        data, notes = saveguard.check(1, "a", save(), save(scrap=1_000_000), 3600, {})
        self.assertEqual(notes, [])

    def test_bad_structure_fixed(self):
        bad = save(eq={"weapon": {"id": "g_weapon_1", "g": 7, "e": 99}}, inv=[{"id": "kit_s", "n": -5}])
        data, notes = saveguard.check(1, "a", None, bad, 10, {})
        self.assertTrue(any(a == "структура" for a, _ in notes))
        w = data["S"]["eq"]["weapon"]
        self.assertEqual((w["e"], w["g"]), (15, 3))
        self.assertEqual(data["S"]["inv"][0]["n"], 0)

    def test_enchant_jump(self):
        data, notes = saveguard.check(1, "a", save(), save(eq={"weapon": {"id": "g_weapon_1", "g": 1, "e": 12}}), 10, {})
        self.assertIsNone(data)

    def test_first_save_without_old(self):
        data, notes = saveguard.check(1, "a", None, save(scrap=5_000_000), 10, {})
        self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main()
