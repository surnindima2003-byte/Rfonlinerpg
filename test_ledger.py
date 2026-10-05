"""Учёт ресурсов (ledger.py): совпадение формул с game.html, честная игра не выходит за приход, приход не теряется.

Запуск: python -m unittest test_ledger
"""
import math
import random
import re
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
        m = types.ModuleType("metrics"); m.inc = m.gauge = m.observe = lambda *a, **k: None
        sys.modules["metrics"] = m

import ledger  # noqa: E402

GAME = (ROOT / "game.html").read_text(encoding="utf-8")


class Contract(unittest.TestCase):
    """Формулы дропа и цены продажи — как в game.html. Поменяли там — поменяйте и в ledger.py."""

    def test_base_mobs(self):
        blk = GAME[GAME.index("const NPCS = {"):GAME.index("\n};", GAME.index("const NPCS = {"))]
        for mid, (lvl, scrap, loot) in ledger.NPCS.items():
            line = re.search(mid + r":\{[^\n]*", blk).group(0)
            self.assertIn(f"scrap:[{scrap[0]},{scrap[1]}]", line, mid)
            got = dict((k, float(v)) for k, v in re.findall(r"(\w+):(\.\d+|\d\.\d+)", line.split("loot:{")[1]))
            self.assertEqual(got, loot, mid)
            self.assertIn(f"NPCS.{mid}.lvl = {lvl};", GAME)

    def test_dungeon_and_boss(self):
        self.assertIn("scrap:[L + 1, 2*L + 4]", GAME)
        self.assertIn("[id, +(c*0.6).toFixed(3)]", GAME)
        self.assertIn("scrap:[m.scrap[0]*10, m.scrap[1]*10]", GAME)
        self.assertIn(f"BOSS_LOOT = {ledger.BOSS_LOOT};", GAME)
        self.assertIn(f"const SCRAP_CHANCE = {ledger.SCRAP_CHANCE:.2f};", GAME)
        self.assertIn('const [base, noun] = DG_BASE[(L - 1) % 4]', GAME)
        order = re.findall(r'\["(\w+)","[^"]+"\]', GAME[GAME.index("const DG_BASE = ["):GAME.index("\n", GAME.index("const DG_BASE = ["))])
        self.assertEqual(tuple(order), ledger.DG_BASE)

    def test_drop_chances(self):
        self.assertIn("const base = 0.13 + 0.0079*s;", GAME)
        self.assertIn("chance:P(+(base*Math.max(0.3, Math.min(1, c/0.4))).toFixed(4))", GAME)
        self.assertIn('if(lv >= 3 || L.cores) out.items.push({id:"cores", chance:P(+(0.02 + 0.001*s).toFixed(4))', GAME)
        self.assertIn("it.chance = Math.min(0.9, it.chance*BOSS_LOOT)", GAME)
        locs = GAME[GAME.index("const LOCS = {"):GAME.index("\n};", GAME.index("const LOCS = {"))]
        with_cores = set(re.findall(r"^\s*(\w+):\{[^\n]*\bcores:", locs, re.M))
        self.assertEqual(with_cores, ledger.LOC_CORES)

    def test_sell_price(self):
        self.assertIn("sell:Math.round(6*Math.pow(k, 2.2))", GAME)
        self.assertIn("const tierK = L => 1 + 0.12*L + 0.0022*L*L", GAME)
        self.assertIn('const sellPrice = x => Math.round(ITEMS[x.id].sell * (ITEMS[x.id].type === "gear" ? GRADES[x.g||0].sell*(1 + 0.5*(x.e || 0)) : 1));', GAME)
        tiers = dict((int(n), int(l)) for n, l in re.findall(r"\{n:(\d+), lvl:(\d+),", GAME))
        self.assertEqual(tiers, ledger.TIER_LVL)
        grades = [int(x) for x in re.findall(r'mult:[\d.]+, sell:(\d+)\}', GAME)]
        self.assertEqual(tuple(grades), ledger.GRADE_SELL)
        # пример: золотой (g=3) +2 поколения 5 (20 ур.)
        k = 1 + 0.12 * 20 + 0.0022 * 400
        self.assertEqual(ledger.gear_sell("g_rifle_5", 3, 2), round(round(6 * k ** 2.2) * 10 * 2))
        self.assertEqual(ledger.gear_sell("sph_cu"), 0)


