"""Лёгкие метрики сервера без внешних зависимостей.

Счётчики, выборки задержек (p50/p95/p99 по последним N замерам) и задержка event loop.
Отдаются в JSON через /metrics (см. webserver.py), доступ по METRICS_TOKEN.
"""
import asyncio
import time
from collections import defaultdict, deque

SAMPLES = 2048              # сколько последних замеров держим на каждую метрику

counters = defaultdict(int)             # имя -> число
gauges = {}                             # имя -> значение (последнее)
_samples = defaultdict(lambda: deque(maxlen=SAMPLES))   # имя -> deque миллисекунд
started = time.time()


def inc(name, n=1):
    counters[name] += n


def gauge(name, value):
    gauges[name] = value


def observe(name, ms):
    _samples[name].append(ms)


class timer:
    """with metrics.timer("sql"): ...  — пишет длительность блока в миллисекундах."""
    __slots__ = ("name", "t0")

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        observe(self.name, (time.perf_counter() - self.t0) * 1000)
        return False


def _pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = min(len(sorted_vals) - 1, int(round(p / 100 * (len(sorted_vals) - 1))))
    return round(sorted_vals[k], 2)


def summary():
    lat = {}
    for name, dq in list(_samples.items()):
        vals = sorted(dq)
        lat[name] = {"n": len(vals), "p50": _pct(vals, 50), "p95": _pct(vals, 95), "p99": _pct(vals, 99),
                     "max": round(vals[-1], 2) if vals else 0.0}
    return {"uptime_s": int(time.time() - started), "counters": dict(counters), "gauges": dict(gauges), "latency_ms": lat}


async def loop_lag_monitor(interval=0.1):
    """Каждые 100 мс проверяем, насколько позже запланированного проснулся цикл: это и есть лаг event loop."""
    loop = asyncio.get_running_loop()
    while True:
        t0 = loop.time()
        await asyncio.sleep(interval)
        lag = (loop.time() - t0 - interval) * 1000
        observe("loop_lag", max(0.0, lag))
