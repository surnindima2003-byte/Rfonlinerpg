"""Вайп не должен трогать деньги игроков, а каждая таблица должна быть осознанно отнесена к «вайпу» или «сохранить»."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODELS = (ROOT / "models.py").read_text(encoding="utf-8")
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")

MONEY = {"gram_wallets", "gram_tx", "gram_withdrawals", "referrals", "ref_earn", "star_payments"}
# игровые таблицы, которые вайп обнуляет; новая таблица должна попасть сюда или в KEEP_ON_WIPE
GAME = {"players", "game_saves", "grants", "docs", "market_lots", "market_hist", "item_inst", "sphere_bal",
        "pvp_stats", "server_prog", "loot_day", "faction_lock"}


class WipePolicyTest(unittest.TestCase):
    def keep(self):
        m = re.search(r"KEEP_ON_WIPE = \{(.*?)\}", WEB, re.S)
        self.assertIsNotNone(m)
        return set(re.findall(r'"(\w+)"', m.group(1)))

    def test_money_is_kept(self):
        self.assertTrue(MONEY <= self.keep())

    def test_every_table_is_classified(self):
        tables = set(re.findall(r'__tablename__ = "(\w+)"', MODELS))
        unknown = tables - self.keep() - GAME
        self.assertFalse(unknown, f"новые таблицы без решения про вайп: {unknown}")


if __name__ == "__main__":
    unittest.main()
