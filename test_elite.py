"""Элитная фарм-зона: правила зоны (elite.py), элитные мобы на сервере (mobguard, ledger, mobworld, seasonpts)
и их зеркало в game.html. Группы и вход лидера проверяет test_party.EliteGroupTest на настоящем коде webserver.py.

Запуск: python -m unittest test_elite
"""
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

import elite      # noqa: E402
import ledger     # noqa: E402
import mobguard   # noqa: E402

GAME = (ROOT / "game.html").read_text(encoding="utf-8")
ITEMS_SRC = (ROOT / "items.py").read_text(encoding="utf-8")


class ZoneRules(unittest.TestCase):
    def setUp(self):
        elite._used.clear(); elite._last.clear(); elite._runs.clear(); elite._open.clear()

    def test_minutes_are_counted_only_inside(self):
        elite.account(1, now=1000)
        for t in range(1001, 1061):
            elite.account(1, now=t)                     # минута в зоне
        self.assertAlmostEqual(elite.used(1), 60, delta=0.01)
        elite.account(1, now=1200)                      # обрыв связи на 140 с — не в счёт
        self.assertAlmostEqual(elite.used(1), 60, delta=0.01)
        elite.out_of_zone(1)
        elite.account(1, now=5000)
        self.assertAlmostEqual(elite.left(1), elite.LIMIT - 60, delta=0.01)

    def test_group_of_three_or_everybody_out(self):
        loc = elite.start("p1", [1, 2, 3], now=0)
        where = {1: loc, 2: loc, 3: loc}
        self.assertEqual(elite.tick(where.get, now=elite.START_GRACE + 1), [])
        where[3] = "lobby"                              # один вышел
        self.assertEqual(elite.tick(where.get, now=100), [])            # сначала — ожидание (переподключение)
        out = elite.tick(where.get, now=100 + elite.GRACE)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0][0], sorted(out[0][1])), (loc, [1, 2]))  # выводит всех, кто ещё в зоне
        self.assertIsNone(elite.run_of_loc(loc))                         # экземпляр свободен

    def test_short_absence_is_forgiven(self):
        loc = elite.start("p1", [1, 2, 3], now=0)
        where = {1: loc, 2: loc, 3: None}
        elite.tick(where.get, now=50)
        where[3] = loc                                  # вернулся за GRACE секунд
        self.assertEqual(elite.tick(where.get, now=50 + elite.GRACE - 1), [])
        self.assertEqual(elite.tick(where.get, now=50 + elite.GRACE + 5), [])
        self.assertIsNotNone(elite.run_of_loc(loc))

    def test_daily_limit_kicks_the_player(self):
        loc = elite.start("p1", [1, 2, 3], now=0)
        elite._used[1] = [elite._today(), elite.LIMIT]
        out = elite.tick({1: loc, 2: loc, 3: loc}.get, now=5)
        self.assertEqual(out[0][1], [1])
        self.assertIn("Лимит", out[0][2])

    def test_slots_and_entry_checks(self):
        locs = [elite.start(f"p{i}", [i], now=0) for i in range(elite.SLOTS)]
        self.assertEqual(sorted(locs), sorted(elite.LOCS))
        self.assertIsNone(elite.start("extra", [99], now=0))          # все экземпляры заняты
        self.assertTrue(elite.member_of(locs[0], 0))
        self.assertFalse(elite.member_of(locs[0], 5))
        infos = {1: {"nick": "a", "lvl": 30}, 2: {"nick": "b", "lvl": 24}, 3: {"nick": "c", "lvl": 30}}
        self.assertIn("25", elite.check_enter(1, [1, 2, 3], infos))
        infos[2]["lvl"] = 25
        self.assertIsNone(elite.check_enter(1, [1, 2, 3], infos))
        self.assertIn("3", elite.check_enter(1, [1, 2], infos))
        infos[3] = None
        self.assertIn("не в сети", elite.check_enter(1, [1, 2, 3], infos))


