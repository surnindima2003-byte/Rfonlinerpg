"""Общие мобы: у всех игроков в локации одни и те же мобы.

Раскладка мобов (номер, тип, место) в игре детерминирована: у всех телефонов номер N в локации —
один и тот же моб. Сервер хранит только то, что меняется: полученный урон, смерть и возрождение.

  телефон → сервер   {"t":"mhit", "loc", "h":[[номер, тип, урон], ...]}   (пачкой раз в ~150 мс)
  сервер → все в локации:
     {"t":"mhp",  "loc", "u":[[номер, hp, max], ...]}  — прочность изменилась (раз в тик мира)
     {"t":"mdie", "loc", "i", "by", "iid"}             — моб убит; by — кто нанёс больше всего урона
     {"t":"mres", "loc", "i"}                           — моб возродился
  сервер → вошедшему в локацию: {"t":"mstate", "loc", "dead":[[номер, сек]], "hp":[[номер, hp, max]]}

Движение и атаки: моба «ведёт» тот игрок, за которым он гонится (владелец). Его телефон считает
ИИ моба — путь, стены, удары по себе — и шлёт позицию; сервер пересылает её остальным, и они
видят того же моба в том же месте. Моб без владельца стоит (подземелье) или бродит по общему
для всех расписанию (поле), поэтому тоже совпадает у всех.
  телефон → сервер   {"t":"mctl", "loc", "i":[номера]}            — «моб гонится за мной», просьба вести
                     {"t":"mpos", "loc", "m":[[номер, x, y, угол, атак], ...]}  — только от владельца
  сервер → все в локации: {"t":"mown", "loc", "i", "uid"}  (uid 0 — владельца нет)
                          {"t":"mmv", "loc", "m":[[номер, x, y, угол, атак, владелец], ...]}

Лут и опыт получает тот, кто нанёс больше всего урона (а не «добивший»), — так нельзя
украсть чужого моба последним ударом. Его убийство сервер заранее отмечает в mobguard,
поэтому обычная выдача лута (items.process_kills) работает без изменений.
"""
import itertools
import math
import time

import metrics
import mobguard

ARENA = {"arena_fear", "lobby", "tower"}      # на арене волны свои у каждого, в ангаре мобов нет
RESPAWN, RESPAWN_BOSS = 10, 300
REGEN_AFTER = 12                     # без ударов столько секунд — моб полностью восстанавливается
OWNER_STALE = 2.0                    # владелец не присылал позицию столько секунд — моб свободен
MAX_IDX = 5000

_mobs = {}                           # loc -> {номер: {...}}
_dirty = {}                          # loc -> set(номер) — прочность изменилась, разослать в тик
_own = {}                            # loc -> {номер: {"uid", "t", "x", "y", "a", "n", "type"}}
_moved = {}                          # loc -> set(номер) — позиция изменилась, разослать в тик
_iid = itertools.count(int(time.time()) % 100000 * 1000 + 7_000_000_000)


_budget = {}                         # uid -> [запас урона, время] — бюджет урона по общим мобам
LVL_GAP = 12                         # как в items.process_kills: моб сильнее предела уровня на 12+ не засчитывается


def _mob(loc, i, typ):
    """Состояние моба номер i. None — такого моба здесь быть не может, или под этим номером другой моб.

    Тип присылает телефон, поэтому сервер проверяет, что такой моб вообще водится в этой локации
    (иначе можно «призвать» главаря подземелья в поле). Раньше другой тип под тем же номером сбрасывал моба
    на полную прочность — так можно было отнять чужого почти убитого моба. Теперь такие удары отбрасываются:
    раскладка мобов у всех телефонов одинаковая, честный клиент другой тип не пришлёт."""
    if mobguard.mob_level(typ, loc) is None:
        return None
    m = _mobs.setdefault(loc, {}).get(i)
    if m is not None:
        return m if m["type"] == typ else None
    hp = mobguard.mob_hp(typ)
    if hp is None:
        return None
    m = _mobs[loc][i] = {"type": typ, "hp": hp, "max": hp, "dmg": {}, "dead_until": 0, "last": 0}
    return m


