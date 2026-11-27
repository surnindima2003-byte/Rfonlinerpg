"""Локации: сервер пускает в локацию только с её уровня (как портал в игре) и возвращает телефон при отказе.

Раньше сервер принимал любую присланную локацию — подделанный телефон 1-го уровня мог зайти в Мир (35+),
а при отказе в сезонную зону (нет билета) телефон об отказе не узнавал и «стоял» в зоне, где сервер его не видит.
Запуск: python -m unittest test_locations
"""
import ast
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")


def const(name):
    for node in ast.parse(WEB).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise KeyError(name)


class Server(unittest.TestCase):
    def test_gate_in_pos_handler(self):
        fn = WEB[WEB.index("async def handle_ws_message"):WEB.index('elif t == "elite":')]
        self.assertIn("if nl in LOC_MIN_LVL and nl != old_loc and not info.get(\"admin\"):", fn)
        self.assertIn("cap = progress.cached_cap(info[\"id\"]) or info.get(\"lvl_cap\") or 1", fn)
        gate = fn[fn.index("if nl in LOC_MIN_LVL"):fn.index("if d.get(\"loc\") == SEASON_LOC")]
        self.assertIn('"t": "loc_fix"', gate)

    def test_season_denial_returns_the_phone(self):
        fn = WEB[WEB.index("if d.get(\"loc\") == SEASON_LOC"):WEB.index("if d.get(\"loc\") in elite.LOCS")]
        self.assertIn('d = {**d, "loc": old_loc, "x": info["x"], "y": info["y"]}', fn)
        self.assertIn('"t": "loc_fix"', fn)

    def test_titles_for_every_gated_location(self):
        self.assertEqual(set(const("LOC_MIN_LVL")), set(const("LOC_TITLE")))
        self.assertTrue(set(const("LOC_MIN_LVL")) <= const("LOCS"))


@unittest.skipUnless(shutil.which("node"), "нет node")
class SameAsGame(unittest.TestCase):
    def test_levels_match_portal(self):
        head = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8")
        head = head[:head.index("\n", head.index("const run = "))]
        js = head + r'''
process.stdout.write(run(`JSON.stringify({min:Object.fromEntries(Object.entries(LOCS).map(([k, L]) => [k, L.min || 0])),
  name:Object.fromEntries(Object.entries(LOCS).map(([k, L]) => [k, L.name]))})`));
'''
        res = subprocess.run(["node", "-", str(ROOT / "game.html")], input=js, capture_output=True, text=True, timeout=300)
        self.assertEqual(res.returncode, 0, res.stderr[-2000:])
        g = json.loads(res.stdout)
        gated = const("LOC_MIN_LVL")
        for k, n in gated.items():
            self.assertEqual(g["min"][k], n, k)
            self.assertEqual(g["name"][k], const("LOC_TITLE")[k], k)
        # всё, что в игре закрыто уровнем выше 1, сервер тоже проверяет (кроме событий со своей проверкой)
        # «Страх», экземпляры элитной зоны и залы «Сотрудничества» пускают только своих (fear.enter, elite, coop)
        own = lambda k: k == "arena_fear" or k.startswith(("elite", "coop", "cw"))
        for k, n in g["min"].items():
            if n > 1 and not own(k):
                self.assertIn(k, gated, k)


if __name__ == "__main__":
    unittest.main()
