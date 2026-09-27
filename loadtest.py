"""Нагрузочный тест живого мира MetalWar: N ботов по WebSocket + фоновая HTTP-нагрузка.

ТОЛЬКО для staging. На сервере должны быть ENV_NAME=staging и LOADTEST=1, а у теста — токен
ТЕСТОВОГО бота (тот же BOT_TOKEN, что у staging-сервера): им тест сам подписывает initData.
Боты получают выдуманные id от 9_100_000_000_000, настоящих игроков это не касается.

Пример:
  pip install aiohttp
  python tools/loadtest.py --url https://metalwar-staging.up.railway.app --bot-token $STAGING_BOT_TOKEN \
      --players 100 --ramp 120 --duration 1500 --metrics-token $METRICS_TOKEN
Худший случай (все в одной локации):  добавить --one-loc
Короткая проверка:  --players 25 --ramp 10 --duration 60

Что меряется (от отправки одним ботом до получения другим, часы общие — всё в одном процессе):
  chat_world, chat_dm, pvp_hit — p50/p95/p99, плюс успешность подключений и неожиданные разрывы.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import random
import time
import urllib.parse
from collections import defaultdict

import aiohttp

UID_BASE = 9_100_000_000_000
PVP_LOC = "sector1"
LOCS = ["lobby", "sector1", "sector2", "scrapfields", "reactor_ruins", "iron_canyon"]


def init_data(bot_token, uid, username):
    """Подпись initData так же, как её делает Telegram (HMAC-SHA256 с ключом от токена бота)."""
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": f"LT{uid}",
        "user": json.dumps({"id": uid, "first_name": "LT", "username": username}, separators=(",", ":")),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


class Stats:
    def __init__(self):
        self.lat = defaultdict(list)
        self.sent = {}                    # ключ события -> время отправки
        self.counts = defaultdict(int)
        self.close_codes = defaultdict(int)

    def mark(self, key):
        self.sent[key] = time.perf_counter()

    def got(self, kind, key, record=True):
        t = self.sent.get(key)
        if t is not None and record:
            self.lat[kind].append((time.perf_counter() - t) * 1000)

    @staticmethod
    def pct(vals, p):
        if not vals:
            return None
        s = sorted(vals)
        return round(s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))], 1)

    def report(self):
        out = {}
        for k, v in self.lat.items():
            out[k] = {"n": len(v), "p50": self.pct(v, 50), "p95": self.pct(v, 95), "p99": self.pct(v, 99), "max": round(max(v), 1)}
        return out


class Bot:
    def __init__(self, n, args, stats, session, roster):
        self.n, self.args, self.stats, self.http, self.roster = n, args, stats, session, roster
        self.uid = UID_BASE + n
        self.nick = f"lt{n:03d}"
        self.slow = False
        self.stuck = False
        self.fighter_of = None           # uid соперника
        self.loc = PVP_LOC if args.one_loc else random.choice(LOCS)
        self.x, self.y = random.uniform(100, 2000), random.uniform(100, 2000)
        self.ws = None
        self.hits_taken = 0
        self.alive = False

    async def run(self, stop_at):
        a = self.args
        try:
            self.ws = await self.http.ws_connect(a.ws_url, heartbeat=25, max_msg_size=0, timeout=10)
        except Exception as e:
            self.stats.counts["connect_fail"] += 1
            print(f"[{self.nick}] не подключился: {e}")
            return
        await self.ws.send_str(json.dumps({"t": "auth", "initData": init_data(a.bot_token, self.uid, self.nick)}))
        msg = await self.ws.receive(timeout=10)
        if msg.type != aiohttp.WSMsgType.TEXT or json.loads(msg.data).get("t") != "hello":
            self.stats.counts["auth_fail"] += 1
            print(f"[{self.nick}] нет hello: {msg.type} {str(msg.data)[:80]}")
            await self.ws.close()
            return
        self.stats.counts["connected"] += 1
        self.alive = True
        tasks = [asyncio.create_task(self.sender(stop_at))]
        if not self.stuck:
            tasks.append(asyncio.create_task(self.reader()))
        try:
            await asyncio.wait(tasks, timeout=max(0, stop_at - time.time()) + 2, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.alive = False
            for t in tasks:
                t.cancel()
            code = self.ws.close_code
            if self.ws.closed and time.time() < stop_at - 1:
                self.stats.close_codes[code] += 1
                self.stats.counts["unexpected_close"] += 1
            await self.ws.close()

    async def sender(self, stop_at):
        a = self.args
        seq = 0
        next_pos = next_hit = time.time()
        next_chat = time.time() + random.uniform(0, a.players / max(a.chat_rate, 0.01))
        next_dm = time.time() + random.uniform(0, a.players / max(a.dm_rate, 0.01))
        while time.time() < stop_at and not self.ws.closed:
            now = time.time()
            if now >= next_pos:                                   # позиция: pos_rate сообщений в секунду
                next_pos = now + 1 / a.pos_rate * random.uniform(0.8, 1.2)
                if not self.fighter_of:
                    self.x = min(7900, max(50, self.x + random.uniform(-40, 40)))
                    self.y = min(7900, max(50, self.y + random.uniform(-40, 40)))
                await self.ws.send_str(json.dumps({"t": "pos", "loc": self.loc, "x": self.x, "y": self.y, "ang": 0.5, "aim": 0.5,
                                                   "moving": True, "nick": self.nick, "fac": "aegis", "lvl": 20, "hp": 500, "mhp": 500,
                                                   "cp": 100, "mcp": 100, "bm": 1000, "eq": {"weapon": 1}, "wpn": "gun", "cls": "guard"}))
            if self.fighter_of and now >= next_hit:               # удар: ~1,5 в секунду
                next_hit = now + 0.66
                seq += 1
                dmg = 1 + seq % 150                     # не выше серверного предела урона для 20 ур.
                self.stats.mark(("pvp", self.uid, self.fighter_of, dmg))
                await self.ws.send_str(json.dumps({"t": "pvp", "to": self.fighter_of, "dmg": dmg}))
            if now >= next_chat:                                  # мировой чат: суммарно chat_rate в секунду
                next_chat = now + a.players / a.chat_rate * random.uniform(0.7, 1.3)
                seq += 1
                token = f"lt:{self.uid}:{seq}"
                self.stats.mark(("chat", token))
                await self.ws.send_str(json.dumps({"t": "chat", "ch": "world", "text": token, "cid": token}))
            if now >= next_dm and self.roster:                    # личные: суммарно dm_rate в секунду
                next_dm = now + a.players / a.dm_rate * random.uniform(0.7, 1.3)
                other = random.choice(self.roster)
                if other is not self:
                    seq += 1
                    token = f"dm:{self.uid}:{seq}"
                    self.stats.mark(("dm", token))
                    await self.ws.send_str(json.dumps({"t": "chat", "ch": "dm", "text": token, "to": other.nick, "to_uid": other.uid}))
            await asyncio.sleep(0.02)

    async def reader(self):
        record = not self.slow                   # задержки медленных клиентов в SLO не входят
        while True:
            if self.slow:
                await asyncio.sleep(random.uniform(0.5, 1.5))     # «плохая сеть»: читаем пачками
            msg = await self.ws.receive()
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            self.stats.counts["msgs_in"] += 1
            self.stats.counts["bytes_in"] += len(msg.data)
            d = json.loads(msg.data)
            t = d.get("t")
            if t == "players":
                self.stats.counts["snapshots"] += 1
            elif t == "chat":
                m = d.get("m") or {}
                text = m.get("text", "")
                if m.get("ch") == "world" and m.get("uid") != str(self.uid):
                    self.stats.got("chat_world", ("chat", text), record)
                elif m.get("ch") == "dm" and m.get("uid") != str(self.uid):
                    self.stats.got("chat_dm", ("dm", text), record)
            elif t == "pvp_hit":
                self.stats.got("pvp_hit", ("pvp", d.get("from"), self.uid, d.get("dmg")), record)
                self.hits_taken += 1
                if self.hits_taken % 30 == 0:                     # иногда «умираем» — проверяем запись итога боя в базу
                    await self.ws.send_str(json.dumps({"t": "pvp_dead", "by": d.get("from")}))
            elif t in ("pvp_kill", "pvp_died"):
                self.stats.counts[t] += 1


async def http_load(args, session, stop_at, stats):
    """Фон: сохранения и рейтинг PvP через HTTP API."""
    async def post(path, body, kind):
        t0 = time.perf_counter()
        try:
            async with session.post(args.http_url + path, json=body, timeout=aiohttp.ClientTimeout(total=10)) as r:
                await r.read()
                stats.counts[f"http_{r.status}"] += 1
        except Exception:
            stats.counts["http_error"] += 1
        stats.lat[kind].append((time.perf_counter() - t0) * 1000)

    while time.time() < stop_at:
        n = random.randrange(args.players)
        uid, nick = UID_BASE + n, f"lt{n:03d}"
        idata = init_data(args.bot_token, uid, nick)
        if random.random() < args.save_rate / (args.save_rate + args.top_rate):
            asyncio.create_task(post("/api/state/save", {"initData": idata, "data": {"S": {"level": 20, "name": nick}, "bm": 1000}}, "http_save"))
        else:
            asyncio.create_task(post("/api/pvp/top", {"initData": idata}, "http_pvp_top"))
        await asyncio.sleep(1 / (args.save_rate + args.top_rate))


async def loop_lag(stats, stop_at):
    """Лаг самого теста: если он большой, замеры задержек завышены по вине генератора."""
    loop = asyncio.get_running_loop()
    while time.time() < stop_at:
        t0 = loop.time()
        await asyncio.sleep(0.1)
        stats.lat["tester_loop_lag"].append((loop.time() - t0 - 0.1) * 1000)


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="адрес staging, например https://metalwar-staging.up.railway.app")
    p.add_argument("--bot-token", required=True, help="токен ТЕСТОВОГО бота (как BOT_TOKEN на staging)")
    p.add_argument("--players", type=int, default=100)
    p.add_argument("--ramp", type=float, default=120, help="секунд на подключение всех ботов")
    p.add_argument("--duration", type=float, default=1500, help="секунд полной нагрузки после набора")
    p.add_argument("--pairs", type=int, default=20, help="сражающихся пар")
    p.add_argument("--pos-rate", type=float, default=1.0, help="pos в секунду на бота")
    p.add_argument("--chat-rate", type=float, default=10, help="сообщений мирового чата в секунду суммарно")
    p.add_argument("--dm-rate", type=float, default=10, help="личных сообщений в секунду суммарно")
    p.add_argument("--save-rate", type=float, default=5)
    p.add_argument("--top-rate", type=float, default=2)
    p.add_argument("--slow-share", type=float, default=0.1, help="доля ботов с плохой сетью")
    p.add_argument("--stuck", type=int, default=1, help="сколько ботов совсем перестают читать")
    p.add_argument("--one-loc", action="store_true", help="худший случай: все в одной локации")
    p.add_argument("--metrics-token", default="", help="METRICS_TOKEN сервера, чтобы приложить его метрики")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default="loadtest-report.json")
    args = p.parse_args()
    random.seed(args.seed)
    base = args.url.rstrip("/")
    args.http_url = base
    args.ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/ws"

    stats = Stats()
    conn = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=conn) as session:
        bots = []
        for n in range(args.players):
            bots.append(Bot(n, args, stats, session, None))
        for b in bots:
            b.roster = bots
        for i in range(min(args.pairs, args.players // 2)):              # пары бойцов стоят рядом в PvP-локации
            a, b = bots[2 * i], bots[2 * i + 1]
            a.loc = b.loc = PVP_LOC
            a.x, a.y = 300 + (i % 10) * 600, 300 + (i // 10) * 600
            b.x, b.y = a.x + 150, a.y
            a.fighter_of, b.fighter_of = b.uid, a.uid
        others = bots[2 * min(args.pairs, args.players // 2):]
        for b in others[:int(len(bots) * args.slow_share)]:
            b.slow = True
        for b in others[-args.stuck:] if args.stuck else []:
            b.stuck = True

        t_start = time.time()
        stop_at = t_start + args.ramp + args.duration
        print(f"Старт: {args.players} ботов, набор {args.ramp:.0f} с, нагрузка {args.duration:.0f} с, "
              f"{'одна локация' if args.one_loc else 'разные локации'}")
        tasks = [asyncio.create_task(loop_lag(stats, stop_at)), asyncio.create_task(http_load(args, session, stop_at, stats))]
        for i, b in enumerate(bots):
            tasks.append(asyncio.create_task(b.run(stop_at)))
            await asyncio.sleep(args.ramp / max(1, args.players))

        async def progress():
            while time.time() < stop_at:
                await asyncio.sleep(15)
                r = stats.report()
                online = sum(1 for b in bots if b.alive)
                print(f"[{int(time.time() - t_start):>5} с] в сети {online}, "
                      + ", ".join(f"{k} p95={v['p95']}" for k, v in r.items() if k in ("chat_world", "pvp_hit", "chat_dm")))
        tasks.append(asyncio.create_task(progress()))
        await asyncio.gather(*tasks, return_exceptions=True)

        server = None
        if args.metrics_token:
            try:
                async with session.get(base + "/metrics", headers={"X-Metrics-Token": args.metrics_token}) as r:
                    server = await r.json()
            except Exception as e:
                print("Метрики сервера недоступны:", e)

    report = {"args": {k: v for k, v in vars(args).items() if k != "bot_token"}, "latency_ms": stats.report(),
              "counts": dict(stats.counts), "close_codes": {str(k): v for k, v in stats.close_codes.items()}, "server": server}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    slo = {"chat_world": (200, 400), "chat_dm": (200, 400), "pvp_hit": (120, 250)}
    print("\n=== Итог ===")
    for k, (p95, p99) in slo.items():
        r = report["latency_ms"].get(k)
        if not r:
            print(f"{k}: нет данных")
            continue
        ok = r["p95"] <= p95 and r["p99"] <= p99
        print(f"{k}: n={r['n']} p50={r['p50']} p95={r['p95']} p99={r['p99']} мс  {'OK' if ok else 'НЕ ВЫПОЛНЕНО'} (цель p95≤{p95}, p99≤{p99})")
    c = stats.counts
    total = args.players
    print(f"подключились {c['connected']}/{total}, неожиданных разрывов {c['unexpected_close']} "
          f"(ожидаемо: {args.stuck} «зависших»), коды: {dict(stats.close_codes)}")
    lag = report["latency_ms"].get("tester_loop_lag", {})
    if lag.get("p99", 0) > 50:
        print(f"ВНИМАНИЕ: сам генератор лагает (p99 {lag['p99']} мс) — запусти его на машине помощнее или уменьши число ботов")
    if server:
        sl = server.get("latency_ms", {}).get("loop_lag", {})
        print(f"сервер: event loop lag p99={sl.get('p99')} мс, тик мира p99={server.get('latency_ms', {}).get('world.tick', {}).get('p99')} мс")
    print(f"Полный отчёт: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
