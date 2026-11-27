"""Учёт ресурсов и продажа торговцу всего, кроме снаряжения (материалы, зелья, сферы, руны, ремкомплекты).

Продажу делает телефон, сервер её не видит — но видит, что вещи пропали из сумки и склада между сохранениями.
Цену пропавшего учёт ресурсов (RES_LEDGER) прибавляет к допустимому приходу лома: иначе после включения
RES_LEDGER=on честная продажа торговцу срезалась бы. Цены — как ITEMS[id].sell в игре.
Также проверяется, что награды обучения (всё целиком) помещаются в постоянный запас одного сохранения.
Запуск: python -m unittest test_ledger_sell
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
if "metrics" not in sys.modules:
    try:
        import metrics  # noqa: F401
    except Exception:
        sys.modules["metrics"] = mock.MagicMock()

import ledger  # noqa: E402


def S(**n):
    return {"scrap": n.pop("scrap", 0), "inv": [{"id": k, "n": v} for k, v in n.items()], "store": []}


@unittest.skipUnless(shutil.which("node"), "нет node")
class PricesMatchGame(unittest.TestCase):
    def test_sell_table(self):
        head = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8")
        head = head[:head.index("\n", head.index("const run = "))]
        js = head + r'''
process.stdout.write(run(`JSON.stringify({sell:Object.fromEntries(Object.entries(ITEMS).filter(([k, v]) => v.type !== "gear" && v.sell > 0 && !v.nosell).map(([k, v]) => [k, v.sell])),
  tut:TUT.filter(s => s.reward).reduce((a, s) => a + (s.reward.scrap || 0), 0) + TUT_FINAL.scrap, tutSph:TUT_FINAL.sph || 0})`));
'''
        res = subprocess.run(["node", "-", str(ROOT / "game.html")], input=js, capture_output=True, text=True, timeout=300)
        self.assertEqual(res.returncode, 0, res.stderr[-2000:])
        g = json.loads(res.stdout)
        self.assertEqual(ledger.SELL, g["sell"])
        # обучение целиком (все шаги и финал) меньше постоянного запаса лома одного сохранения —
        # поэтому его награды не нужно отдельно проводить через сервер
        self.assertLess(g["tut"], ledger.SLACK["scrap"])
        self.assertLessEqual(g["tutSph"], 1)


class SoldValue(unittest.TestCase):
    def test_only_what_disappeared(self):
        self.assertEqual(ledger.sold_value(S(chip=100, wire=10), S(chip=40, wire=12)), 60 * 8)
        self.assertEqual(ledger.sold_value(S(r_war=1, sph_ti=2), S()), 2000 + 400)
        self.assertEqual(ledger.sold_value(S(), S(chip=5)), 0)
        old = {"inv": [{"id": "pot_xp", "n": 3}], "store": [{"id": "pot_xp", "n": 7}]}
        self.assertEqual(ledger.sold_value(old, {"inv": [], "store": [{"id": "pot_xp", "n": 2}]}), 8 * 75)

    def test_check_allows_honest_sale(self):
        uid = 9001
        ledger._acc[uid] = {"t": 0}                               # игрок в игре с перезапуска, прихода с мобов нет
        big = 20_000                                              # продал 2 500 микросхем = 20 000 лома
        notes = ledger.check(uid, "t", S(scrap=0, chip=2500), S(scrap=big), {})
        self.assertEqual(notes, [])
        ledger._acc[uid] = {"t": 0}
        notes = ledger.check(uid, "t", S(scrap=0, chip=2500), S(scrap=big + ledger.SLACK["scrap"] + 1, chip=2500), {})
        self.assertEqual([k for k, *_ in notes], ["scrap"])        # без продажи столько лома взяться неоткуда


if __name__ == "__main__":
    unittest.main()
