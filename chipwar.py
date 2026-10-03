"""Chip War — битва трёх фракций за чип в центре Железного каньона (в духе RF Online).

Как идёт бой: каждую секунду сервер смотрит, кто стоит в круге чипа. Если внутри живые пилоты
(10+ уровня) только одной фракции — она удерживает чип и получает очки (+1 за каждого, до +3).
Если в круге несколько фракций — чип оспаривается, очки не идут никому. Побеждает фракция,
первой набравшая TARGET очков, или та, у которой больше очков к концу времени.

Награда: победившая фракция на BUFF_HOURS часов получает бонус к шансу ценного лута (сервер)
и к опыту (игра). Участники (не меньше REWARD_MIN_SEC секунд в круге) получают лом и ядра.
GRAM за событие не даётся намеренно: реальные деньги за «постой в круге» стали бы целью для ботов.

Всё считает сервер по серверным данным: положению, уровню и закреплённой фракции игрока.
"""
import json
import logging
import os
import time
from config import env_int, env_float      # пустая переменная в Railway не роняет сервер

log = logging.getLogger("chipwar")

LOC = os.getenv("CHIPWAR_LOC", "iron_canyon")
ZONE = (env_float("CHIPWAR_X", 1100), env_float("CHIPWAR_Y", 1100), env_float("CHIPWAR_R", 190))
# расписание по московскому времени: «день ЧЧ:ММ» через запятую
SCHEDULE = os.getenv("CHIPWAR_SCHEDULE", "tue 19:00, sat 18:00")
TZ_OFFSET = env_int("CHIPWAR_TZ_OFFSET", 3)          # часы от UTC
DURATION = env_int("CHIPWAR_MINUTES", 20) * 60
TARGET = env_int("CHIPWAR_TARGET", 900)
MIN_LVL = 10                                                  # как защита новичков в PvP
PER_PLAYER_MAX = 3                                            # больше трёх в круге очки не ускоряют
BUFF_HOURS = 24
BUFF_LOOT = 1.15
BUFF_EXP = 0.10
REWARD_MIN_SEC = 60
FACTIONS = ("aegis", "vex", "core")
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_schedule(text):
    """'tue 19:00, sat 18:00' -> [(1, 19, 0), (5, 18, 0)]. Ошибочные части пропускаются."""
    out = []
    for part in (text or "").split(","):
        try:
            day, hm = part.strip().lower().split()
            h, m = hm.split(":")
            if day in DAYS and 0 <= int(h) < 24 and 0 <= int(m) < 60:
                out.append((DAYS[day], int(h), int(m)))
        except ValueError:
            continue
    return out


