"""Соединения живого мира: очередь отправки на каждого игрока, индексы по uid и локации.

Главное правило: обработчики НИКОГДА не ждут физическую отправку. Они кладут сообщение
в очередь соединения и сразу идут дальше, а отдельная задача-отправитель у каждого
сокета отдаёт очередь в сеть. Медленный клиент тормозит только сам себя, а если
отстал слишком сильно, его отключают.

Два класса сообщений:
  * надёжные (чат, удары, пати, системные) — FIFO, не теряются, пока клиент успевает;
  * снимки мира — «последний побеждает»: неотправленный старый снимок заменяется новым.
"""
import asyncio
import json
import logging
import math
import os
import time
from collections import deque

import metrics
from config import env_int, env_float      # пустая переменная в Railway не роняет сервер

log = logging.getLogger("realtime")

MAX_Q_MSGS = env_int("WS_MAX_QUEUE_MSGS", 400)          # надёжных сообщений в очереди
MAX_Q_BYTES = env_int("WS_MAX_QUEUE_BYTES", 512 * 1024)
SEND_TIMEOUT = env_float("WS_SEND_TIMEOUT", 10)           # столько ждём одну отправку медленному клиенту

CLOSE_SLOW = 4008        # клиент не успевает читать
CLOSE_REPLACED = 4003    # слишком много вкладок одного игрока
CLOSE_RESTART = 1012     # сервер перезапускается — клиенту переподключиться


# ---- разбор входящего JSON ----
# Стандартный json.loads принимает NaN, Infinity и 1e999 (это уже бесконечность). Такое число, попав в данные
# игрока, уходит другим в снимке мира, и JSON.parse у них падает: весь мир в локации «замирает».
# Честный клиент таких чисел не шлёт никогда (JSON.stringify превращает NaN/Infinity в null),
# поэтому сообщение с ними просто отбрасываем.
MAX_INT_DIGITS = 30           # больше — мусор: float() от такого числа ещё конечен, а int в 300+ цифр уже нет


def _reject_constant(name):
    raise ValueError(f"non-finite number: {name}")


def _finite_float(text):
    v = float(text)
    if not math.isfinite(v):
        raise ValueError("non-finite number")
    return v


def _small_int(text):
    if len(text.lstrip("-")) > MAX_INT_DIGITS:
        raise ValueError("number too long")
    return int(text)


def loads(text):
    """json.loads без NaN, Infinity и чисел-гигантов. Ошибка — ValueError (как у обычного json.loads)."""
    return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float, parse_int=_small_int)


def _finite(v):
    """Копия данных, где NaN и бесконечности заменены нулём (запасной путь для encode)."""
    if isinstance(v, float):
        return v if math.isfinite(v) else 0
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_finite(x) for x in v]
    return v


