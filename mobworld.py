"""Общие мобы: у всех игроков в локации одни и те же мобы.

Раскладка мобов (номер, тип, место) в игре детерминирована: у всех телефонов номер N в локации —
один и тот же моб. Сервер хранит только то, что меняется: полученный урон, смерть и возрождение.

  телефон → сервер   {"t":"mhit", "loc", "h":[[номер, тип, урон], ...]}   (пачкой раз в ~150 мс)
  сервер → все в локации:
     {"t":"mhp",  "loc", "u":[[номер, hp, max], ...]}  — прочность изменилась (раз в тик мира)
     {"t":"mdie", "loc", "i", "by", "iid"}             — моб убит; by — кто нанёс больше всего урона
     {"t":"mres", "loc", "i"}                           — моб возродился
  сервер → вошедшему в локацию: {"t":"mstate", "loc", "dead":[[номер, сек]], "hp":[[номер, hp, max]]}

Лут и опыт получает тот, кто нанёс больше всего урона (а не «добивший»), — так нельзя
украсть чужого моба последним ударом. Его убийство сервер заранее отмечает в mobguard,
поэтому обычная выдача лута (items.process_kills) работает без изменений.
"""
import itertools
import time

import metrics
import mobguard

ARENA = {"arena_fear", "lobby"}      # на арене волны свои у каждого, в ангаре мобов нет
RESPAWN, RESPAWN_BOSS = 10, 300
REGEN_AFTER = 12                     # без ударов столько секунд — моб полностью восстанавливается
MAX_IDX = 5000

_mobs = {}                           # loc -> {номер: {...}}
_dirty = {}                          # loc -> set(номер) — прочность изменилась, разослать в тик
_iid = itertools.count(int(time.time()) % 100000 * 1000 + 7_000_000_000)


def _mob(loc, i, typ):
    m = _mobs.setdefault(loc, {}).get(i)
    if m is None or m["type"] != typ:
        hp = mobguard.mob_hp(typ)
        if hp is None:
            return None
        m = _mobs[loc][i] = {"type": typ, "hp": hp, "max": hp, "dmg": {}, "dead_until": 0, "last": 0}
    return m


def on_hits(info, d, hub):
    """Удары одного игрока. Возвращает список событий смерти для рассылки."""
    loc = d.get("loc")
    if loc != info.get("loc") or loc in ARENA or not isinstance(d.get("h"), list):
        return
    now = time.time()
    for h in d["h"][:60]:
        try:
            i, typ, dmg = int(h[0]), str(h[1])[:24], int(h[2])
        except (TypeError, ValueError, IndexError):
            continue
        if not 0 <= i < MAX_IDX or dmg <= 0:
            continue
        m = _mob(loc, i, typ)
        if m is None or m["dead_until"] > now:
            continue
        dmg = min(dmg, m["max"])                              # один пакет не больше полной прочности
        m["hp"] -= dmg
        m["last"] = now
        m["dmg"][info["id"]] = m["dmg"].get(info["id"], 0) + dmg
        _dirty.setdefault(loc, set()).add(i)
        if m["hp"] <= 0:
            _kill(loc, i, m, hub, now)


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
    metrics.inc("mobs.shared_kill")


def tick(hub):
    """Раз в тик мира: разослать изменения прочности, возродить и восстановить мобов."""
    now = time.time()
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
    return {"t": "mstate", "loc": loc,
            "dead": [[i, round(m["dead_until"] - now, 1)] for i, m in mobs.items() if m["dead_until"] > now],
            "hp": [[i, max(0, int(m["hp"])), m["max"]] for i, m in mobs.items() if not m["dead_until"] and m["hp"] < m["max"]]}