class ServerMobs(unittest.TestCase):
    def test_level_and_hp(self):
        self.assertEqual(mobguard.mob_level("el30", "elite2"), 30)
        self.assertIsNone(mobguard.mob_level("el30", "farm1"))         # элитные — только в элитной зоне
        self.assertIsNone(mobguard.mob_level("el40", "elite1"))
        self.assertIsNone(mobguard.mob_level("dg30", "elite1"))
        for lv in range(26, 37):
            self.assertEqual(mobguard.mob_hp(f"el{lv}"), int(mobguard.mob_hp(f"dg{lv}") * 2.5 + 0.5))

    def test_ledger(self):
        lv, scrap, loot, boss = ledger.mob_info("el30")
        dlv, dscrap, dloot, _ = ledger.mob_info("dg30")
        self.assertEqual((lv, boss, loot), (30, False, dloot))
        self.assertEqual(scrap, (dscrap[0] * 2, dscrap[1] * 2))
        a, b = ledger.kill_income("el30", "elite1"), ledger.kill_income("dg30", "sector2")
        self.assertGreater(a["scrap"], b["scrap"])
        self.assertAlmostEqual(a["cores"], min(1.0, b["cores"] * 2 / ledger.LEDGER_K) * ledger.LEDGER_K, places=6)

    def test_respawn_and_season(self):
        src = (ROOT / "mobworld.py").read_text(encoding="utf-8")
        self.assertIn('RESPAWN_ELITE if m["type"].startswith("el")', src)
        sp = (ROOT / "seasonpts.py").read_text(encoding="utf-8")
        self.assertIn('str(loc).startswith("elite")', sp)
        web = (ROOT / "webserver.py").read_text(encoding="utf-8")
        self.assertIn("SAFE_LOCS |= set(elite.LOCS)", web)                # в зоне группы PvP нет
        self.assertIn("not elite.member_of(d.get(\"loc\"), info[\"id\"])", web)   # чужая группа не войдёт
        self.assertIn("elite_tick()", web)


class GameMirror(unittest.TestCase):
    def test_constants(self):
        m = re.search(r"const ELITE = \{MIN:(\d+), SIZE:(\d+), LIMIT:(\d+), SLOTS:(\d+), LOOT:(\d+), ROOM_N:(\d+)", GAME)
        mn, size, limit, slots, loot, room_n = map(int, m.groups())
        self.assertEqual((mn, size, limit * 60, slots), (elite.MIN_LVL, elite.SIZE, elite.LIMIT, elite.SLOTS))
        self.assertEqual(loot, ledger.ELITE_LOOT)
        self.assertIn(f"ELITE_LOOT = {loot}", ITEMS_SRC)
        self.assertEqual(room_n, 50)
        rooms = re.findall(r"lo:(\d+), hi:(\d+), name:", GAME)
        self.assertEqual((int(rooms[0][0]), int(rooms[-1][1])), mobguard.ELITE_RANGE)
        self.assertEqual(mobguard.ELITE_RANGE, ledger.ELITE_RANGE)
        self.assertEqual(tuple(f"elite{i}" for i in range(1, slots + 1)), elite.LOCS)
        self.assertEqual(mobguard.ELITE_LOCS, elite.LOCS)

    def test_mob_formulas(self):
        self.assertIn("hp:Math.round(m.hp*2.5), atk:Math.round(m.atk*1.5), def:Math.round(m.def*1.5),", GAME)
        self.assertIn("speed:m.speed*2, exp:m.exp*3, scrap:[m.scrap[0]*2, m.scrap[1]*2]};", GAME)
        self.assertEqual(mobguard.ELITE_HP, 2.5)

    def test_shard_row_mirrors_server(self):
        base = re.search(r'SHARD_BASE = env_float\("SHARD_BASE_PCT", ([\d.]+)\)', ITEMS_SRC).group(1)
        step = re.search(r'SHARD_STEP = env_float\("SHARD_STEP_PCT", ([\d.]+)\)', ITEMS_SRC).group(1)
        self.assertIn(f"const ELITE_SHARD = [{base}, {step}];", GAME)

    def test_rules_in_window(self):
        for text in ("Вход с ${ELITE.MIN} уровня, только полной группой из ${ELITE.SIZE} человек",
                     "Войти в зону может только лидер группы — он затягивает за собой остальных участников",
                     "Если в зоне останется меньше ${ELITE.SIZE} человек группы, всех выводит из зоны",
                     "сами не нападают, но удар по одному будит всю пачку",
                     "Не более ${ELITE.LIMIT} минут в зоне на игрока в день"):
            self.assertIn(text, GAME)


if __name__ == "__main__":
    unittest.main()