def encode(payload):
    """Один раз превращаем сообщение в строку — дальше рассылаем готовую строку всем.
    NaN/Infinity в строку не попадают никогда: их не понимает JSON.parse в браузере."""
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except ValueError:
        metrics.inc("ws.encode_nonfinite")
        log.warning("в исходящем сообщении NaN/Infinity, заменены нулём: t=%s",
                    payload.get("t") if isinstance(payload, dict) else type(payload).__name__)
        return json.dumps(_finite(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Conn:
    __slots__ = ("ws", "info", "q", "q_bytes", "snap", "wake", "task", "closing", "opened", "last_snap",
                 "snap_view", "sent_view", "delta")

    def __init__(self, ws, info):
        self.ws = ws
        self.info = info
        self.q = deque()         # (время постановки, строка)
        self.q_bytes = 0
        self.snap = None         # последний неотправленный снимок мира
        self.last_snap = None    # последний отправленный снимок (чтобы не слать одинаковые)
        # протокол дельт: клиент получает только изменившихся соседей.
        # sent_view — то, что клиент ДЕЙСТВИТЕЛЬНО получил (id -> строка игрока); меняется только при отправке,
        # поэтому снимок, вытесненный новым до отправки, ничего не теряет: новая дельта считается от sent_view.
        self.snap_view = None
        self.sent_view = None
        self.delta = bool(info.get("proto", 0) >= 2)
        self.wake = asyncio.Event()
        self.closing = False
        self.opened = time.monotonic()
        self.task = asyncio.create_task(self._sender())

    @property
    def uid(self):
        return self.info.get("id")

    # ---- постановка в очередь ----
    def push(self, text):
        """Надёжное сообщение. False — соединение закрыто или переполнено (и тогда закрывается)."""
        if self.closing:
            return False
        if len(self.q) >= MAX_Q_MSGS or self.q_bytes + len(text) > MAX_Q_BYTES:
            metrics.inc("ws.overflow_close")
            self.close(CLOSE_SLOW, "slow consumer")
            return False
        self.q.append((time.monotonic(), text))
        self.q_bytes += len(text)
        self.wake.set()
        return True

    def push_snapshot(self, text, keepalive=False, view=None):
        """Снимок мира: заменяет неотправленный старый. Одинаковые подряд не шлём (кроме keepalive).
        view — полное состояние, которое клиент будет знать после этой отправки (для дельт)."""
        if self.closing:
            return
        if not keepalive and text == self.last_snap and self.snap is None:
            metrics.inc("world.snap_skipped")
            return
        if self.snap is not None:
            metrics.inc("world.snap_coalesced")
        self.snap = text
        self.snap_view = view
        self.wake.set()

    def reset_view(self):
        """Клиент сменил локацию или переподключился — следующая дельта будет полной."""
        self.sent_view = None
        self.last_snap = None

    # ---- отправитель ----
    async def _sender(self):
        ws = self.ws
        idx = 0
        try:
            while not self.closing:
                await self.wake.wait()
                self.wake.clear()
                while (self.q or self.snap is not None) and not self.closing:
                    if self.q:
                        t_in, text = self.q[0]
                        await self._send(text)
                        self.q.popleft()
                        self.q_bytes -= len(text)
                        metrics.observe("ws.queue_wait", (time.monotonic() - t_in) * 1000)
                    else:
                        text, self.snap = self.snap, None
                        view, self.snap_view = self.snap_view, None
                        if view is not None:
                            # считаем отправленным уже в момент отправки: дельта, собранная, пока эта
                            # строка идёт в сеть, должна считаться от неё (иначе потеряется «ушёл»)
                            self.sent_view = view
                        await self._send(text)
                        self.last_snap = text
                    idx += 1
                    if idx % 64 == 0:
                        await asyncio.sleep(0)       # не держим цикл, если очередь длинная
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            metrics.inc("ws.send_timeout_close")
            self.close(CLOSE_SLOW, "send timeout")
        except Exception as e:                       # сокет оборвался во время отправки
            metrics.inc("ws.send_error")
            log.debug("send error uid=%s: %s", self.uid, e)
            self.close(1011, "send error")

    async def _send(self, text):
        if self.ws.closed:
            raise ConnectionResetError("closed")
        await asyncio.wait_for(self.ws.send_str(text), SEND_TIMEOUT)
        metrics.inc("ws.msgs_out")
        metrics.inc("ws.bytes_out", len(text))

    def close(self, code=1000, message="bye"):
        """Закрыть без ожидания (можно звать из синхронного кода)."""
        if self.closing:
            return
        self.closing = True
        self.wake.set()
        metrics.inc(f"ws.close.{code}")
        if not self.ws.closed:
            asyncio.create_task(_safe_close(self.ws, code, message))

    async def drain(self, timeout):
        """Дать надёжной очереди уйти в сеть (при остановке сервера)."""
        end = time.monotonic() + timeout
        while self.q and not self.ws.closed and time.monotonic() < end:
            await asyncio.sleep(0.05)

    def stop(self):
        self.closing = True
        self.wake.set()
        if self.task and not self.task.done():
            self.task.cancel()


async def _safe_close(ws, code, message):
    try:
        await asyncio.wait_for(ws.close(code=code, message=message.encode()), 5)
    except Exception:
        pass


class Hub:
    """Все подключённые игроки и индексы, чтобы не перебирать всех на каждом шаге."""

    def __init__(self, max_per_uid=3):
        self.conns = {}          # ws -> Conn
        self.by_uid = {}         # uid -> [Conn] (в порядке подключения)
        self.by_loc = {}         # loc -> set(Conn)
        self.max_per_uid = max_per_uid

    # ---- жизненный цикл ----
    def add(self, ws, info):
        c = Conn(ws, info)
        self.conns[ws] = c
        lst = self.by_uid.setdefault(c.uid, [])
        lst.append(c)
        while len(lst) > self.max_per_uid:           # самая старая вкладка уступает место
            old = lst.pop(0)
            old.close(CLOSE_REPLACED, "too many connections")
        self.by_loc.setdefault(info.get("loc"), set()).add(c)
        metrics.gauge("ws.authed", len(self.conns))
        return c

    def remove(self, ws):
        c = self.conns.pop(ws, None)
        if not c:
            return None
        c.stop()
        lst = self.by_uid.get(c.uid)
        if lst and c in lst:
            lst.remove(c)
            if not lst:
                self.by_uid.pop(c.uid, None)
        loc_set = self.by_loc.get(c.info.get("loc"))
        if loc_set:
            loc_set.discard(c)
            if not loc_set:
                self.by_loc.pop(c.info.get("loc"), None)
        metrics.gauge("ws.authed", len(self.conns))
        return c

    def moved(self, c, old_loc):
        """Игрок сменил локацию — переносим в индексе."""
        new_loc = c.info.get("loc")
        if new_loc == old_loc:
            return
        s = self.by_loc.get(old_loc)
        if s:
            s.discard(c)
            if not s:
                self.by_loc.pop(old_loc, None)
        self.by_loc.setdefault(new_loc, set()).add(c)
        c.reset_view()

    # ---- поиск ----
    def info_of(self, uid):
        lst = self.by_uid.get(uid)
        return lst[-1].info if lst else None

    def is_online(self, uid):
        return uid in self.by_uid

    # ---- рассылка (не ждёт сеть) ----
    def to_uid(self, uid, payload):
        lst = self.by_uid.get(uid)
        if not lst:
            return False
        text = payload if isinstance(payload, str) else encode(payload)
        ok = False
        for c in list(lst):
            ok = c.push(text) or ok
        return ok

    def to_all(self, payload, only=None):
        text = encode(payload)
        n = 0
        for c in list(self.conns.values()):
            if only is None or only(c.info):
                n += c.push(text)
        return n

    def to_loc(self, loc, payload, skip_uid=None):
        text = encode(payload)
        for c in list(self.by_loc.get(loc, ())):
            if c.uid != skip_uid:
                c.push(text)

    def gauges(self):
        worst = 0
        oldest = 0.0
        now = time.monotonic()
        for c in self.conns.values():
            worst = max(worst, len(c.q))
            if c.q:
                oldest = max(oldest, now - c.q[0][0])
        metrics.gauge("ws.queue_max_len", worst)
        metrics.gauge("ws.queue_oldest_ms", round(oldest * 1000, 1))
        metrics.gauge("ws.authed", len(self.conns))
        metrics.gauge("ws.locations", {k: len(v) for k, v in self.by_loc.items()})
