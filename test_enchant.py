"""Заточка: шансы на сервере и в игре одни и те же, двойное нажатие и смена вещи во время запроса не ломают учёт.

Запуск: python -m unittest test_enchant
"""
import json
import re
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
GAME = (ROOT / "game.html").read_text(encoding="utf-8")
ITEMS_SRC = (ROOT / "items.py").read_text(encoding="utf-8")


def server_list(name):
    m = re.search(rf"^{name} = (\[[^\]]*\])", ITEMS_SRC, re.M)
    return json.loads(m.group(1))


class EnchantTest(unittest.TestCase):
    def test_chances_same_as_game(self):
        game = json.loads(re.search(r"const ENCH_CHANCE = (\[[^\]]*\]);", GAME).group(1))
        self.assertEqual(server_list("ENCH_CHANCE"), game)
        self.assertEqual(len(game), int(re.search(r"const ENCH_MAX = (\d+);", GAME).group(1)))

    def test_one_request_at_a_time(self):
        """Пока сервер точит, вторая заточка не уходит (раньше нажатие на другую сферу отправляло ещё одну)."""
        a = GAME.index("doEnchant = async function(){")
        body = GAME[a:GAME.index("\n};", a)]
        self.assertIn("if(!x || enchBusy || enchReq) return;", body)
        self.assertIn("finally{ enchReq = false; }", body)
        self.assertIn('regApi("enchant", {uid:x.uid, sphere:sph})', body)       # сфера запроса, а не текущий выбор
        self.assertIn("enchSph = sph;", body)

    def test_result_goes_to_the_same_item(self):
        """Окно переключили на другую вещь, пока ждали ответа: итог достаётся той, что точилась."""
        self.assertIn("if(enchGet() !== x){ enchApplyQuiet(x, sph, r); return; }", GAME)
        a = GAME.index("function enchApplyQuiet(x, sph, r){")
        body = GAME[a:GAME.index("\n}", a)]
        for part in ("removeItem(S.inv, sph, 1)", 'r.result === "broken"', "runesBack(x)", "S.inv.splice(i, 1)", "x.e = r.e", "save()"):
            self.assertIn(part, body)

    def test_debris_scrap_counted_after_commit(self):
        """Лом за обломки идёт в учёт ресурсов только после записи результата (раньше — и при откате)."""
        a = ITEMS_SRC.index('if op == "enchant":')
        body = ITEMS_SRC[a:ITEMS_SRC.index('if op == "gone":', a)]
        self.assertLess(body.index("await s.commit()"), body.index("ledger.add(uid"))


if __name__ == "__main__":
    unittest.main()
