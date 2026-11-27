"""Награды сюжетных и ежедневных квестов — через сервер.

Раньше награды квестов (до 30 000 лома и 60 000 опыта за сюжетный квест, лом, ядра и сферы за ежедневные)
выдавал только телефон: сервер о них не знал. Их можно было подделать, а учёт ресурсов (RES_LEDGER=on)
срезал бы их честным игрокам. Теперь телефон просит награду у сервера (/api/quests/claim), а сервер:

* выдаёт каждый сюжетный квест один раз (номер в цепочке), опыт — в серверный учёт опыта (progress);
  квест «достигни N уровня» — только если уровень, который допускает сервер, не ниже N;
* ежедневные: не больше трёх разных заданий в день и бонус дня — после трёх; размер награды считает сам
  по формуле игры и уровню игрока, который допускает сервер;
* записывает лом и ядра в учёт ресурсов (ledger), сферы — в проверку сохранений (saveguard).

Ход выполнения (сколько убито, куплено) по-прежнему считает телефон: сервер ограничивает то, ЧТО и СКОЛЬКО
можно получить, — подделка даёт не больше честного максимума и только один раз.
Отмеченное хранится в документе quests/<uid> (таблица docs). Зеркало таблиц game.html сверяет test_quests.py.
"""
import json
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

# сюжет: (тип, нужно, опыт, лом, ремкомплекты) — STORY_FLAT в игре, по порядку
STORY = [
    ("kill", 10, 50, 25, 6), ("kill", 10, 50, 25, 0), ("buy", 10, 60, 30, 0), ("kill", 30, 120, 60, 0), ("kill", 30, 120, 60, 6),
    ("lvl", 3, 150, 75, 0), ("kill", 50, 200, 100, 0), ("kill", 50, 200, 100, 0), ("lvl", 5, 350, 175, 0), ("lvl", 10, 400, 200, 6),
    ("kill", 100, 450, 225, 0), ("kill", 100, 450, 225, 0), ("lvl", 15, 500, 250, 0), ("lvl", 20, 300, 150, 0), ("loc", 1, 600, 300, 6),
    ("kill", 10, 1000, 500, 6), ("kill", 10, 1000, 500, 0), ("buy", 10, 1200, 600, 0), ("kill", 30, 2400, 1200, 0), ("kill", 30, 2400, 1200, 6),
    ("lvl", 23, 3000, 1500, 0), ("kill", 50, 4000, 2000, 0), ("kill", 50, 4000, 2000, 0), ("lvl", 25, 7000, 3500, 0), ("ench", 2, 8000, 4000, 6),
    ("kill", 100, 9000, 4500, 0), ("kill", 100, 9000, 4500, 0), ("ench", 3, 10000, 5000, 0), ("boss", 1, 6000, 3000, 0), ("ench", 5, 12000, 6000, 6),
    ("loc", 1, 15000, 7500, 6), ("kill", 10, 15000, 7500, 0), ("kill", 10, 15000, 7500, 0), ("kill", 30, 20000, 10000, 0), ("kill", 30, 20000, 10000, 6),
    ("lvl", 30, 25000, 12500, 0), ("kill", 50, 25000, 12500, 0), ("pvp", 5, 20000, 10000, 6), ("lvl", 35, 30000, 15000, 0), ("kill", 100, 35000, 17500, 0),
    ("kill", 100, 35000, 17500, 0), ("ench", 7, 40000, 20000, 6), ("boss", 3, 45000, 22500, 0), ("lvl", 40, 50000, 25000, 0), ("ench", 9, 60000, 30000, 6),
]
# ежедневные: id задания -> с какого уровня (QUEST_TYPES в игре)
DAILY = {"kill": 0, "dkill": 0, "scrap": 0, "skill": 0, "wave": 0, "boss": 3, "loot": 0}
DAILY_PER_DAY = 3


def daily_reward(kind, lv):
    """qReward в игре."""
    base = 80 + lv * 15
    if kind == "boss":
        return {"scrap": base * 3, "cores": 5}
    if kind == "wave":
        return {"scrap": base * 2, "kit": 2}
    if kind == "dkill":
        return {"scrap": int(base * 1.5 + 0.5), "cores": 2}      # как Math.round в игре (round в Python округляет .5 к чётному)
    return {"scrap": base, "kit": 1}


def bonus_reward(lv):
    """Бонус за все три задания дня (как в игре)."""
    return {"sph": 1, "cores": 5, "scrap": 100 + lv * 20}


