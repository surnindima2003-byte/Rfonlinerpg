"""Элитная фарм-зона (как в LIBERTY): отдельная зона на группу из 3 пилотов.

Правила (их же показывает окно события в игре):
  * вход с 25 уровня, только полной группой из 3 человек;
  * войти может только лидер группы — сервер выдаёт группе свой экземпляр зоны (elite1…elite6)
    и затягивает туда всех участников (сообщение elite_go);
  * если в зоне осталось меньше 3 человек группы, через GRACE секунд всех выводит из зоны;
  * не более 120 минут в зоне на игрока в сутки (по Москве, как попытки «Страха» и башни).
Группа — обычная пати сервера (webserver.parties): «Создать группу» открывает набор, и группа видна в «Открытых
группах» — вступить может любой пилот 25+ без приглашения. Монстров зоны ведут телефоны группы как общих мобов
(mobworld: урон и смерть считает сервер), типы el26…el36 знает mobguard.

Модуль без базы и сети: состояние в памяти (перезапуск сервера заканчивает забеги и обнуляет минуты, как у башни).
"""
import time
from datetime import datetime, timedelta, timezone

SLOTS = 6
LOCS = tuple(f"elite{i}" for i in range(1, SLOTS + 1))
SIZE = 3
MIN_LVL = 25
LIMIT = 120 * 60                    # секунд в зоне на игрока в сутки
GRACE = 10                          # столько секунд группа может стоять неполной (переподключение, переход)
START_GRACE = 25                    # после входа — время всем добраться до зоны
STEP_MAX = 3.0                      # между двумя сообщениями из зоны засчитываем не больше 3 с (обрыв связи — не в зоне)
MSK = timezone(timedelta(hours=3))

_used = {}       # uid -> [день по Москве, секунд в зоне]
_last = {}       # uid -> время последнего сообщения из зоны
_runs = {}       # loc -> {"pid", "members": [uid], "need", "t0", "low"}
_open = {}       # pid -> время, когда лидер открыл набор


def _today():
    return datetime.now(MSK).strftime("%Y-%m-%d")


def used(uid):
    d = _used.get(uid)
    return d[1] if d and d[0] == _today() else 0.0


def left(uid):
    """Сколько секунд в зоне осталось игроку сегодня."""
    return max(0.0, LIMIT - used(uid))


def account(uid, now=None):
    """Игрок прислал позицию из зоны — прибавляем время с прошлого сообщения (обрыв связи не считаем)."""
    now = time.time() if now is None else now
    prev = _last.get(uid)
    _last[uid] = now
    if prev is None:
        return
    dt = now - prev
    if 0 < dt <= STEP_MAX:
        day = _today()
        d = _used.get(uid)
        if not d or d[0] != day:
            d = _used[uid] = [day, 0.0]
        d[1] += dt


def out_of_zone(uid):
    _last.pop(uid, None)


def run_of_loc(loc):
    return _runs.get(loc)


def run_of_pid(pid):
    for loc, r in _runs.items():
        if r["pid"] == pid:
            return loc
    return None


def member_of(loc, uid):
    r = _runs.get(loc)
    return bool(r and uid in r["members"])


def set_open(pid, on=True, now=None):
    if on:
        _open.setdefault(pid, time.time() if now is None else now)
    else:
        _open.pop(pid, None)


def is_open(pid):
    return pid in _open


def check_enter(leader, members, infos, admin=False):
    """Можно ли лидеру завести группу. members — список uid, infos — uid -> info (или None, если не в сети).
    Возвращает текст отказа или None."""
    if not admin and len(members) != SIZE:
        return f"Нужна полная группа из {SIZE} человек"
    for uid in members:
        i = infos.get(uid)
        nick = (i or {}).get("nick") or "Участник"
        if not i:
            return f"{nick} не в сети"
        if not admin and int(i.get("lvl") or 1) < MIN_LVL:
            return f"{nick}: нужен {MIN_LVL} уровень"
        if left(uid) < 60:
            return f"{nick}: лимит {LIMIT // 60} минут на сегодня исчерпан"
        if i.get("dead"):
            return f"{nick} сейчас погиб — подождите возрождения"
        if i.get("loc") in LOCS:
            return f"{nick} уже в элитной зоне"
    return None


def start(pid, members, now=None, need=None):
    """Выдать группе свободный экземпляр зоны. Возвращает loc или None, если все заняты."""
    now = time.time() if now is None else now
    loc = run_of_pid(pid)
    if not loc:
        loc = next((l for l in LOCS if l not in _runs), None)
        if not loc:
            return None
    _runs[loc] = {"pid": pid, "members": list(members), "need": need or SIZE, "t0": now, "low": 0.0}
    _open.pop(pid, None)
    return loc


def end(loc):
    return _runs.pop(loc, None)


def tick(where, now=None):
    """Раз в секунду. where(uid) — локация игрока в сети или None.
    Возвращает список (loc, [uid…], текст) — кого вывести из зоны и почему."""
    now = time.time() if now is None else now
    out = []
    for loc, r in list(_runs.items()):
        inside = [u for u in r["members"] if where(u) == loc]
        tired = [u for u in inside if left(u) <= 0]
        if tired:
            out.append((loc, tired, f"Лимит {LIMIT // 60} минут в элитной зоне на сегодня исчерпан"))
            inside = [u for u in inside if u not in tired]
        if now - r["t0"] < START_GRACE and not tired:
            continue
        if len(inside) >= r["need"]:
            r["low"] = 0.0
            continue
        if not inside:
            end(loc)                                        # в зоне никого — экземпляр свободен
            continue
        if not r["low"]:
            r["low"] = now
        elif now - r["low"] >= GRACE:
            end(loc)
            out.append((loc, inside, f"В зоне осталось меньше {SIZE} человек группы — всех вывели из зоны"))
    return out


def cleanup(online_ids, parties_alive, now=None):
    """Раз в минуту: вчерашние минуты, ушедшие игроки, распавшиеся группы."""
    now = time.time() if now is None else now
    day = _today()
    for uid in [u for u, d in _used.items() if d[0] != day]:
        _used.pop(uid, None)
    for uid in [u for u in _last if u not in online_ids]:
        _last.pop(uid, None)
    for pid in [p for p in _open if p not in parties_alive]:
        _open.pop(pid, None)
