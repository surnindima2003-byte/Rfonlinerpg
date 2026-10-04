"""Цены паков магазина в игре и на сервере совпадают (сервер списывает GRAM по своей таблице)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FULL_PRICE_TABS = {"boost", "drones", "runes", "artifacts"}      # без скидки сезона


class ShopPricesTest(unittest.TestCase):
    def test_same_prices(self):
        game = (ROOT / "game.html").read_text(encoding="utf-8")
        start = game.index("const PACKS = [\n")
        blk = game[start:game.index("\n];", start)]
        sale = float(re.search(r"const SHOP_SALE = ([\d.]+);", game).group(1))
        client = {}
        for pid, base, tab in re.findall(r'SP\("(\w+)", "[^"]+", ([\d.]+), "[^"]+", \[.*?\](?:, "(\w+)")?\)', blk):
            b = float(base)
            client[pid] = b if tab in FULL_PRICE_TABS else round(b * sale, 2)
        client["season"] = 15
        # паки дронов строятся из таблицы DRONES: «dr_» + имя, цена — price
        drones = game[game.index("const DRONES = {"):game.index("\n};", game.index("const DRONES = {"))]
        for did, price in re.findall(r'd_(\w+):\s*\{[^\n]*?src:"gram", price:(\d+)', drones):
            client["dr_" + did] = float(price)
        # паки крыльев строятся из таблицы WINGS: «wn_» + имя без «wg_», цена — price (полная)
        wings = game[game.index("const WINGS = {"):game.index("\n};", game.index("const WINGS = {"))]
        for wid, price in re.findall(r'wg_(\w+):\s*\{[^\n]*?src:"gram", price:(\d+)', wings):
            client["wn_" + wid] = float(price)
        src = (ROOT / "gram.py").read_text(encoding="utf-8")
        ns = {}
        a = src.index("SHOP_SALE =")
        b = src.index("\n\n", src.index("PACK_PRICES = {"))          # словарь и все PACK_PRICES.update(...) после него
        exec(src[a:b], ns)
        self.assertEqual(client, ns["PACK_PRICES"])


if __name__ == "__main__":
    unittest.main()
