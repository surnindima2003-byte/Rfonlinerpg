"""«Страх»: попытки и вход (fear.py), монстры fw1…fw59 на сервере (mobguard, ledger) и их зеркало в game.html.

Запуск: python -m unittest test_fear
"""
import json
import re
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
        m = types.ModuleType("metrics"); m.inc = m.gauge = m.observe = lambda *a, **k: None
        sys.modules["metrics"] = m

import fear  # noqa: E402
import ledger  # noqa: E402
import mobguard  # noqa: E402

GAME = (ROOT / "game.html").read_text(encoding="utf-8")


class Entry(unittest.TestCase):
    def setUp(self):
        fear._used.clear(); fear._inside.clear()

    def test_level_10(self):
        self.assertEqual(fear.enter(1, 9, now=1000), (False, "«Страх» открывается с 10 уровня"))
        self.assertTrue(fear.enter(1, 10, now=1000)[0])

    def test_two_tries_a_day(self):
        for t in (1000, 2000):
            self.assertTrue(fear.enter(1, 20, now=t)[0]); fear.left(1)
        ok, why = fear.enter(1, 20, now=3000)
        self.assertFalse(ok); self.assertIn("закончились", why)
        self.assertEqual(fear.used(1), 2)
        self.assertTrue(fear.enter(1, 20, admin=True, now=4000)[0])          # админ входит всегда

    def test_reconnect_is_not_a_new_try(self):
        self.assertTrue(fear.enter(1, 20, now=1000)[0])
        fear.seen_inside(1, now=1050)
        self.assertTrue(fear.enter(1, 20, now=1100)[0])                     # связь пропала и вернулась посреди забега
        self.assertEqual(fear.used(1), 1)
        self.assertTrue(fear.enter(1, 20, now=1100 + fear.RETURN_SEC + 1)[0])   # давно не было в подземелье — новая попытка
        self.assertEqual(fear.used(1), 2)

    def test_leaving_closes_the_return_window(self):
        self.assertTrue(fear.enter(1, 20, now=1000)[0])
        fear.left(1)
        self.assertTrue(fear.enter(1, 20, now=1010)[0])
        self.assertEqual(fear.used(1), 2)

    def test_new_day(self):
        fear._used[1] = ["2000-01-01", 2]
        self.assertEqual(fear.used(1), 0)
        self.assertTrue(fear.enter(1, 20, now=1000)[0])
        fear.cleanup(now=10**9)
        self.assertNotIn(1, fear._inside)


class ServerMobs(unittest.TestCase):
    def test_level_only_in_fear(self):
        self.assertEqual(mobguard.mob_level("fw12", "arena_fear"), 12)
        self.assertEqual(mobguard.mob_level("fw59", "arena_fear"), 59)
        self.assertIsNone(mobguard.mob_level("fw12", "sector1"))
        self.assertIsNone(mobguard.mob_level("fw60", "arena_fear"))
        self.assertIsNone(mobguard.mob_level("fw0", "arena_fear"))

    def test_hp_and_loot(self):
        self.assertEqual(mobguard.mob_hp("fw1"), 17)                        # round((20 + 14 + 0.25)·0,5)
        self.assertIsNone(mobguard.mob_hp("fw60"))
        lv, scrap, loot, boss = ledger.mob_info("fw3")                       # 3-я волна — шагоходы
        self.assertEqual((lv, scrap, boss), (3, (4, 10), False))
        self.assertEqual(loot, {"plate": 0.3, "chip": 0.18})
        self.assertIsNone(ledger.mob_info("fw60"))


class GameMirror(unittest.TestCase):
    """Числа в game.html и на сервере одни и те же."""

    def test_constants(self):
        fe = re.search(r"const FEAR = \{TRIES:(\d+), MIN:(\d+), WAVES:(\d+), PER:(\d+)", GAME)
        self.assertEqual(tuple(map(int, fe.groups()[:3])), (fear.TRIES, fear.MIN_LVL, mobguard.FEAR_WAVES))
        self.assertEqual(int(fe.group(4)), 20)
        self.assertIn('const FEAR_NOUN = [["scrap_crawler", "Краулер"], ["rogue_drone", "Дрон"], ["war_walker", "Шагоход"]];', GAME)
        self.assertEqual(ledger.FEAR_BASE, ("scrap_crawler", "rogue_drone", "war_walker"))

    def test_hp_formula(self):
        src = re.search(r"const fearHp = (w => [^;]+);", GAME).group(1)
        try:
            out = subprocess.run(["node", "-e", f"const f = {src}; process.stdout.write(JSON.stringify(Array.from({{length:59}}, (_, i) => f(i + 1))))"],
                                 capture_output=True, text=True, timeout=30, check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            raise unittest.SkipTest(f"нужен node: {e}")
        self.assertEqual(json.loads(out.stdout), [mobguard.mob_hp(f"fw{w}") for w in range(1, 60)])

    def test_no_teleport_into_fear(self):
        # в «Страх» — только кнопкой «Войти» в Событиях: ни портал, ни админский телепорт туда не ведут
        self.assertIn('Object.entries(LOCS).filter(([k, L]) => k !== "arena_fear" && !L.elite).map(([k, L]) => `<button type="button" data-adm="tp"', GAME)
        self.assertIn('if(key === "arena_fear" && locKey !== "arena_fear" && !(FEAR.pass', GAME)
        portal = GAME[GAME.index("function renderPortal(){"):GAME.index("\n}\n", GAME.index("function renderPortal(){"))]
        self.assertIn("L.arena", portal)


if __name__ == "__main__":
    unittest.main()
