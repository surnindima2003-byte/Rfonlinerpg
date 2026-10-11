"""Нагрузка: рейтинг игроков и гильдий не читает сохранения целиком и строится не чаще раза в TOP_TTL секунд;
при запуске игры сохранение загружается один раз.

Раньше одно открытие рейтинга читало 100 строк game_saves со всей колонкой data (до ~30 МБ).
Запуск: python -m unittest test_top_load
"""
import asyncio
import json
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")
TOP = WEB[WEB.index("TOP_TTL = "):WEB.index("# ---------- живой мир: WebSocket")]


class NoFullRows(unittest.TestCase):
    def test_only_needed_columns(self):
        # select(GameSave) / select(Doc) без колонок — это вся строка вместе с data
        self.assertNotIn("select(GameSave)", TOP)
        self.assertNotIn("select(Doc)", TOP)
        self.assertNotIn("GameSave.data", TOP)

    def test_route_still_wired(self):
        self.assertIn('app.router.add_post("/api/top", api_top)', WEB)


class Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Cache(unittest.TestCase):
    def ns(self):
        built = []
        ns = {"asyncio": asyncio, "time": time, "json": json, "SessionLocal": Session,
              "metrics": types.SimpleNamespace(inc=lambda *a, **k: None)}
        a = TOP.index("TOP_TTL = "); b = TOP.index("async def _top_players")
        c = TOP.index("async def _top_cached"); d = TOP.index("async def api_top")
        exec(TOP[a:b] + TOP[c:d], ns)

        async def build(s):
            built.append(1)
            await asyncio.sleep(0.01)
            return ["list"]
        return ns, built, build

    def test_concurrent_opens_build_once(self):
        ns, built, build = self.ns()

        async def go():
            return await asyncio.gather(*(ns["_top_cached"]("players", build) for _ in range(50)))
        res = asyncio.run(go())
        self.assertEqual(len(built), 1)
        self.assertTrue(all(r == ["list"] for r in res))

    def test_rebuilt_after_ttl(self):
        ns, built, build = self.ns()
        asyncio.run(ns["_top_cached"]("guilds", build))
        ns["_top_cache"]["guilds"] = (time.time() - ns["TOP_TTL"] - 1, ["old"])
        self.assertEqual(asyncio.run(ns["_top_cached"]("guilds", build)), ["list"])
        self.assertEqual(len(built), 2)


class OneLoadOnStart(unittest.TestCase):
    """При запуске игры сохранение загружается один раз (раньше метка тестового сервера просила его второй раз)."""
    def test_single_state_load(self):
        game = (ROOT / "game.html").read_text(encoding="utf-8")
        self.assertEqual(game.count('"/api/state/load"'), 1)
        on = game[game.index("function onLoaded(r)"):game.index("loadState();", game.index("function onLoaded(r)"))]
        self.assertIn("showEnvBadge(r.env)", on)
        self.assertIn("function showEnvBadge(env)", game)


if __name__ == "__main__":
    unittest.main()
