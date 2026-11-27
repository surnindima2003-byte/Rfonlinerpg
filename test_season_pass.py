"""Пропуск сезона через сервер: опыт копит сервер, награды забираются через /api/season/pass.

Раньше опыт пропуска и награды (до 6 000 лома за уровень, ядра, сферы) считал только телефон — их можно было
подделать, а учёт ресурсов (RES_LEDGER=on) срезал бы их честным игрокам. Проверяется:
* таблицы seasonNeed / seasonReward в игре и на сервере совпадают;
* награду нельзя забрать раньше уровня, дважды и премиум без билета; приход уходит в учёт ресурсов;
* опыт копится за подтверждённые сервером убийства и удачную заточку; прогресс текущего сезона
  переносится из сохранения один раз.
Запуск: python -m unittest test_season_pass
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
for name in ("aiohttp", "sqlalchemy", "db", "models", "metrics", "gram", "config"):
    try:
        __import__(name)
    except Exception:
        m = types.ModuleType(name); sys.modules[name] = m
        m.web = None; m.select = None; m.SessionLocal = None; m.SeasonPts = m.GameSave = m.Meta = m.GramTx = None; m.GRAM_USD = 1.55
        m.inc = m.gauge = m.observe = lambda *a, **k: None
        m.env_int = lambda k, d: d; m.env_float = lambda k, d: d

import seasonpts as sp  # noqa: E402

GAME = (ROOT / "game.html").read_text(encoding="utf-8")
ITEMS = (ROOT / "items.py").read_text(encoding="utf-8")


def run(c):
    return asyncio.run(c)


class Mirror(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "нет node")
    def test_tables_match_game(self):
        a = GAME.index("const SEASON_LV = ")
        b = GAME.index("function seasonReward(lv, prem){")
        b = GAME.index("\n}\n", b) + 3
        js = GAME[a:b].replace("function seasonState", "function _x").replace("function seasonAddXp", "function _y")
        js = js.split("const seasonId")[0] + "\n" + GAME[GAME.index("function seasonReward(lv, prem){"):b]
        js += "\nprocess.stdout.write(JSON.stringify({lv:SEASON_LV, max:SEASON_MAX, need:[...Array(30)].map((_, i) => seasonNeed(i + 1))," \
              " rew:[...Array(30)].map((_, i) => [seasonReward(i + 1, false), seasonReward(i + 1, true)])}));"
        out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out["lv"], sp.PASS_LV)
        self.assertEqual(out["max"], sp.PASS_MAX)
        self.assertEqual(out["need"], [sp.pass_need(i) for i in range(1, 31)])
        for lv, (f, p) in enumerate(out["rew"], 1):
            for game, prem in ((f, False), (p, True)):
                self.assertEqual(len(game), 1)
                g = game[0]
                self.assertEqual(sp.pass_reward(lv, prem), {g["id"]: g["n"]}, (lv, prem))

    def test_xp_sources_match_game(self):
        self.assertIn("seasonAddXp(k.boss ? 20 : 1)", GAME)
        self.assertIn("seasonAddXp(5)", GAME)
        self.assertEqual((sp.PASS_KILL, sp.PASS_BOSS, sp.PASS_ENCH), (1, 20, 5))
        self.assertIn("await seasonpts.pass_add(uid, seasonpts.PASS_BOSS if boss else seasonpts.PASS_KILL)", ITEMS)
        self.assertIn("await seasonpts.pass_add(uid, seasonpts.PASS_ENCH)", ITEMS)

    def test_client_asks_server(self):
        self.assertIn('NET.api("/api/season/pass", {lv:i, prem:prem ? 1 : 0})', GAME)
        self.assertIn("seasonClaim(key, +lv, b)", GAME)
        self.assertIn("sptLoad(); passSync();", GAME)


class Claim(unittest.TestCase):
    def setUp(self):
        sp._st.clear(); sp._dirty.clear()
        self.led, self.grants = [], []
        import ledger
        import saveguard
        self._orig = (ledger.add, saveguard.note_grant, sp._load)
        ledger.add = lambda uid, inc: self.led.append(inc)
        saveguard.note_grant = lambda uid, g: self.grants.append(g)

        async def fake_load(uid):
            return sp._st.setdefault(uid, {"season": sp.season_key(), "pts": 0, "data": {}})
        sp._load = fake_load
        sp._st[1] = {"season": sp.season_key(), "pts": 0, "data": {"pass": {"xp": 0, "f": [], "p": []}}}

    def tearDown(self):
        import ledger
        import saveguard
        ledger.add, saveguard.note_grant, sp._load = self._orig

    def test_levels(self):
        self.assertEqual(sp.pass_level(0), 0)
        self.assertEqual(sp.pass_level(299), 0)
        self.assertEqual(sp.pass_level(300), 1)
        self.assertEqual(sp.pass_level(700), 2)
        self.assertEqual(sp.pass_level(sp.PASS_MAX), 30)

    def test_xp_from_kills_capped(self):
        run(sp.pass_add(1, 299))
        self.assertEqual(sp.pass_level(sp._st[1]["data"]["pass"]["xp"]), 0)
        run(sp.pass_add(1, 1))
        self.assertEqual(sp._st[1]["data"]["pass"]["xp"], 300)
        run(sp.pass_add(1, 10 ** 9))
        self.assertEqual(sp._st[1]["data"]["pass"]["xp"], sp.PASS_MAX)
        self.assertIn(1, sp._dirty)

    def test_claim_rules(self):
        st = sp._st[1]
        self.assertEqual(sp.pass_claim(1, st, 1, False, False), "Награда откроется на 1 уровне пропуска")
        st["data"]["pass"]["xp"] = 300 + 400                         # 2 уровень
        self.assertIsNone(sp.pass_claim(1, st, 1, False, False))     # 1 ур.: 200 лома
        self.assertEqual(self.led[-1], {"scrap": 200})
        self.assertEqual(self.grants[-1], {"scrap": 200, "sph": 0})
        self.assertEqual(sp.pass_claim(1, st, 1, False, False), "Награда уже получена")
        self.assertEqual(sp.pass_claim(1, st, 1, True, False), "Премиум-награды — только с билетом сезона")
        self.assertIsNone(sp.pass_claim(1, st, 1, True, True))       # премиум 1 ур.: 11 ядер
        self.assertEqual(self.led[-1], {"cores": 11})
        self.assertEqual(sp.pass_claim(1, st, 0, False, True), "Нет такой награды")
        self.assertEqual(sp.pass_claim(1, st, 31, False, True), "Нет такой награды")
        self.assertEqual(st["data"]["pass"], {"xp": 700, "f": [1], "p": [1]})

    def test_spheres_go_to_save_check(self):
        st = sp._st[1]
        st["data"]["pass"]["xp"] = sp.PASS_MAX
        self.assertIsNone(sp.pass_claim(1, st, 10, False, False))    # 10 ур.: 2 медные сферы
        self.assertEqual(self.grants[-1], {"sph": 2})
        self.assertIsNone(sp.pass_claim(1, st, 5, True, True))       # премиум 5 ур.: титановая
        self.assertEqual(self.grants[-1], {"sph": 1})

    def test_same_season(self):
        self.assertTrue(sp._same_season("2026-1", "2026-01"))
        self.assertTrue(sp._same_season("2026-11", "2026-11"))
        self.assertFalse(sp._same_season("2026-10", "2026-11"))
        self.assertFalse(sp._same_season(None, "2026-11"))


if __name__ == "__main__":
    unittest.main()
