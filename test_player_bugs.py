"""Баги, которые видят игроки: продажа торговцу не съедает учтённые сферы, вкладка после вайпа не теряет
прогресс молча, «Сессия устарела» не показывается вытесненной вкладке, в призовом рейтинге нет сырого ника Telegram.

Запуск: python -m unittest test_player_bugs
Проверка продажи запускает настоящую функцию из game.html в node (если node нет — этот тест пропускается).
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GAME = (ROOT / "game.html").read_text(encoding="utf-8")
SEASON = (ROOT / "seasonpts.py").read_text(encoding="utf-8")


def _line(start):
    a = GAME.index(start)
    return GAME[a:GAME.index("\n", a)]


class TraderSale(unittest.TestCase):
    def run_js(self, inv, store, reg, n):
        node = shutil.which("node")
        if not node:
            self.skipTest("нет node")
        a = GAME.index("function regUseSph(")
        fn = GAME[a:GAME.index("\nfunction regGone", a)]
        js = "\n".join([
            "const ITEMS = {sph_ti:{type:'ench'}};",
            _line("const count = (list, id) =>"),
            f"const S = {{inv:{json.dumps(inv)}, store:{json.dumps(store)}}};",
            f"const REG = {{on:true, sph:{{sph_ti:{reg}}}}};",
            "const sent = []; function regApi(op, b){ sent.push(b); }",
            fn,
            f"regUseSph('sph_ti', {n});",
            "console.log(JSON.stringify({left: REG.sph.sph_ti, sent}));",
        ])
        out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_unregistered_go_first(self):
        # 10 сфер, из них 4 учтённых (продаются за GRAM). Продаём 5 — учтённые не трогаем
        r = self.run_js([{"id": "sph_ti", "n": 10}], [], 4, 5)
        self.assertEqual(r["left"], 4)
        self.assertEqual(r["sent"], [])

    def test_registered_only_when_needed(self):
        # продаём 8 из 10: неучтённых 6, значит учтённых уходит 2
        r = self.run_js([{"id": "sph_ti", "n": 10}], [], 4, 8)
        self.assertEqual(r["left"], 2)
        self.assertEqual(r["sent"], [{"sph": {"sph_ti": 2}}])

    def test_store_counts_as_unregistered_stock(self):
        # в сумке 3, на складе 5, учтённых 3 (реестр общий на сумку и склад): неучтённых 5 — продажа 3 их и тратит
        r = self.run_js([{"id": "sph_ti", "n": 3}], [{"id": "sph_ti", "n": 5}], 3, 3)
        self.assertEqual(r["left"], 3)

    def test_called_before_removal(self):
        a = GAME.index("else if(d.sell !== undefined){")
        part = GAME[a:GAME.index("else if(d.st)", a)]
        self.assertLess(part.index("regUseSph("), part.index("removeItem(S.inv"))


class StaleEpoch(unittest.TestCase):
    def test_push_handles_stale_epoch(self):
        a = GAME.index("async function push(force)")
        push = GAME[a:GAME.index("setInterval(() => push(false)", a)]
        self.assertIn('includes("stale epoch")', push)
        self.assertIn("staleEpoch()", push)
        fn = GAME[GAME.index("function staleEpoch()"):GAME.index("const api = NET.api")]
        self.assertIn("serverEpoch = null", fn)          # сохранения больше не уходят
        self.assertIn("location.reload()", fn)           # загрузка увидит новую эпоху и начнёт заново


class CloseReason(unittest.TestCase):
    def test_replaced_tab_is_not_session_expired(self):
        a = GAME.index("ws.onclose = ev =>")
        h = GAME[a:GAME.index("};", a)]
        self.assertIn('includes("too many")', h)
        self.assertLess(h.index("replacedByOther()"), h.index("sessionExpired()"))
        rt = (ROOT / "realtime.py").read_text(encoding="utf-8")
        self.assertIn('old.close(CLOSE_REPLACED, "too many connections")', rt)
        self.assertIn("function replacedByOther()", GAME)


class SeasonNames(unittest.TestCase):
    def test_ranking_uses_public_name(self):
        nicks = SEASON[SEASON.index("async def _nicks"):SEASON.index("async def top(")]
        self.assertIn("_public(b, c)", nicks)
        self.assertNotIn('b or c or "Пилот"', SEASON)
        self.assertIn('"nick": _public("", h.nick)', SEASON)         # итоги прошлых сезонов тоже

    def test_fallback_without_webserver(self):
        ns = {}
        a = SEASON.index("def _public(")
        exec(SEASON[a:SEASON.index("\n\n\n", a)], ns)
        sys.modules.setdefault("webserver", None)                  # импорт webserver не удастся
        try:
            self.assertEqual(ns["_public"]("", "Администратор"), "Пилот")
            self.assertEqual(ns["_public"]("Rex", "Администратор"), "Rex")
        finally:
            if sys.modules.get("webserver") is None:
                sys.modules.pop("webserver", None)


if __name__ == "__main__":
    unittest.main()
