"""Распродажа в магазине GRAM: скидку, срок и разделы задаёт администратор, цену считает сервер.

Запуск: python -m unittest test_shop_sale
База и сеть не нужны: таблица meta и рассылка игрокам подменяются в памяти.
"""
import asyncio
import json
import logging
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _stub(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m


# без установленных зависимостей — заглушки (как в test_gram_deposits); с ними — настоящие модули
try:
    import aiohttp  # noqa: F401
except ImportError:
    _stub("aiohttp", web=None, ClientTimeout=lambda **k: None, ClientSession=None)
try:
    import sqlalchemy  # noqa: F401
    import sqlalchemy.orm.attributes  # noqa: F401
except ImportError:
    _stub("sqlalchemy", select=None, func=None, update=None)
    _stub("sqlalchemy.orm")
    _stub("sqlalchemy.orm.attributes", set_committed_value=lambda *a: None)
for _name, _attrs in (("db", {"SessionLocal": None}),
                      ("models", {k: None for k in ("GramWallet", "GramTx", "GramWithdrawal", "GameSave", "Meta",
                                                    "Referral", "RefEarn", "StarPayment")})):
    try:
        __import__(_name)
    except Exception:
        _stub(_name, **_attrs)
import gram  # noqa: E402

logging.getLogger("gram").setLevel(logging.ERROR)          # предупреждение о недоступной базе в выводе тестов не нужно
NOW = 1_800_000_000
SALE = {"pct": 30, "tabs": ["packs", "dops"], "start": NOW - 60, "end": NOW + 3600}


class FakeWeb:
    """web из aiohttp: json_response возвращает сам словарь, чтобы его было удобно проверять."""
    class HTTPBadRequest(Exception):
        def __init__(self, text=""):
            super().__init__(text)

    @staticmethod
    def json_response(data):
        return data


class Prices(unittest.TestCase):
    def test_regular_prices_without_sale(self):
        for pack, base in gram.PACK_PRICES.items():
            self.assertEqual(gram.sale_price(pack, None, NOW), base)
        self.assertEqual(gram.PACK_PRICES["p_std"], 20)              # раньше всегда 14: «скидка» 30% без срока
        self.assertIsNone(gram.sale_price("nope", SALE, NOW))

    def test_sale_discounts_only_chosen_tabs(self):
        self.assertEqual(gram.sale_price("p_std", SALE, NOW), 14.0)
        self.assertEqual(gram.sale_price("d_base", SALE, NOW), 45.5)
        self.assertEqual(gram.sale_price("p_start", SALE, NOW), 0.7)
        self.assertEqual(gram.sale_price("u1", SALE, NOW), 25)       # «Усиление» в эту распродажу не входит
        self.assertEqual(gram.sale_price("season", {**SALE, "tabs": list(gram.SHOP_TABS)}, NOW), 15)   # билет сезона — никогда

    def test_sale_has_start_and_end(self):
        self.assertEqual(gram.sale_price("p_std", SALE, SALE["end"]), 20)       # закончилась — обычная цена
        self.assertEqual(gram.sale_price("p_std", SALE, SALE["start"] - 1), 20)
        self.assertEqual(gram.sale_price("p_std", SALE, SALE["end"] - 1), 14.0)

    def test_rounding_in_whole_hundredths(self):
        odd = {**SALE, "pct": 33}
        self.assertEqual(gram.sale_price("p_base", odd, NOW), 3.35)               # 5 × 0,67
        self.assertEqual(gram.sale_price("p_start", {**SALE, "pct": 45}, NOW), 0.55)   # 0,55 ровно, без 0,54999
        for pack in gram.PACK_PRICES:
            p = gram.sale_price(pack, {**SALE, "pct": 37, "tabs": list(gram.SHOP_TABS)}, NOW)
            self.assertEqual(round(p, 2), p, pack)

    def test_tabs_cover_every_pack(self):
        for pack in gram.PACK_PRICES:
            self.assertEqual(gram.pack_tab(pack) in gram.SHOP_TABS, pack != "season", pack)
        self.assertEqual(gram.pack_tab("tp:5"), "")


class ViewAndForm(unittest.TestCase):
    def test_view(self):
        v = gram.sale_view(SALE, NOW)
        self.assertEqual(v, {"pct": 30, "tabs": ["packs", "dops"], "end": SALE["end"] * 1000, "left": 3600 * 1000})
        self.assertIsNone(gram.sale_view(SALE, SALE["end"]))
        self.assertIsNone(gram.sale_view(None, NOW))

    def test_parse_ok(self):
        sale, err = gram.sale_parse({"pct": "25", "hours": 48, "tabs": ["dops", "packs", "nope", 5]}, NOW)
        self.assertIsNone(err)
        self.assertEqual(sale, {"pct": 25, "tabs": ["packs", "dops"], "start": NOW, "end": NOW + 48 * 3600})

    def test_parse_rejects(self):
        for body in ({"pct": 3, "hours": 24, "tabs": ["packs"]}, {"pct": 95, "hours": 24, "tabs": ["packs"]},
                     {"pct": 30, "hours": 0.5, "tabs": ["packs"]}, {"pct": 30, "hours": 24 * 15, "tabs": ["packs"]},
                     {"pct": 30, "hours": "nan", "tabs": ["packs"]}, {"pct": 30, "hours": 24, "tabs": []},
                     {"pct": 30, "hours": 24, "tabs": "packs"}, {"pct": "много", "hours": 24, "tabs": ["packs"]}, {}):
            with self.subTest(body=body):
                sale, err = gram.sale_parse(body, NOW)
                self.assertIsNone(sale)
                self.assertTrue(err)

    def test_price_mismatch(self):
        self.assertIsNone(gram.price_mismatch(14.0, 14))
        self.assertIsNone(gram.price_mismatch(45.5, "45.5"))
        self.assertEqual(gram.price_mismatch(20, 14), "Цена изменилась: теперь 20 GRAM")
        self.assertEqual(gram.price_mismatch(45.5, 65), "Цена изменилась: теперь 45,5 GRAM")
        self.assertIn("Магазин обновился", gram.price_mismatch(20, None))      # старая игра цену не присылает
        self.assertTrue(gram.price_mismatch(20, "nan"))


class Storage(unittest.TestCase):
    def setUp(self):
        self.meta = {}
        self.sent = []

        async def get(key):
            if isinstance(self.meta.get(key), Exception):
                raise self.meta[key]
            return self.meta.get(key, "")

        async def put(key, value):
            self.meta[key] = value

        self.orig = gram._meta_get, gram._meta_set, gram.sale_broadcast, gram.web, gram.time.time
        gram._meta_get, gram._meta_set = get, put
        gram.sale_broadcast = lambda sale: self.sent.append(sale)
        gram.web = FakeWeb
        gram.time.time = lambda: NOW
        gram._sale.update(v=None, at=0.0)

    def tearDown(self):
        gram._meta_get, gram._meta_set, gram.sale_broadcast, gram.web, gram.time.time = self.orig
        gram._sale.update(v=None, at=0.0)

    def test_cache_and_bad_record(self):
        self.meta[gram.SALE_KEY] = json.dumps(SALE)
        self.assertEqual(asyncio.run(gram.sale_get()), SALE)
        self.meta[gram.SALE_KEY] = ""
        self.assertEqual(asyncio.run(gram.sale_get()), SALE)                     # кэш: базу читаем не чаще раза в SALE_TTL
        gram._sale["at"] = 0
        self.meta[gram.SALE_KEY] = '{"pct": 30}'
        self.assertIsNone(asyncio.run(gram.sale_get()))                          # испорченная запись — без скидки
        gram._sale.update(v=SALE, at=0)
        self.meta[gram.SALE_KEY] = RuntimeError("база недоступна")
        self.assertEqual(asyncio.run(gram.sale_get()), SALE)                     # база упала — остаётся прежнее

    def test_admin_start_and_stop(self):
        r = asyncio.run(gram._admin_sale({"action": "start", "pct": 40, "hours": 24, "tabs": ["boost"]}, {"id": 7}))
        self.assertTrue(r["ok"])
        self.assertEqual(r["sale"], {"pct": 40, "tabs": ["boost"], "end": (NOW + 86400) * 1000, "left": 86400 * 1000})
        saved = json.loads(self.meta[gram.SALE_KEY])
        self.assertEqual((saved["pct"], saved["by"]), (40, 7))
        self.assertEqual(self.sent[-1]["tabs"], ["boost"])                       # игроки в сети узнают сразу
        self.assertEqual(gram.sale_price("u2", asyncio.run(gram.sale_get()), NOW), 24.0)
        r = asyncio.run(gram._admin_sale({"action": "stop"}, {"id": 7}))
        self.assertTrue(r["ok"])
        self.assertIsNone(r["sale"])
        self.assertIsNone(self.sent[-1])
        self.assertEqual(self.meta[gram.SALE_KEY], "")

    def test_admin_bad_form_changes_nothing(self):
        r = asyncio.run(gram._admin_sale({"action": "start", "pct": 99, "hours": 24, "tabs": ["packs"]}, {"id": 7}))
        self.assertFalse(r["ok"])
        self.assertNotIn(gram.SALE_KEY, self.meta)
        self.assertEqual(self.sent, [])
        with self.assertRaises(FakeWeb.HTTPBadRequest):
            asyncio.run(gram._admin_sale({"action": "drop"}, {"id": 7}))


class Wiring(unittest.TestCase):
    SRC = (ROOT / "gram.py").read_text(encoding="utf-8")

    def block(self, start, end):
        a = self.SRC.index(start)
        return self.SRC[a:self.SRC.index(end, a)]

    def test_spend_charges_server_price_seen_by_player(self):
        spend = self.block('        if op == "spend":', '        if op == "withdraw":')
        self.assertIn("price = sale_price(pack, sale)", spend)
        self.assertIn('price_mismatch(price, body.get("expect"))', spend)
        self.assertIn('"sale": sale_view(sale)', self.block('        if op == "state":', '        if op == "spend":'))

    def test_only_admin_starts_sales(self):
        adm = self.block("async def api_gram_admin(request):", "    async with SessionLocal() as s:")
        self.assertLess(adm.index('if not user["admin"]'), adm.index('if op == "sale"'))


if __name__ == "__main__":
    unittest.main()