def honest_session(mob, loc, kills, loot_bonus, vip_scrap, drop_bonus, rng):
    """Что честный игрок выбьет за kills убийств (как rollDrops в игре)."""
    lv, scrap, loot, boss = ledger.mob_info(mob)
    s = lv - 1
    got = {"scrap": 0, "cores": 0, "wire": 0, "plate": 0, "chip": 0}
    base = (0.13 + 0.0079 * s) / 100
    for _ in range(kills):
        if boss or rng.random() < ledger.SCRAP_CHANCE:
            got["scrap"] += round(rng.randint(scrap[0], scrap[1]) * (1 + loot_bonus + vip_scrap))
        for mid, c in loot.items():
            ch = base * max(0.3, min(1, c / 0.4))
            if boss:
                ch = min(0.9, ch * ledger.BOSS_LOOT)
            if rng.random() < ch * (1 + drop_bonus):
                got[mid] += 1
        if lv >= 3 or loc in ledger.LOC_CORES:
            ch = (0.02 + 0.001 * s) / 100
            if boss:
                ch = min(0.9, ch * ledger.BOSS_LOOT)
            if rng.random() < ch * (1 + drop_bonus):
                got["cores"] += 1
    return got


class HonestPlay(unittest.TestCase):
    """Честный игрок с максимальными бонусами не выходит за приход + запас. Иначе режим on резал бы честных."""

    def run_case(self, mob, loc, kills, sessions=400):
        rng = random.Random(42)
        vip_scrap, drop = 2.3, 2.3 + 0.6 + 0.5          # что знает сервер: максимальный VIP, билет и сезонная зона
        inc = ledger.kill_income(mob, loc, vip_scrap=vip_scrap, drop_bonus=drop)
        over = 0
        for _ in range(sessions):
            # у игрока ещё и максимальные бонусы, которых сервер не видит: снаряжение и улучшение «Бонус к дропу»
            got = honest_session(mob, loc, kills, ledger.LOOT_MAX, vip_scrap, drop + ledger.DROP_UP_MAX, rng)
            if any(got[k] > inc.get(k, 0) * kills + ledger.SLACK[k] for k in got):
                over += 1
        return over / sessions

    def test_field_mobs(self):
        for mob, loc in (("scrap_crawler", "scrapfields"), ("sentry_bot", "reactor_ruins"), ("war_walker", "iron_canyon")):
            for kills in (25, 150, 1500):                 # одно сохранение — от минуты до получаса фарма
                self.assertLessEqual(self.run_case(mob, loc, kills), 0.01, f"{mob} × {kills}")

    def test_dungeon_and_bosses(self):
        for mob, loc in (("dg10", "sector1"), ("dg25", "farm1"), ("dg40", "sector2"), ("db20", "sector1"), ("db40", "sector2")):
            for kills in ((5, 20) if mob.startswith("db") else (25, 150, 1500)):
                self.assertLessEqual(self.run_case(mob, loc, kills), 0.01, f"{mob} × {kills}")

    def test_much_tighter_than_old_rate(self):
        # старый потолок: 1200 лома в секунду. Новый за убийства (2,5 в с, сильнейший обычный моб, макс. бонусы):
        per_sec = ledger.kill_income("dg40", "sector2", vip_scrap=2.3)["scrap"] * 2.5
        self.assertLess(per_sec, 1200 / 2)


class Accumulator(unittest.TestCase):
    def setUp(self):
        ledger._acc.clear(); ledger.recent.clear(); ledger.MODE = "shadow"

    def test_over_is_reported_not_changed(self):
        ledger.add(7, {"scrap": 100})
        old, new = {"scrap": 0}, {"scrap": 50_000}
        notes = ledger.check(7, "x", old, new, {})
        self.assertEqual(notes[0][0], "scrap")
        self.assertEqual(new["scrap"], 50_000)            # shadow: ничего не меняет
        self.assertEqual(len(ledger.recent), 1)

    def test_income_after_check_survives_reset(self):
        ledger.add(8, {"scrap": 500})
        ledger.check(8, "x", {"scrap": 0}, {"scrap": 400}, {})
        ledger.add(8, {"scrap": 300})                     # убийство в фоне, пока сохранение пишется
        ledger.reset(8)
        self.assertAlmostEqual(ledger.peek(8)["scrap"], 300)

    def test_no_income_record_after_restart(self):
        self.assertEqual(ledger.check(9, "x", {"scrap": 0}, {"scrap": 10 ** 7}, {}), [])

    def test_materials_counted_in_inventory_and_storage(self):
        ledger.add(10, {"chip": 1})
        old = {"inv": [{"id": "chip", "n": 1}], "store": []}
        new = {"inv": [{"id": "chip", "n": 3}], "store": [{"id": "chip", "n": 5}]}
        notes = ledger.check(10, "x", old, new, {})
        self.assertEqual([n[0] for n in notes], ["chip"])


if __name__ == "__main__":
    unittest.main()