def _take_budget(uid, lvl, dmg, now):
    """Сколько урона из dmg засчитать: не больше, чем игрок его уровня может нанести (как mobguard.on_hits).
    Без этого одно сообщение mhit убивало главаря целиком, а _kill ещё и записывал бой в mobguard как
    «виденный» — то есть обходил даже MOB_GUARD=on."""
    cap = mobguard.dps_cap(lvl)
    tokens, t0 = _budget.get(uid, (cap * mobguard.BUDGET_SECONDS, now))
    tokens = min(cap * mobguard.BUDGET_SECONDS, tokens + (now - t0) * cap)
    take = min(dmg, tokens)
    _budget[uid] = (tokens - take, now)
    if take < dmg:
        metrics.inc("mobs.budget_cut")
    return take


def on_hits(info, d, hub):
    """Удары одного игрока по общим мобам."""
    loc = d.get("loc")
    if loc != info.get("loc") or loc in ARENA or not isinstance(d.get("h"), list):
        return
    now = time.time()
    metrics.inc("mobs.hit_msgs")
    lvl = info.get("lvl_cap") or info.get("lvl") or 1          # уровень, подтверждённый сервером
    for h in d["h"][:60]:
        try:
            i, typ, dmg = int(h[0]), str(h[1])[:24], int(h[2])
        except (TypeError, ValueError, IndexError):
            continue
        if not 0 <= i < MAX_IDX or dmg <= 0:
            continue
        m = _mob(loc, i, typ)
        if m is None:
            metrics.inc("mobs.bad_type")
            continue
        if m["dead_until"] > now:
            continue
        if not info.get("admin") and mobguard.mob_level(typ, loc) > lvl + LVL_GAP:
            continue                                          # слишком сильный моб — его убийство всё равно не засчитают
        dmg = min(dmg, m["max"])                              # один пакет не больше полной прочности
        if not info.get("admin"):
            dmg = _take_budget(info["id"], lvl, dmg, now)
            if dmg <= 0:
                continue
        m["hp"] -= dmg
        m["last"] = now
        m["dmg"][info["id"]] = m["dmg"].get(info["id"], 0) + dmg
        _dirty.setdefault(loc, set()).add(i)
        if m["hp"] <= 0:
            _kill(loc, i, m, hub, now)


def cleanup(online_ids):
    """Раз в минуту: бюджет ушедших игроков не нужен."""
    for uid in [u for u in _budget if u not in online_ids]:
        _budget.pop(uid, None)


def on_claim(info, d, hub):
    """Моб погнался за игроком — отдаём его вести этому игроку, если у моба нет живого владельца."""
    loc = d.get("loc")
    if loc != info.get("loc") or loc in ARENA or not isinstance(d.get("i"), list):
        return
    now, own = time.time(), _own.setdefault(loc, {})
    for i in d["i"][:40]:
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        if not 0 <= i < MAX_IDX:
            continue
        m = _mobs.get(loc, {}).get(i)
        if m and m["dead_until"] > now:
            continue
        o = own.get(i)
        if o and o["uid"] != info["id"] and now - o["t"] < OWNER_STALE:
            continue                                          # уже ведёт другой игрок
        if not o or o["uid"] != info["id"]:
            own[i] = {"uid": info["id"], "t": now, "x": None, "y": None, "a": 0, "n": 0}
            hub.to_loc(loc, {"t": "mown", "loc": loc, "i": i, "uid": info["id"]})
        else:
            o["t"] = now


def on_pos(info, d):
    """Позиции мобов от их владельца."""
    loc = d.get("loc")
    if loc != info.get("loc") or not isinstance(d.get("m"), list):
        return
    now, own = time.time(), _own.get(loc, {})
    for row in d["m"][:60]:
        try:
            i, x, y, a, n = int(row[0]), float(row[1]), float(row[2]), float(row[3]), int(row[4])
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(a)):
            continue                                          # "nan"/"inf" строкой: не рассылаем другим
        o = own.get(i)
        if not o or o["uid"] != info["id"]:
            continue                                          # чужого моба двигать нельзя
        o.update(t=now, x=max(0.0, min(8000.0, x)), y=max(0.0, min(8000.0, y)), a=round(a, 2), n=n)
        _moved.setdefault(loc, set()).add(i)