def next_start(now, schedule=None, tz=TZ_OFFSET):
    """Ближайшее время начала по расписанию (UTC, секунды) строго после now. None — расписание пустое."""
    slots = parse_schedule(SCHEDULE if schedule is None else schedule)
    if not slots:
        return None
    local = now + tz * 3600
    day0 = int(local // 86400) * 86400                     # полночь по местному времени
    weekday = (int(local // 86400) + 3) % 7                # 1970-01-01 — четверг (3)
    best = None
    for add in range(0, 8):
        wd = (weekday + add) % 7
        for d, h, m in slots:
            if d != wd:
                continue
            t = day0 + add * 86400 + h * 3600 + m * 60 - tz * 3600
            if t > now and (best is None or t < best):
                best = t
    return best


class War:
    """Состояние одного события. Без сети и базы — чтобы логику можно было проверить тестами."""

    def __init__(self):
        self.phase = "idle"          # idle | live
        self.start = 0.0
        self.end = 0.0
        self.sc = {f: 0 for f in FACTIONS}
        self.hold = None             # фракция, удерживающая чип, "contested" или None
        self.part = {}               # uid -> {"fac", "sec", "lvl"}
        self.winner = None

    def begin(self, now, duration=DURATION):
        self.__init__()
        self.phase, self.start, self.end = "live", now, now + duration

    def in_zone(self, info):
        x, y, r = ZONE
        return (info.get("x", -1e9) - x) ** 2 + (info.get("y", -1e9) - y) ** 2 <= r * r

    def eligible(self, info):
        return (info.get("loc") == LOC and not info.get("dead") and (info.get("lvl") or 1) >= MIN_LVL
                and info.get("fac") in FACTIONS and self.in_zone(info))

    def tick(self, infos):
        """Одна секунда боя. infos — данные игроков в локации. True — кто-то набрал TARGET."""
        if self.phase != "live":
            return False
        by_fac = {}
        for info in infos:
            if not self.eligible(info):
                continue
            uid = info["id"]
            p = self.part.get(uid)
            if p is None:
                p = self.part[uid] = {"fac": info["fac"], "sec": 0, "lvl": info.get("lvl") or 1}
            p["lvl"] = max(p["lvl"], info.get("lvl") or 1)
            by_fac.setdefault(p["fac"], set()).add(uid)     # фракция закреплена на всё событие
        if len(by_fac) == 1:
            fac, uids = next(iter(by_fac.items()))
            self.hold = fac
            self.sc[fac] += min(PER_PLAYER_MAX, len(uids))
        elif by_fac:
            self.hold = "contested"
        else:
            self.hold = None
        for uids in by_fac.values():
            for uid in uids:
                self.part[uid]["sec"] += 1
        return max(self.sc.values()) >= TARGET

    def finish(self):
        """Завершить событие: победитель — единственный лидер по очкам (ничья — без победителя)."""
        best = max(self.sc.values())
        leaders = [f for f, v in self.sc.items() if v == best]
        self.winner = leaders[0] if best > 0 and len(leaders) == 1 else None
        self.phase = "idle"
        return self.winner

    def rewards(self):
        """[(uid, fac, lvl, победитель?)] — участники, простоявшие в круге достаточно долго."""
        return [(uid, p["fac"], p["lvl"], p["fac"] == self.winner)
                for uid, p in self.part.items() if p["sec"] >= REWARD_MIN_SEC]


WAR = War()
BUFF = {"fac": None, "until": 0}
STATE = {"next": None, "soon_sent": False, "last_all": 0.0}


def loot_mult(fac, now=None):
    now = now or time.time()
    return BUFF_LOOT if fac and fac == BUFF["fac"] and now < BUFF["until"] else 1.0


def status(now=None):
    """Состояние для клиента (сообщение cw)."""
    now = now or time.time()
    x, y, r = ZONE
    d = {"t": "cw", "ph": WAR.phase, "loc": LOC, "x": x, "y": y, "r": r, "sc": dict(WAR.sc), "hold": WAR.hold,
         "target": TARGET, "min": MIN_LVL}
    if WAR.phase == "live":
        d["end"] = int(WAR.end * 1000)
    if STATE["next"]:
        d["next"] = int(STATE["next"] * 1000)
    if BUFF["fac"] and now < BUFF["until"]:
        d["buff"] = {"fac": BUFF["fac"], "until": int(BUFF["until"] * 1000), "loot": BUFF_LOOT, "exp": BUFF_EXP}
    return d


def reward_amounts(lvl, won):
    """Награда участнику: лом и ядра (мягкая валюта игры)."""
    lvl = max(1, min(50, int(lvl)))
    return {"scrap": (400 + 40 * lvl) if won else (150 + 15 * lvl), "cores": 5 if won else 0}


# ---------- работа с базой и сетью (вызывается из webserver) ----------
async def load_buff():
    from sqlalchemy import select
    from db import SessionLocal
    from models import Meta
    try:
        async with SessionLocal() as s:
            row = (await s.execute(select(Meta).where(Meta.key == "chipwar_buff"))).scalar_one_or_none()
        if row:
            d = json.loads(row.value)
            BUFF.update(fac=d.get("fac"), until=float(d.get("until", 0)))
    except Exception:
        log.exception("не удалось загрузить бонус Chip War")


async def _save_buff():
    from db import SessionLocal
    from db_atomic import insert_ignore
    from models import Meta
    from sqlalchemy import update
    value = json.dumps({"fac": BUFF["fac"], "until": BUFF["until"]})
    async with SessionLocal() as s:
        await s.execute(insert_ignore(Meta.__table__, key="chipwar_buff", value=value))
        await s.execute(update(Meta).where(Meta.key == "chipwar_buff").values(value=value).execution_options(synchronize_session=False))
        await s.commit()


async def _give_rewards(push):
    """Выдать награды через обычные выдачи (grants): онлайн — сразу, офлайн — при следующем входе."""
    import funnel
    from db import SessionLocal
    from models import Grant
    rewards = WAR.rewards()
    if not rewards:
        return 0
    delivered = []
    async with SessionLocal() as s:
        for uid, fac, lvl, won in rewards:
            for kind, amount in reward_amounts(lvl, won).items():
                if amount <= 0:
                    continue
                g = Grant(tg_id=uid, kind=kind, payload=json.dumps({"amount": amount, "won": won}), by_admin="chipwar", created=int(time.time()))
                s.add(g)
                delivered.append((uid, g))
            funnel.mark(uid, "chipwar")
        await s.commit()
    for uid, g in delivered:
        await push(uid, {"t": "grant", "grant": {"id": g.id, "kind": g.kind, "payload": json.loads(g.payload), "by": g.by_admin}})
    return len(rewards)


async def start_now(hub, duration=DURATION):
    WAR.begin(time.time(), duration)
    STATE["soon_sent"] = False
    log.warning("Chip War началась: %s, %s мин", LOC, duration // 60)
    hub.to_all(status() | {"ann": "start"})


async def finish_now(hub, push):
    winner = WAR.finish()
    if winner:
        BUFF.update(fac=winner, until=time.time() + BUFF_HOURS * 3600)
        try:
            await _save_buff()
        except Exception:
            log.exception("не удалось сохранить бонус Chip War")
    try:
        n = await _give_rewards(push)
    except Exception:
        n = 0
        log.exception("не удалось выдать награды Chip War")
    STATE["next"] = next_start(time.time())
    log.warning("Chip War окончена: победитель %s, очки %s, награждено %s", winner, WAR.sc, n)
    hub.to_all(status() | {"ann": "end", "win": winner})


async def loop(hub, push, metrics):
    """Раз в секунду: расписание, подсчёт очков, рассылка состояния."""
    import asyncio
    await load_buff()
    STATE["next"] = next_start(time.time())
    while True:
        await asyncio.sleep(1.0)
        try:
            now = time.time()
            if WAR.phase == "idle":
                nxt = STATE["next"]
                if nxt and not STATE["soon_sent"] and 0 < nxt - now <= 300:
                    STATE["soon_sent"] = True
                    hub.to_all(status() | {"ann": "soon"})
                if nxt and now >= nxt:
                    await start_now(hub)
                continue
            infos = [c.info for c in list(hub.by_loc.get(LOC, ())) if not c.closing]
            done = WAR.tick(infos)
            hub.to_loc(LOC, status())                   # в каньоне — каждую секунду
            if now - STATE["last_all"] >= 10:           # остальным — счёт раз в 10 с для баннера
                STATE["last_all"] = now
                hub.to_all(status(), only=lambda i: i.get("loc") != LOC)
            metrics.gauge("chipwar.score", dict(WAR.sc))
            if done or now >= WAR.end:
                await finish_now(hub, push)
        except Exception:
            log.exception("chipwar tick")
