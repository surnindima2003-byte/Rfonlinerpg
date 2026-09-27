"""Проверка очередей отправки: медленный клиент не тормозит остальных, переполнение отключает только его.

Запуск: python -m pytest tests/test_realtime.py   (или python -m unittest tests.test_realtime)
"""
import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("WS_SEND_TIMEOUT", "0.5")

import realtime  # noqa: E402


class FakeWS:
    def __init__(self, delay=0.0, stuck=False):
        self.delay, self.stuck = delay, stuck
        self.sent = []
        self.closed = False
        self.close_code = None

    async def send_str(self, text):
        if self.stuck:
            await asyncio.sleep(3600)          # клиент перестал читать: отправка висит
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append((time.monotonic(), text))

    async def close(self, code=1000, message=b""):
        self.closed, self.close_code = True, code


def run(coro):
    return asyncio.run(coro)


class RealtimeTest(unittest.TestCase):
    def test_slow_client_does_not_delay_others(self):
        async def main():
            hub = realtime.Hub()
            fast = [FakeWS() for _ in range(50)]
            stuck = FakeWS(stuck=True)
            for i, ws in enumerate(fast):
                hub.add(ws, {"id": i, "loc": "sector1"})
            hub.add(stuck, {"id": 999, "loc": "sector1"})
            t0 = time.monotonic()
            hub.to_all({"t": "chat", "m": {"text": "hi"}})
            await asyncio.sleep(0.05)
            for ws in fast:
                self.assertEqual(len(ws.sent), 1)
                self.assertLess(ws.sent[0][0] - t0, 0.05)
            await asyncio.sleep(0.6)                    # таймаут отправки медленному клиенту
            self.assertTrue(stuck.closed)
            self.assertEqual(stuck.close_code, realtime.CLOSE_SLOW)
            for ws in list(hub.conns):
                hub.remove(ws)
        run(main())

    def test_overflow_closes_only_that_client(self):
        async def main():
            hub = realtime.Hub()
            slow, ok = FakeWS(stuck=True), FakeWS()
            c_slow = hub.add(slow, {"id": 1, "loc": "a"})
            hub.add(ok, {"id": 2, "loc": "a"})
            for i in range(realtime.MAX_Q_MSGS + 5):
                hub.to_all({"t": "chat", "i": i})
                if i % 50 == 0:
                    await asyncio.sleep(0)               # между сообщениями отправители успевают работать
            await asyncio.sleep(0.05)
            self.assertTrue(c_slow.closing)
            self.assertEqual(len(ok.sent), realtime.MAX_Q_MSGS + 5)
            for ws in list(hub.conns):
                hub.remove(ws)
        run(main())

    def test_snapshots_coalesce_and_reliable_first(self):
        async def main():
            hub = realtime.Hub()
            ws = FakeWS(delay=0.02)
            c = hub.add(ws, {"id": 1, "loc": "a"})
            for i in range(10):
                c.push_snapshot(f"snap{i}")
            c.push('{"t":"chat"}')
            await asyncio.sleep(0.2)
            texts = [t for _, t in ws.sent]
            self.assertIn('{"t":"chat"}', texts)
            snaps = [t for t in texts if t.startswith("snap")]
            self.assertLessEqual(len(snaps), 2)          # промежуточные снимки выброшены
            self.assertEqual(snaps[-1], "snap9")         # последний всегда доходит
            c.push_snapshot("snap9")                     # тот же снимок повторно не шлём
            await asyncio.sleep(0.05)
            self.assertEqual([t for _, t in ws.sent].count("snap9"), 1)
            hub.remove(ws)
        run(main())

    def test_indexes(self):
        async def main():
            hub = realtime.Hub(max_per_uid=2)
            a, b, c3 = FakeWS(), FakeWS(), FakeWS()
            ca = hub.add(a, {"id": 7, "loc": "lobby"})
            hub.add(b, {"id": 7, "loc": "lobby"})
            hub.add(c3, {"id": 7, "loc": "lobby"})       # третья вкладка вытесняет первую
            self.assertTrue(ca.closing)
            self.assertEqual(len(hub.by_uid[7]), 2)
            cb = hub.conns[b]
            cb.info["loc"] = "sector1"
            hub.moved(cb, "lobby")
            self.assertIn(cb, hub.by_loc["sector1"])
            self.assertNotIn(cb, hub.by_loc["lobby"])
            for ws in (a, b, c3):
                hub.remove(ws)
            self.assertEqual(hub.by_uid, {})
            self.assertEqual(hub.by_loc, {})
        run(main())


if __name__ == "__main__":
    unittest.main()
