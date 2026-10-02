"""Цены паков магазина в игре и на сервере совпадают (сервер списывает GRAM по своей таблице)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class ShopPricesTest(unittest.TestCase):
    def test_same_prices(self):
        game = (ROOT / "game.html").read_text(encoding="utf-8")
        blk = game[game.index("const PACKS = [\n"):game.index("];", game.index("const PACKS = [\n"))]
        sale = float(re.search(r"const SHOP_SALE = ([\d.]+);", game).group(1))
        client = {}
        for pid, base, tab in re.findall(r'SP\("(\w+)", "[^"]+", ([\d.]+), "[^"]+", \[.*?\](?:, "(\w+)")?\)', blk):
            b = float(base)
            client[pid] = b if tab == "boost" else round(b * sale, 2)
        client["season"] = 15
        src = (ROOT / "gram.py").read_text(encoding="utf-8")
        ns = {}
        exec(src[src.index("SHOP_SALE ="):src.index("}}", src.index("PACK_PRICES = {")) + 2], ns)
        self.assertEqual(client, ns["PACK_PRICES"])


if __name__ == "__main__":
    unittest.main()