def parse_day(day):
    """День телефона «2026-11-27» (местная дата игрока, без нулей). Принимаем только ±1 день от даты сервера:
    часовые пояса игроков — от UTC−12 до UTC+14."""
    try:
        y, m, d = (int(x) for x in str(day).split("-"))
        dt = datetime(y, m, d, tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if abs((dt - today).days) > 1:
        return None
    return f"{y}-{m}-{d}"


def _clean_days(days):
    """Оставляем только дни в окне ±1 от сегодня — документ не растёт."""
    return {k: v for k, v in days.items() if parse_day(k)}


def check_story(st, idx, lvl):
    """Можно ли выдать сюжетный квест idx. Возвращает текст ошибки или None. st — {"si": следующий номер}."""
    if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(STORY):
        return "Нет такого квеста"
    if idx < st["si"]:
        return "Награда уже получена"
    t, n = STORY[idx][0], STORY[idx][1]
    if t == "lvl" and lvl < n:
        return f"Нужен {n} уровень"
    return None


def check_daily(st, day, kind, lvl):
    """Ежедневное задание kind в день day (уже проверенный parse_day). Текст ошибки или None."""
    if kind not in DAILY:
        return "Нет такого задания"
    if lvl < DAILY[kind]:
        return "Задание откроется с " + str(DAILY[kind]) + " уровня"
    box = st["days"].setdefault(day, {"t": [], "b": False})
    if kind in box["t"]:
        return "Награда уже получена"
    if len(box["t"]) >= DAILY_PER_DAY:
        return "Сегодня уже получены награды за три задания"
    return None


def check_bonus(st, day):
    box = st["days"].setdefault(day, {"t": [], "b": False})
    if box["b"]:
        return "Награда уже получена"
    if len(box["t"]) < DAILY_PER_DAY:
        return "Сначала забери награды трёх заданий"
    return None


def _credit(uid, rew):
    import ledger
    import saveguard
    money = {k: rew[k] for k in ("scrap", "cores") if rew.get(k)}
    ledger.add(uid, money)
    saveguard.note_grant(uid, {**money, "sph": rew.get("sph", 0)})


async def _state(s, uid):
    """Документ quests/<uid>. Первый раз — номер сюжетного квеста из последнего сохранения (честный прогресс
    не теряется при обновлении) и полученные сегодня ежедневные награды."""
    from models import Doc, GameSave
    path = f"quests/{uid}"
    row = (await s.execute(select(Doc).where(Doc.path == path))).scalar_one_or_none()
    if row:
        try:
            st = json.loads(row.data or "{}")
        except ValueError:
            st = {}
    else:
        st = {}
        raw = (await s.execute(select(GameSave.data).where(GameSave.tg_id == uid))).scalar_one_or_none()
        try:
            S = (json.loads(raw) or {}).get("S") if raw else None
        except ValueError:
            S = None
        if isinstance(S, dict):
            story = S.get("story")
            i = story.get("i") if isinstance(story, dict) else 0
            st["si"] = max(0, min(len(STORY), i)) if isinstance(i, int) and not isinstance(i, bool) else 0
            q = S.get("quests")
            day = parse_day(q.get("day")) if isinstance(q, dict) else None
            if day:
                got = [t.get("id") for t in (q.get("list") or []) if isinstance(t, dict) and t.get("got") and t.get("id") in DAILY]
                st["days"] = {day: {"t": got[:DAILY_PER_DAY], "b": bool(q.get("bonus"))}}
        row = Doc(path=path, col="quests", data="{}")
        s.add(row)
    st["si"] = st.get("si", 0) if isinstance(st.get("si"), int) else 0
    st["days"] = _clean_days(st.get("days") if isinstance(st.get("days"), dict) else {})
    return row, st


async def _level(s, uid, admin):
    """Уровень игрока для квестов: из сохранения, но не выше допустимого сервером."""
    import progress
    from models import GameSave
    if admin:
        return progress.LEVEL_CAP
    lvl = (await s.execute(select(GameSave.lvl).where(GameSave.tg_id == uid))).scalar() or 1
    return max(1, min(int(lvl), await progress.cap_of(s, uid)))


async def claim(s, uid, body, admin=False):
    """Забрать награду. Возвращает (ошибка или None, ответ). Коммит — за вызывающим."""
    row, st = await _state(s, uid)
    lvl = await _level(s, uid, admin)
    kind = body.get("kind")
    if kind == "story":
        try:
            idx = int(body.get("idx"))
        except (TypeError, ValueError):
            idx = -1
        err = check_story(st, idx, lvl)
        if err:
            return err, {"si": st["si"]}
        t, n, xp, scrap, kit = STORY[idx]
        st["si"] = idx + 1
        _credit(uid, {"scrap": scrap})
        if xp:
            import progress
            await progress.add_exp(s, uid, xp)
        out = {"si": st["si"]}
    elif kind in ("daily", "bonus"):
        day = parse_day(body.get("day"))
        if not day:
            return "Обнови игру: дата заданий не совпадает с сервером", {}
        if kind == "daily":
            k = str(body.get("type", ""))
            err = check_daily(st, day, k, lvl)
            if err:
                return err, {}
            st["days"][day]["t"].append(k)
            _credit(uid, daily_reward(k, lvl))
        else:
            err = check_bonus(st, day)
            if err:
                return err, {}
            st["days"][day]["b"] = True
            _credit(uid, bonus_reward(lvl))
        out = {}
    else:
        return "Нет такой награды", {}
    row.data, row.updated = json.dumps(st), int(time.time())
    return None, out


def setup(app, read_auth):
    from aiohttp import web
    from db import SessionLocal
    import metrics

    async def api_claim(request):
        body, user = await read_auth(request)
        import saveguard
        async with saveguard.player_lock(user["id"]):           # заодно с сохранением — по очереди
            async with SessionLocal() as s:
                err, out = await claim(s, user["id"], body, bool(user.get("admin")))
                if err:
                    return web.json_response({"ok": False, "error": err, **out})
                await s.commit()
        metrics.inc("quests.claim." + str(body.get("kind")))
        return web.json_response({"ok": True, **out})

    app.router.add_post("/api/quests/claim", api_claim)
