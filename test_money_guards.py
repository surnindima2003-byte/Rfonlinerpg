"""Критичное «деньги и данные»: RES_LEDGER=on действительно срезает, потолок ценного лута, вайп только
с подтверждением и с полной копией перед ним.

Запуск: python -m unittest test_money_guards
База и сеть не нужны: функции берутся из исходников, база подменяется в памяти.
"""
import datetime
import gzip
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")
ITEMS = (ROOT / "items.py").read_text(encoding="utf-8")
BACKUP = (ROOT / "backup.py").read_text(encoding="utf-8")

if "metrics" not in sys.modules:
    try:
        import metrics  # noqa: F401
    except Exception:
        m = types.ModuleType("metrics"); m.inc = lambda *a, **k: None; m.gauge = lambda *a, **k: None
        sys.modules["metrics"] = m
import ledger  # noqa: E402


class LedgerOn(unittest.TestCase):
    def setUp(self):
        self.mode = ledger.MODE
        ledger.MODE = "on"

    def tearDown(self):
        ledger.MODE = self.mode
        ledger._acc.pop(7, None)

    def test_scrap_and_cores_clamped(self):
        old = {"scrap": 1000, "cores": 10}
        new = {"scrap": 900_000, "cores": 500}
        fix = {}
        bad = ledger.enforce(old, new, [("scrap", 899_000, 3000), ("cores", 490, 3)], fix)
        self.assertEqual(bad, [])
        self.assertEqual(new["scrap"], 4000)
        self.assertEqual(new["cores"], 13)
        self.assertEqual(fix, {"scrap": 4000, "cores": 13})

    def test_materials_rejected(self):
        bad = ledger.enforce({"scrap": 0}, {"scrap": 0}, [("chip", 500, 3), ("scrap", 10, 3)], {})
        self.assertEqual(bad, ["chip"])

    def test_shadow_changes_nothing(self):
        ledger.MODE = "shadow"
        new = {"scrap": 900_000}
        fix = {}
        self.assertEqual(ledger.enforce({"scrap": 0}, new, [("scrap", 900_000, 3000)], fix), [])
        self.assertEqual(new["scrap"], 900_000)
        self.assertEqual(fix, {})

    def test_full_cycle_with_check(self):
        # приход за 10 убийств краулеров, а в сохранении миллион лома
        for _ in range(10):
            ledger.add(7, ledger.kill_income("scrap_crawler", "scrapfields"))
        old = {"scrap": 100, "inv": []}
        new = {"scrap": 1_000_000, "inv": []}
        notes = ledger.check(7, "t", old, new, {})
        self.assertTrue(notes)
        fix = {}
        self.assertEqual(ledger.enforce(old, new, notes, fix), [])
        self.assertLess(new["scrap"], 100 + 3000 + 100)
        self.assertEqual(fix["scrap"], new["scrap"])

    def test_wired_into_api_save(self):
        fn = WEB[WEB.index("async def api_save"):WEB.index("async def api_ack")]
        self.assertIn("ledger.enforce(", fn)
        self.assertIn('saveguard.MODE != "off" or ledger.MODE == "on"', fn)   # учёт работает и без SAVE_GUARD
        # отказ из-за материалов — тем же ответом, что понимает телефон
        part = fn[fn.index("ledger.enforce("):fn.index("saveguard.check(")]
        self.assertIn('"save rejected"', part)
        self.assertIn("status=409", part)


def loot_ns(**env):
    ns = {"env_int": lambda k, d: int(env.get(k, d)), "env_float": lambda k, d: float(env.get(k, d))}
    a = ITEMS.index("LOOT_FULL = env_int"); b = ITEMS.index("async def count_loot_kill")
    exec(ITEMS[a:b], ns)
    return ns


