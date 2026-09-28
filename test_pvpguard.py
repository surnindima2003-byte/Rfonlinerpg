"""Серверный учёт CP и прочности в PvP и проверка скорости."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pvpguard  # noqa: E402

BM_CAP_46 = 3000 + 900 * 46 + 20 * 46 * 46     # как progress.bm_cap(46)


def player(uid=1, hp=1000, mhp=1000, cp=500, mcp=10000, df=20, lvl=46):
    info = {"id": uid, "hp": hp, "mhp": mhp, "cp": cp, "mcp": mcp, "df": df, "lvl": lvl, "x": 100.0, "y": 100.0, "loc": "iron_canyon"}
    pvpguard.sanitize(info, BM_CAP_46)
    return info


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class PvpGuardTest(unittest.TestCase):
    def setUp(self):
        pvpguard._state.clear()
        self.clock = Clock()
        self.p = mock.patch("pvpguard._now", self.clock)
        self.p.start()
        pvpguard.PVP_MODE = "on"
        pvpguard.MOVE_MODE = "on"

    def tearDown(self):
        self.p.stop()

    def test_caps(self):
        v = player(mhp=10_000_000, hp=10_000_000, cp=10**9, mcp=10**9, df=10**6)
        max_hp, _, max_def = pvpguard.caps(BM_CAP_46)
        self.assertEqual(v["mhp"], max_hp)
        self.assertLessEqual(v["cp"], v["mhp"] * 10)
        self.assertEqual(v["df_eff"], max_def)

    def test_cp_absorbs_first(self):
        v = player(cp=500, hp=1000, df=0)
        self.assertFalse(pvpguard.on_hit(v, 300))
        self.assertEqual((v["cp"], v["hp"]), (200, 1000))
        pvpguard.on_hit(v, 400)                      # 200 в CP, 200 × 0,55 в прочность
        self.assertEqual(v["cp"], 0)
        self.assertEqual(v["hp"], 890)

    def test_god_mode_client_dies_on_server(self):
        v = player(cp=0, hp=300, df=0)
        dead = False
        for _ in range(60):
            self.clock.t += 0.5
            v["hp"], v["cp"] = 300, 0                # изменённый телефон всё время шлёт полную прочность
            pvpguard.on_report(v)
            dead = pvpguard.on_hit(v, 100) or dead
        self.assertTrue(dead)

    def test_honest_regen_allowed(self):
        v = player(cp=0, hp=1000, df=0)
        pvpguard.on_hit(v, 1000)                     # -550 на сервере
        self.clock.t += 2
        v["hp"] = 600                                # телефон: 450 + ремкомплект 150
        pvpguard.on_report(v)
        self.assertEqual(v["hp"], 600)

    def test_combat_ends(self):
        v = player(cp=0, hp=1000, df=0)
        pvpguard.on_hit(v, 1000)
        self.clock.t += pvpguard.COMBAT_SEC + 1
        v["hp"] = 1000
        pvpguard.on_report(v)
        self.assertEqual(v["hp"], 1000)

    def test_move_guard(self):
        i = player()
        self.assertTrue(pvpguard.check_move(i, 100, 100, "iron_canyon", False))
        self.clock.t += 0.125
        self.assertTrue(pvpguard.check_move(i, 140, 100, "iron_canyon", False))     # обычный шаг
        self.assertFalse(pvpguard.check_move(i, 3000, 100, "iron_canyon", False))   # телепорт через карту
        self.assertTrue(pvpguard.check_move(i, 3000, 100, "lobby", False))          # смена локации — можно


if __name__ == "__main__":
    unittest.main()
