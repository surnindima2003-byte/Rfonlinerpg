"""Пределы прочности, CP и брони в PvP — по настоящей формуле игры и купленным очкам навыка.

Раньше пределы шли от предела боевой мощи: на 30 ур. сервер разрешал 32 000 прочности и 320 000 CP (больше любого
честного игрока без купленных очков), а на 1 ур. — 2 613 прочности и 392 брони (меньше, чем у честного игрока с
кодексом). Очки улучшения «CP» сервер не учитывал вовсе.

Запуск: python -m unittest test_pvp_caps
Сверка таблиц с game.html запускает tools/stat_caps.js в node (если node нет — эта проверка пропускается).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
if "metrics" not in sys.modules:
    try:
        import metrics  # noqa: F401
    except Exception:
        m = types.ModuleType("metrics"); m.inc = lambda *a, **k: None; m.gauge = lambda *a, **k: None
        sys.modules["metrics"] = m
import pvpguard  # noqa: E402

WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")
GRAM = (ROOT / "gram.py").read_text(encoding="utf-8")


class TablesMatchGame(unittest.TestCase):
    def test_same_as_stat_in_game(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("нет node")
        out = subprocess.run([node, str(ROOT / "tools" / "stat_caps.js"), str(ROOT / "game.html")],
                             capture_output=True, text=True, timeout=600)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        d = json.loads(out.stdout.strip().splitlines()[-1])
        msg = "баланс в game.html изменился: перезапусти node tools/stat_caps.js и перенеси числа в pvpguard.py"
        self.assertEqual([list(x) for x in pvpguard.HP_AB], d["hp"], msg)
        self.assertEqual([list(x) for x in pvpguard.DEF_AB], d["def"], msg)
        self.assertEqual(pvpguard.PACK_PTS, d["packPts"], msg)
        self.assertEqual(list(pvpguard.GEM_K), d["gemK"], msg)          # множители бонуса самоцветов (прочность, броня)
        self.assertEqual((pvpguard.UP_MAX, pvpguard.CP_PER_POINT, pvpguard.PTS_PER_LEVEL), (d["upMax"], d["cpPer"], d["ptsPerLevel"]))


class Caps(unittest.TestCase):
    def tearDown(self):
        pvpguard._pts.clear()

    def test_tighter_where_old_was_too_wide(self):
        hp, cp, df = pvpguard.caps(30, 0)
        self.assertLess(hp, 32_000)
        # CP от прочности (×10) уже старого предела. Улучшение «CP» старый предел не учитывал вовсе, а у Стража
        # с лучшим снаряжением (множитель прочности класса действует на итог) вместе с ним выходит чуть больше 320 000
        self.assertLess(cp - pvpguard.CP_PER_POINT * pvpguard.points(30, 0), 320_000)
        self.assertLess(df, 4_800)

    def test_wider_where_old_cut_honest_players(self):
        hp, _, df = pvpguard.caps(1, 0)
        self.assertGreater(hp, 2_613)           # кодекс и снаряжение — честные
        self.assertGreater(df, 392)

    def test_bought_points_raise_cap(self):
        base = pvpguard.caps(30, 0)
        more = pvpguard.caps(30, 40)
        self.assertGreater(more[0], base[0])
        self.assertEqual(more[1] - more[0] * 10, pvpguard.CP_PER_POINT * min(300, 87 + 40))   # улучшение «CP» учтено
        self.assertEqual(pvpguard.caps(30, 10_000), pvpguard.caps(30, None))           # не больше 300 очков

    def test_points(self):
        self.assertEqual(pvpguard.points(1, 0), 0)
        self.assertEqual(pvpguard.points(30, 0), 87)
        self.assertEqual(pvpguard.points(30, None), 300)        # купленные ещё не загружены — по максимуму
        self.assertEqual(pvpguard.points(999, 0), 300)
        self.assertEqual(pvpguard.caps(999, 0)[0], pvpguard.caps(50, 300)[0])

    def test_sanitize_uses_server_points(self):
        pvpguard.set_points(7, 0)
        info = {"id": 7, "mhp": 10**7, "hp": 10**7, "mcp": 10**9, "cp": 10**9, "df": 10**6}
        pvpguard.sanitize(info, 30)
        hp, cp, df = pvpguard.caps(30, 0)
        self.assertEqual((info["mhp"], info["mcp"], info["df_eff"]), (hp, cp, df))
        pvpguard.add_pack(7, "p_epic")                          # +20 очков сразу после покупки
        info.update(mhp=10**7, mcp=10**9)
        pvpguard.sanitize(info, 30)
        self.assertEqual(info["mhp"], pvpguard.caps(30, 20)[0])

    def test_honest_numbers_untouched(self):
        pvpguard.set_points(8, 0)
        info = {"id": 8, "mhp": 9000, "hp": 9000, "mcp": 90000 + 200 * 50, "cp": 1000, "df": 900}
        pvpguard.sanitize(info, 30)
        self.assertEqual((info["mhp"], info["mcp"], info["df_eff"]), (9000, 100000, 900))

    def test_admin_untouched(self):
        info = {"id": 9, "admin": True, "mhp": 10**6, "mcp": 10**7, "hp": 1, "cp": 1}
        pvpguard.sanitize(info, 1)
        self.assertEqual(info["mhp"], 10**6)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class BoughtPoints(unittest.TestCase):
    def test_counts_packs_from_shop_journal(self):
        ns = {"select": lambda *a: types.SimpleNamespace(where=lambda *w: None),
              "GramTx": types.SimpleNamespace(ref=0, tg_id=0, kind=0)}
        a = GRAM.index("async def bought_points"); b = GRAM.index("async def _spend")
        exec(GRAM[a:b], ns)

        class S:
            async def execute(self, q):
                return Result(["shop:5:p_epic:1", "shop:5:p_std:2", "shop:5:p_std:3", "shop:5:dr_sol:4", "bad"])
        self.assertEqual(asyncio.run(ns["bought_points"](S(), 5)), 22)

    def test_wired(self):
        self.assertIn('pvpguard.sanitize(info, info.get("lvl", 1))', WEB)
        self.assertNotIn("progress.bm_cap(info", WEB)
        self.assertIn('pvpguard.set_points(user["id"], await gram.bought_points(s_, user["id"]))', WEB)
        spend = GRAM[GRAM.index("async def _spend"):GRAM.index("# ---------- API игрока")]
        self.assertIn("pvpguard.add_pack(uid, pack)", spend)
        self.assertLess(spend.index("await s.commit()"), spend.index("pvpguard.add_pack("))


if __name__ == "__main__":
    unittest.main()