def _free(loc, i, hub):
    if _own.get(loc, {}).pop(i, None):
        hub.to_loc(loc, {"t": "mown", "loc": loc, "i": i, "uid": 0})


def _kill(loc, i, m, hub, now):
    winner = max(m["dmg"].items(), key=lambda kv: kv[1])[0]
    iid = next(_iid)
    # засчитываем победителю «виденный» бой, чтобы его обычная заявка на убийство прошла проверку
    fights = mobguard._fights.setdefault(winner, {})
    if len(fights) >= mobguard.MAX_FIGHTS:
        fights.pop(next(iter(fights)))
    fights[iid] = [m["type"], m["max"], now, now]
    m["dead_until"] = now + (RESPAWN_BOSS if mobguard.is_boss(m["type"]) else RESPAWN)
    m["dmg"] = {}
    m["hp"] = m["max"]
    _dirty.get(loc, set()).discard(i)
    hub.to_loc(loc, {"t": "mdie", "loc": loc, "i": i, "by": winner, "iid": iid})
    _own.get(loc, {}).pop(i, None)
    _moved.get(loc, set()).discard(i)
    metrics.inc("mobs.shared_kill")


def tick(hub):
    """Раз в тик мира: позиции ведомых мобов, прочность, возрождение, восстановление."""
    now = time.time()
    for loc, own in list(_own.items()):
        for i, o in list(own.items()):
            online = any(c.uid == o["uid"] for c in hub.by_loc.get(loc, ()))
            if now - o["t"] > OWNER_STALE or not online:
                _free(loc, i, hub)                             # владелец ушёл, умер или отстал — моб свободен
        moved = _moved.pop(loc, None)
        if moved:
            mv = [[i, round(own[i]["x"], 1), round(own[i]["y"], 1), own[i]["a"], own[i]["n"], own[i]["uid"]]
                  for i in moved if i in own and own[i]["x"] is not None]
            if mv:
                hub.to_loc(loc, {"t": "mmv", "loc": loc, "m": mv})
        if not own:
            _own.pop(loc, None)
    for loc, mobs in list(_mobs.items()):
        if loc not in hub.by_loc:                            # в локации никого — состояние не нужно
            if all(m["dead_until"] <= now for m in mobs.values()):
                _mobs.pop(loc, None)
                _dirty.pop(loc, None)
                continue
        for i, m in list(mobs.items()):
            if m["dead_until"]:
                if m["dead_until"] <= now:
                    m["dead_until"] = 0
                    hub.to_loc(loc, {"t": "mres", "loc": loc, "i": i})
                    mobs.pop(i, None)
            elif m["hp"] < m["max"] and now - m["last"] > REGEN_AFTER:
                m["hp"], m["dmg"] = m["max"], {}
                _dirty.setdefault(loc, set()).add(i)
        dirty = _dirty.pop(loc, None)
        if dirty:
            u = [[i, max(0, int(mobs[i]["hp"])), mobs[i]["max"]] for i in dirty if i in mobs]
            if u:
                hub.to_loc(loc, {"t": "mhp", "loc": loc, "u": u})
            for i in dirty:                                    # полностью здоровых живых забываем
                m = mobs.get(i)
                if m and not m["dead_until"] and m["hp"] >= m["max"]:
                    mobs.pop(i, None)


def state(loc):
    """Снимок для игрока, только что вошедшего в локацию."""
    now = time.time()
    mobs = _mobs.get(loc, {})
    own = _own.get(loc, {})
    return {"t": "mstate", "loc": loc,
            "own": [[i, o["uid"], o["x"], o["y"]] for i, o in own.items() if o["x"] is not None],
            "dead": [[i, round(m["dead_until"] - now, 1)] for i, m in mobs.items() if m["dead_until"] > now],
            "hp": [[i, max(0, int(m["hp"])), m["max"]] for i, m in mobs.items() if not m["dead_until"] and m["hp"] < m["max"]]}