class LootCap(unittest.TestCase):
    def test_shape(self):
        lf = loot_ns()["loot_factor"]
        self.assertEqual(lf(0), 1.0)
        self.assertEqual(lf(2500), 1.0)
        self.assertAlmostEqual(lf(6000), 0.2)
        self.assertAlmostEqual(lf(7500), 0.1)
        self.assertEqual(lf(9000), 0.0)
        self.assertEqual(lf(200_000), 0.0)
        prev = 1.0
        for k in range(0, 12000, 50):          # не растёт с числом убийств
            self.assertLessEqual(lf(k), prev + 1e-12)
            prev = lf(k)

    def test_bot_gets_no_more_than_a_player(self):
        lf = loot_ns()["loot_factor"]
        full = lambda n: sum(lf(k) for k in range(n))
        bot = full(216_000)                      # 2,5 убийства/с круглые сутки
        player = full(6000)
        self.assertLess(bot, 5000)
        self.assertLess(bot / player, 1.1)       # раньше было в 10 раз больше

    def test_zero_not_above_low(self):
        lf = loot_ns(LOOT_ZERO_KILLS=6000)["loot_factor"]   # ошибочная настройка — сразу ноль, без деления на 0
        self.assertEqual(lf(6000), 0.0)


def epoch_ns():
    ns = {}
    exec(WEB[WEB.index("def wipe_allowed"):WEB.index("def ensure_epoch")], ns)
    return ns["wipe_allowed"]


class WipeNeedsConfirm(unittest.TestCase):
    def test_rules(self):
        ok = epoch_ns()
        self.assertFalse(ok("5:wipe-3", "5:wipe-1", "wipe-1", ""))        # WIPE_TOKEN удалили — вайпа нет
        self.assertFalse(ok("5:wipe-3", "5:wipe-4", "wipe-4", ""))        # сменили без подтверждения — нет
        self.assertFalse(ok("5:wipe-3", "5:wipe-4", "wipe-4", "wipe-3"))  # подтверждение от прошлого вайпа — нет
        self.assertTrue(ok("5:wipe-3", "5:wipe-4", "wipe-4", "wipe-4"))
        self.assertFalse(ok(None, "5:wipe-1", "wipe-1", ""))              # новая база
        self.assertFalse(ok("5:wipe-4", "5:wipe-4", "wipe-4", "wipe-4"))  # эпоха та же

    def test_ensure_epoch_keeps_old_epoch_and_backs_up(self):
        fn = WEB[WEB.index("def ensure_epoch"):WEB.index("# ---------- проверка игрока")]
        self.assertIn("global DATA_EPOCH", fn)
        self.assertIn("DATA_EPOCH = current", fn)
        self.assertIn('getenv("WIPE_CONFIRM"', fn)
        self.assertLess(fn.index("dump_all("), fn.index("drop_all("))     # копия — до сноса таблиц


class FakeTable:
    def __init__(self, name, rows):
        self.name, self.rows = name, rows


class FakeConn:
    def __init__(self, tables):
        self.tables = tables

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execution_options(self, **kw):
        return self

    def execute(self, t):
        return [types.SimpleNamespace(_mapping=r) for r in t.rows]


class FullDump(unittest.TestCase):
    def test_all_tables_streamed_and_atomic(self):
        tables = [FakeTable("game_saves", [{"tg_id": 1, "data": "{\"S\":{}}"}, {"tg_id": 2, "data": "x"}]),
                  FakeTable("docs", [{"path": "guilds/g1", "data": "{}"}]),
                  FakeTable("gram_wallets", [{"tg_id": 1, "nano": 5}])]
        tmp = tempfile.mkdtemp()
        ns = {"os": os, "datetime": datetime, "gzip": gzip, "json": json, "BACKUP_DIR": tmp,
              "engine": types.SimpleNamespace(connect=lambda: FakeConn(tables)),
              "sa_inspect": lambda c: types.SimpleNamespace(get_table_names=lambda: [t.name for t in tables]),
              "Base": types.SimpleNamespace(metadata=types.SimpleNamespace(sorted_tables=tables)),
              "select": lambda t: t}
        exec(BACKUP[BACKUP.index("def dump_all"):BACKUP.index("def make_backup")], ns)
        path = ns["dump_all"]("wipe")
        self.assertTrue(os.path.basename(path).startswith("wipe-"))
        self.assertFalse([n for n in os.listdir(tmp) if n.endswith(".part")])
        with gzip.open(path, "rt", encoding="utf-8") as f:
            lines = [json.loads(x) for x in f]
        self.assertEqual([x["table"] for x in lines if "table" in x], ["game_saves", "docs", "gram_wallets"])
        self.assertEqual(len(lines), 3 + 4)

    def test_daily_full_dump_on_pg(self):
        fn = BACKUP[BACKUP.index("def make_backup"):BACKUP.index("async def send_to_admins")]
        self.assertIn('dump_all("full")', fn)
        self.assertIn("if IS_PG:", fn)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
