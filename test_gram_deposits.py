"""Приём пополнений TON: вернувшиеся переводы не зачисляются, большая очередь догоняется без потерь.

Запуск: python -m unittest test_gram_deposits
База и сеть не нужны: курсор, точка продолжения и toncenter подменяются в памяти.
"""
import asyncio
import logging
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _stub(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


# без установленных зависимостей (как в минимальном окружении тестов) — заглушки; с ними — настоящие модули
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
for name, attrs in (("db", {"SessionLocal": None}),
                    ("models", {k: None for k in ("GramWallet", "GramTx", "GramWithdrawal", "GameSave", "Meta",
                                                  "Referral", "RefEarn", "StarPayment")})):
    try:
        __import__(name)
    except Exception:
        _stub(name, **attrs)

import gram  # noqa: E402

logging.getLogger("gram").setLevel(logging.ERROR)          # предупреждения о догоне в выводе тестов не нужны


def tx(lt, src="EQsender", value=10**9, comment="MW00000001", outs=None, bounced=False):
    msg = {"source": src, "destination": "EQgame", "value": str(value), "message": comment}
    if bounced:
        msg["bounced"] = True
    return {"transaction_id": {"lt": str(lt), "hash": f"h{lt}"}, "in_msg": msg, "out_msgs": outs or []}


class FakeResp:
    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self.data


class FakeToncenter:
    """getTransactions как у toncenter: от новых к старым, с lt+hash — начиная с этой транзакции (включительно)."""

    def __init__(self, chain):
        self.chain = chain                      # список транзакций, самая новая — первая
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        i = 0
        if params.get("hash"):
            i = next(k for k, t in enumerate(self.chain) if t["transaction_id"]["hash"] == params["hash"])
        return FakeResp({"ok": True, "result": self.chain[i:i + int(params["limit"])]})


class DepositProblemTest(unittest.TestCase):
    def test_plain_transfer_is_fine(self):
        self.assertIsNone(gram.deposit_problem(tx(1)))

    def test_bounce_back_to_sender(self):
        t = tx(1, src="EQcheater", outs=[{"source": "EQgame", "destination": "EQcheater", "value": "999000000"}])
        self.assertEqual(gram.deposit_problem(t), "вернулся отправителю")
        self.assertEqual(gram.deposit_info(t)[4], "вернулся отправителю")

    def test_any_outgoing_message_needs_review(self):
        t = tx(1, outs=[{"source": "EQgame", "destination": "EQsomeone", "value": "5"}])
        self.assertIsNotNone(gram.deposit_problem(t))

    def test_bounced_payout_coming_back(self):
        self.assertIsNotNone(gram.deposit_problem(tx(1, comment="", bounced=True)))

    def test_not_incoming_transfers_are_skipped(self):
        self.assertIsNone(gram.deposit_info(tx(1, src="")))          # внешнее сообщение — наша выплата
        self.assertIsNone(gram.deposit_info(tx(1, value=0)))
        value, ref, comment, src, problem = gram.deposit_info(tx(7, comment=" mw0000ab "))
        self.assertEqual((value, ref, comment, problem), (10**9, "dep:h7", "MW0000AB", None))


class CatchUpTest(unittest.TestCase):
    """Очередь длиннее DEP_MAX_PAGES страниц: раньше курсор прыгал на самую новую и старые переводы терялись."""

    def setUp(self):
        self.meta = {"cursor": 0, "resume": None}
        self.processed = []
        self._saved = {k: getattr(gram, k) for k in ("_get_cursor", "_set_cursor", "_get_resume", "_set_resume",
                                                     "process_transactions", "TONCENTER_KEY")}

        async def get_cursor():
            return self.meta["cursor"]

        async def set_cursor(lt):
            self.meta["cursor"] = lt

        async def get_resume():
            return self.meta["resume"]

        async def set_resume(r):
            self.meta["resume"] = r

        async def process(txs):
            self.processed.extend(t["transaction_id"]["hash"] for t in txs)

        gram._get_cursor, gram._set_cursor = get_cursor, set_cursor
        gram._get_resume, gram._set_resume = get_resume, set_resume
        gram.process_transactions = process
        gram.TONCENTER_KEY = "test"                 # без пауз между страницами

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(gram, k, v)

    def run_until_caught_up(self, api, limit=20):
        for _ in range(limit):
            pause = asyncio.run(gram.deposit_step(api))
            if pause == gram.DEP_SLEEP:
                return
        self.fail("очередь не догнана")

    def test_big_backlog_is_processed_completely(self):
        old_cursor = 10_000
        new = [tx(lt) for lt in range(old_cursor + 2600, old_cursor, -1)]          # 2600 новых, самая новая первая
        api = FakeToncenter(new + [tx(lt) for lt in range(old_cursor, old_cursor - 50, -1)])
        self.meta["cursor"] = old_cursor
        self.run_until_caught_up(api)
        self.assertEqual(sorted(self.processed), sorted(t["transaction_id"]["hash"] for t in new))
        self.assertEqual(len(self.processed), len(set(self.processed)))           # каждая — один раз
        self.assertEqual(self.meta["cursor"], old_cursor + 2600)
        self.assertIsNone(self.meta["resume"])

    def test_transfers_arriving_during_catch_up_are_not_lost(self):
        old_cursor = 500
        new = [tx(lt) for lt in range(old_cursor + 1500, old_cursor, -1)]
        api = FakeToncenter(new + [tx(old_cursor)])
        self.meta["cursor"] = old_cursor
        pause = asyncio.run(gram.deposit_step(api))
        self.assertEqual(pause, gram.DEP_SLEEP_CATCHUP)                           # не дочитали — курсор на месте
        self.assertEqual(self.meta["cursor"], old_cursor)
        self.assertIsNotNone(self.meta["resume"])
        later = [tx(lt) for lt in range(old_cursor + 1510, old_cursor + 1500, -1)]  # пришли, пока догоняли
        api.chain = later + api.chain
        self.run_until_caught_up(api)
        asyncio.run(gram.deposit_step(api))                                        # обычный круг подбирает новые
        self.assertEqual(sorted(self.processed), sorted(t["transaction_id"]["hash"] for t in later + new))
        self.assertEqual(self.meta["cursor"], old_cursor + 1510)

    def test_normal_round_and_first_run(self):
        api = FakeToncenter([tx(lt) for lt in range(300, 200, -1)])
        self.assertEqual(asyncio.run(gram.deposit_step(api)), gram.DEP_SLEEP)    # первый запуск: одна страница
        self.assertEqual(len(self.processed), gram.DEP_PAGE)
        self.assertEqual(self.meta["cursor"], 300)
        api.chain = [tx(lt) for lt in range(305, 300, -1)] + api.chain
        self.processed.clear()
        asyncio.run(gram.deposit_step(api))
        self.assertEqual(self.processed, [f"h{lt}" for lt in range(305, 300, -1)])
        self.assertEqual(self.meta["cursor"], 305)


if __name__ == "__main__":
    unittest.main()
