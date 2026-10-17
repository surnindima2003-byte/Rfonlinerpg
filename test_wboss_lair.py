"""Мировой босс в своей локации «Логово Владыки» (wboss): PvP, вход, пока босс жив, возврат после гибели.

Запуск: python -m unittest test_wboss_lair
"""
import asyncio
import re
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
if "metrics" not in sys.modules:
    m = types.ModuleType("metrics"); m.inc = m.gauge = m.observe = lambda *a, **k: None
    sys.modules["metrics"] = m

import worldboss  # noqa: E402

GAME = (ROOT / "game.html").read_text(encoding="utf-8")
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")


class Hub:
    def __init__(self):
        self.all, self.loc, self.uid, self.by_loc = [], [], [], {}

    def to_all(self, p, only=None):
        self.all.append(p)

    def to_loc(self, loc, p, skip_uid=None):
        self.loc.append((loc, p))

    def to_uid(self, uid, p):
        self.uid.append((uid, p))


class Season:
    async def add(self, uid, key, n):
        pass


class Lair(unittest.TestCase):
    def setUp(self):
        worldboss.st.update(active=False, hp=0, until=0, hits={}, loot={}, loot_until=0, slot=None, done_slot=None, dirty=False)
        worldboss._last_hit.clear()

    def test_own_location_matches_game(self):
        self.assertEqual(worldboss.LOC, "wboss")
        boss = re.search(r"boss:\{x:(\d+), y:(\d+)\}", GAME[GAME.index("const WBL = {"):])
        self.assertEqual((int(boss.group(1)), int(boss.group(2))), (worldboss.X, worldboss.Y))   # босс там же, где его рисует игра
        self.assertIn('const WB_LOC = "wboss";', GAME)

    def test_open_only_while_alive_or_loot(self):
        self.assertFalse(worldboss.open_now())
        worldboss.start(manual=True)
        self.assertTrue(worldboss.open_now())
        self.assertEqual(worldboss.view()["loot"], 0)
        worldboss._ended(0)
        self.assertFalse(worldboss.open_now())
        worldboss.st["loot"] = {"1": {"id": "1", "kind": "cores", "n": 1, "g": 0, "x": worldboss.X, "y": worldboss.Y}}
        self.assertTrue(worldboss.open_now())                     # добыча ещё лежит — в логово пускают
        self.assertEqual(worldboss.view()["loot"], 1)
        hub = Hub()
        worldboss.on_pick({"id": 7, "loc": "wboss", "x": worldboss.X, "y": worldboss.Y}, {"id": "1"}, hub)
        self.assertFalse(worldboss.open_now())                    # всё разобрали — логово закрывается
        self.assertTrue(worldboss.st["dirty"])

    def test_hits_count_only_in_the_lair(self):
        worldboss.start(manual=True)
        hp = worldboss.st["hp"]
        near = {"id": 1, "x": worldboss.X, "y": worldboss.Y + 200, "lvl": 30, "dead": False}
        asyncio.run(worldboss.on_hit({**near, "loc": "lobby"}, {"dmg": 500}, Hub(), Season()))
        self.assertEqual(worldboss.st["hp"], hp)                  # из ангара босса больше не достать
        asyncio.run(worldboss.on_hit({**near, "loc": "wboss"}, {"dmg": 500}, Hub(), Season()))
        self.assertEqual(worldboss.st["hp"], hp - 500)

    def test_aoe_targets_players_in_the_lair(self):
        worldboss.start(manual=True)
        worldboss.st["aoe_t"] = 0
        c = types.SimpleNamespace(info={"x": worldboss.X, "y": worldboss.Y + 150})
        hub = Hub(); hub.by_loc = {"wboss": [c]}
        worldboss.tick(hub)
        self.assertTrue(any(loc == "wboss" and p["t"] == "wbaoe" for loc, p in hub.loc))


class ServerRules(unittest.TestCase):
    def test_pvp_and_entry(self):
        locs = re.search(r"^LOCS = \{([^}]+)\}", WEB, re.M).group(1)
        safe = re.search(r"^SAFE_LOCS = \{([^}]+)\}", WEB, re.M).group(1)
        self.assertIn('"wboss"', locs)
        self.assertNotIn('"wboss"', safe)                         # в логове PvP
        self.assertIn("not worldboss.open_now()", WEB)           # без босса и добычи в логово не пускают
        self.assertIn('"Мировой босс сейчас не в логове"', WEB)


class GameRules(unittest.TestCase):
    def test_lair_and_return(self):
        self.assertIn("LOCS[WB_LOC] = {name:\"Логово Владыки\", min:1, hidden:true, noLoss:true", GAME)
        self.assertIn("L.arena || L.noLoss ||", GAME)             # ремонт после гибели в логове бесплатный
        self.assertIn("if(key === WB_LOC && locKey !== WB_LOC && !wbOpen() && !window.NET_ADMIN)", GAME)
        self.assertIn('wbBack.innerHTML = `<b>⚔ К боссу</b>', GAME)   # вернуться к боссу из ангара — одна кнопка
        self.assertIn("const wbOn = () => !!(WB.st && WB.st.active && locKey === WB_LOC);", GAME)
        portal = GAME[GAME.index("const _rpWB = renderPortal;"):]
        self.assertIn("data-go=\"${WB_LOC}\"", portal[:800])


if __name__ == "__main__":
    unittest.main()
