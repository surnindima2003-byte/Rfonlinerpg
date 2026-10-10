"""Цены паков магазина в игре и на сервере совпадают (сервер списывает GRAM по своей таблице).

Обычные цены — base в game.html → PACKS и gram.py → PACK_PRICES. Скидку даёт только распродажа:
вкладки паков и формула цены со скидкой в игре (packPrice) и на сервере (sale_price) должны совпадать.
"""
import json
import re
import subprocess
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

GAME = (ROOT / "game.html").read_text(encoding="utf-8")


def client_packs():
    """id пака → (обычная цена, вкладка) из game.html."""
    start = GAME.index("const PACKS = [\n")
    blk = GAME[start:GAME.index("\n];", start)]
    packs = {"season": (15.0, "packs")}
    for pid, base, tab in re.findall(r'SP\("(\w+)", "[^"]+", ([\d.]+), "[^"]+", \[.*?\](?:, "(\w+)")?\)', blk):
        packs[pid] = (float(base), tab or "packs")
    # паки дронов, крыльев и плащей строятся из таблиц: «dr_» + имя, «wn_» + имя без «wg_», «cl_» + имя без «ck_»
    for table, src, dst, tab in (("DRONES", "d_", "dr_", "drones"), ("WINGS", "wg_", "wn_", "wings"), ("CLOAKS", "ck_", "cl_", "cloaks")):
        t = GAME[GAME.index(f"const {table} = {{"):GAME.index("\n};", GAME.index(f"const {table} = {{"))]
        for item, price in re.findall(src + r'(\w+):\s*\{[^\n]*?src:"gram", price:(\d+)', t):
            packs[dst + item] = (float(price), tab)
    return packs


class ShopPricesTest(unittest.TestCase):
    def test_same_regular_prices(self):
        client = {k: v[0] for k, v in client_packs().items()}
        self.assertEqual(client, gram.PACK_PRICES)

    def test_no_permanent_discount(self):
        self.assertNotIn("SHOP_SALE", GAME)                         # раньше «Паки» и «Допы» всегда стоили 70%
        self.assertNotIn("SHOP_SALE", (ROOT / "gram.py").read_text(encoding="utf-8"))
        self.assertIn("for(const pk of PACKS) if(pk.price == null) pk.price = pk.base;", GAME)

    def test_same_tabs(self):
        for pid, (_, tab) in client_packs().items():
            with self.subTest(pack=pid):
                self.assertEqual(gram.pack_tab(pid), "" if pid == "season" else tab)
        self.assertEqual(set(gram.SHOP_TABS), {t for _, t in client_packs().values()})

    def test_same_sale_formula(self):
        """Цена со скидкой в игре (packPrice) и на сервере одинаковая для всех паков и скидок."""
        line = re.search(r"return (Math\.floor\(\(Math\.round\(pk\.base\*100\)\*\(100 - sl\.pct\) \+ 50\)/100\)/100);", GAME)
        self.assertIsNotNone(line, "в packPrice поменялась формула цены")
        pcts = list(range(gram.SALE_PCT[0], gram.SALE_PCT[1] + 1))
        bases = sorted(set(gram.PACK_PRICES.values()))
        js = (f"const B = {json.dumps(bases)}, P = {json.dumps(pcts)}, out = [];\n"
              f"for (const b of B) for (const p of P) {{ const pk = {{base:b}}, sl = {{pct:p}}; out.push({line.group(1)}); }}\n"
              "process.stdout.write(JSON.stringify(out));")
        try:
            res = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=60, check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            raise unittest.SkipTest(f"нужен node: {e}")
        client = json.loads(res.stdout)
        server = [(round(b * 100) * (100 - p) + 50) // 100 / 100 for b in bases for p in pcts]
        self.assertEqual(client, server)
        sale = {"pct": 30, "tabs": ["packs"], "start": 0, "end": 2e9}
        self.assertEqual(gram.sale_price("p_std", sale, now=1e9), 14.0)        # то же, что выдаёт формула выше


if __name__ == "__main__":
    unittest.main()
