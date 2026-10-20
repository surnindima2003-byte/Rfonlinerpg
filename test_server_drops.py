"""Обычный дроп (материалы, ремкомплект, ядра, книги) бросает сервер: items.soft_table — зеркало mobDrops в игре.

Запуск: python -m unittest test_server_drops
"""
import json
import pathlib
import shutil
import subprocess
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from unittest import mock
for name in ("aiohttp", "sqlalchemy", "db", "models", "metrics", "db_atomic"):
    try:
        __import__(name)
    except Exception:
        sys.modules[name] = mock.MagicMock()
import items              # noqa: E402

MOBS = [("scrap_crawler", "scrapfields"), ("war_walker", "iron_canyon"), ("dg1", "sector1"), ("dg12", "sector1"),
        ("db20", "sector1"), ("el30", "elite1"), ("wm50", "world"), ("fw7", "arena_fear"), ("cw15", "coop1")]


@unittest.skipUnless(shutil.which("node"), "нет node")
class SoftDropMirror(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        head = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8")
        head = head[:head.index("\n", head.index("const run = "))]
        mobs = json.dumps(MOBS)
        check = r'''
const out = run(`(() => JSON.stringify({books:BOOK_IDS.map(b => [b, !!ITEMS[b].pas]),
  mobs:''' + mobs + r'''.map(([t, loc]) => [t, Object.fromEntries(mobDrops(t, loc).items.filter(i => !i.id.startsWith("sph_")).map(i => [i.id, i.chance]))])}))()`);
console.log(out);
'''
        res = subprocess.run(["node", "-", str(ROOT / "game.html")], input=head + check, capture_output=True, text=True, timeout=300)
        cls.r = json.loads(res.stdout.strip().splitlines()[-1])

    def test_books_match(self):
        self.assertEqual([b for b, p in self.r["books"] if not p], list(items.BOOKS_SKILL))
        self.assertEqual([b for b, p in self.r["books"] if p], list(items.BOOKS_PAS))

    def test_chances_match(self):
        for mob, game in self.r["mobs"]:
            srv = items.soft_table(mob, dict(MOBS)[mob])
            self.assertEqual(sorted(srv), sorted(game), mob)
            for k, v in game.items():
                self.assertAlmostEqual(srv[k], v, delta=max(1e-9, v * 1e-6), msg=f"{mob} {k}")

    def test_client_does_not_roll_online(self):
        game = (ROOT / "game.html").read_text(encoding="utf-8")
        self.assertIn("if(!srv) for(const it of D.items)", game)
        self.assertIn("if(!srv) for(const [pid, ch] of POT_DROP)", game)


if __name__ == "__main__":
    unittest.main()
