"""Учёт зелий: сервер знает весь законный приход зелий и срезает лишнее в сохранении.

Источники: дроп с мобов (items.roll_pots), мировой босс, паки магазина (saveguard.PACK_GRANTS), награды VIP
(vip.REWARDS — забираются через /api/vip/claim), выдачи админа. У торговца зелий нет.

Запуск: python -m unittest test_pots_ledger
"""
import asyncio
import json
import pathlib
import shutil
import subprocess
import sys
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for name in ("aiohttp", "sqlalchemy", "db", "models", "db_atomic"):
    try:
        __import__(name)
    except Exception:
        sys.modules[name] = mock.MagicMock()
import ledger      # noqa: E402
import saveguard   # noqa: E402
import vip         # noqa: E402

GAME = (ROOT / "game.html").read_text(encoding="utf-8")
COUNTED = ("scrap", "cores", "sph_cu", "sph_ti", "pot_hp", "pot_atk", "pot_xp")


def bag(**pots):
    return [{"id": k, "n": n} for k, n in pots.items()]


class LedgerPots(unittest.TestCase):
    def setUp(self):
        self.mode = ledger.MODE
        ledger.MODE = "on"
        ledger._acc.clear()

    def tearDown(self):
        ledger.MODE = self.mode
        ledger._acc.clear()

    def test_drop_income_passes(self):
        ledger.add(1, {"pot_hp": 2})
        old, new = {"inv": bag(pot_hp=1)}, {"inv": bag(pot_hp=4)}          # +3 = 2 с дропа + 1 запас
        self.assertEqual([n for n in ledger.check(1, "x", old, new, {}) if n[0].startswith("pot")], [])

    def test_extra_is_cut_from_store_then_bag(self):
        ledger.add(1, {"pot_xp": 1})
        old = {"inv": bag(pot_xp=2), "store": []}
        new = {"inv": bag(pot_xp=10), "store": bag(pot_xp=5)}            # +13 при законных 1 + 1
        notes = ledger.check(1, "x", old, new, {})
        self.assertIn("pot_xp", [k for k, *_ in notes])
        fix = {}
        self.assertEqual(ledger.enforce(old, new, notes, fix), [])         # зелья — не отказ, а срез
        self.assertEqual(fix["pot_xp"], 4)
        self.assertEqual(new["store"], [])                                 # склад — первым
        self.assertEqual(new["inv"], [{"id": "pot_xp", "n": 4}])

    def test_admin_grant_and_purchase_count(self):
        new = {"inv": bag(pot_atk=31)}
        notes = ledger.check(2, "x", {"inv": []}, new, {"pot_atk": 30})    # 30 от админа / пака + 1 запас
        self.assertEqual([n for n in notes if n[0] == "pot_atk"], [])

    def test_vip_claim_records_income(self):
        saveguard._purchases.clear()
        level = mock.AsyncMock(return_value=5)

        class Res:
            def __init__(self, v):
                self.v = v

            def scalar_one_or_none(self):
                return self.v

        class Sess:
            def __init__(self):
                self.calls, self.added = 0, []

            async def execute(self, *_a, **_k):
                self.calls += 1
                # 1-й запрос — документ vip/<id> (нет), 2-й — сохранение: VIP 1 уже получен на телефоне
                return Res(None if self.calls == 1 else json.dumps({"S": {"vipClaimed": [0]}}))

            def add(self, row):
                self.added.append(row)

        class FakeDoc:
            path = None

            def __init__(self, **kw):
                self.__dict__.update(kw)

        models = types.SimpleNamespace(Doc=FakeDoc, GameSave=mock.MagicMock())
        with mock.patch.object(vip, "level", level), mock.patch.object(vip, "select", mock.MagicMock()), \
                mock.patch.dict(sys.modules, {"models": models}):
            s = Sess()
            self.assertEqual(asyncio.run(vip.claim(s, 7, 0)), "Награда уже получена")
            s = Sess()
            self.assertIsNone(asyncio.run(vip.claim(s, 7, 4)))
            self.assertEqual(json.loads(s.added[0].data), [0, 4])
            s = Sess()
            self.assertIn("VIP 6", asyncio.run(vip.claim(s, 7, 5)))         # уровень ещё не открыт
        self.assertEqual(ledger.peek(7)["pot_hp"], 10)
        self.assertEqual(saveguard._purchases[7]["pot_xp"], 5)
        self.assertEqual(saveguard._purchases[7]["sph"], 1)
        saveguard._purchases.clear()


class Texts(unittest.TestCase):
    def test_trader_sells_no_potions(self):
        tr = GAME[GAME.index("const TR_SECT = ["):GAME.index("\n];", GAME.index("const TR_SECT = ["))]
        self.assertNotIn("pot_", tr)
        shop = GAME[GAME.index("const SHOP = ["):GAME.index("\n", GAME.index("const SHOP = ["))]
        self.assertNotIn("pot_", shop)

    def test_client_claims_vip_on_server(self):
        self.assertIn('NET.api("/api/vip/claim", {i})', GAME)
        self.assertIn('for(const k of ["pot_hp", "pot_atk", "pot_xp"]){', GAME)


@unittest.skipUnless(shutil.which("node"), "нет node")
class Mirror(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        head = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8")
        head = head[:head.index("\n", head.index("const run = "))]
        check = r'''
const out = run(`(() => JSON.stringify({vip:VIP.map(v => v.rew), packs:Object.fromEntries(PACKS.map(p => [p.id, p.rew || []]))}))()`);
console.log(out);
'''
        res = subprocess.run(["node", "-", str(ROOT / "game.html")], input=head + check, capture_output=True, text=True, timeout=300)
        cls.r = json.loads(res.stdout.strip().splitlines()[-1])

    def test_vip_rewards(self):
        game = [{r["id"]: r["n"] for r in rew if r["id"] in COUNTED} for rew in self.r["vip"]]
        self.assertEqual(game, vip.REWARDS)
        self.assertEqual(len(vip.REWARDS), len(vip.LEVELS))

    def test_pack_potions(self):
        for pid, rew in self.r["packs"].items():
            pots = {r["id"]: r["n"] for r in rew if r["id"].startswith("pot_")}
            srv = {k: v for k, v in saveguard.PACK_GRANTS.get(pid, {}).items() if k.startswith("pot_")}
            self.assertEqual(srv, pots, pid)


if __name__ == "__main__":
    unittest.main()
