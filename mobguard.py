"""Серверный учёт боя с мобами: убийство засчитывается, только если сервер сам видел удары.

Мобы живут на телефоне игрока, поэтому сервер не может считать их полностью сам. Зато он может
проверить, что убийство правдоподобно:

* удары по мобу приходят раньше убийства и в том же WebSocket (порядок гарантирован);
* сумма увиденного урона не меньше SEEN_MIN от прочности моба (прочность сервер знает сам);
* урон, который сервер засчитывает, ограничен «бюджетом» в секунду по уровню игрока,
  поэтому «убить главаря за миг» нельзя: бой занимает хотя бы HP / бюджет секунд;
* главарь одного уровня умирает не чаще, чем возрождается в игре (BOSS_RESPAWN).

Режимы (переменная MOB_GUARD):
  off    — не проверять;
  shadow — проверять и считать в метриках (mobguard.reject.*), но убийство засчитывать (по умолчанию);
  on     — отклонять неправдоподобные убийства.
Сначала смотри /metrics в режиме shadow: у честных игроков отказов почти не должно быть.
"""
import os
import re
import time

import metrics
from config import env_int, env_float      # пустая переменная в Railway не роняет сервер

MODE = os.getenv("MOB_GUARD", "shadow").strip().lower()
SEEN_MIN = env_float("MOB_SEEN_MIN", 0.85)       # доля прочности моба, которую сервер должен увидеть ударами
DPS_K = env_float("MOB_DPS_K", 12)              # запас бюджета урона; 12 перекрывает умения, криты и удары по площади
BUDGET_SECONDS = 8.0                                      # столько секунд бюджета можно накопить заранее
BOSS_RESPAWN = env_int("MOB_BOSS_RESPAWN", 280)  # в игре главарь возрождается через 300 с
FIGHT_TTL = 180                                           # незаконченный бой забывается через 3 минуты
MAX_FIGHTS = 300                                          # одновременных боёв на игрока (защита памяти)

BASE_HP = {"scrap_crawler": 20, "rogue_drone": 30, "sentry_bot": 55, "war_walker": 90}

# какие мобы где водятся (зеркало игры): обычные — в полях, dg/db<уровень> — в подземельях своего диапазона
BASE_MOBS = {"scrap_crawler", "rogue_drone", "sentry_bot", "war_walker"}
LOC_MIN = {"scrapfields": 1, "reactor_ruins": 3, "iron_canyon": 6, "sector1": 1, "sector2": 21, "arena_fear": 1, "farm1": 21, "season1": 21}
# фарм-зона и сезонная зона: мобы 21–30 вперемешку (как DUNGEONS в игре)
DUNGEON_RANGE = {"sector1": (1, 20), "sector2": (21, 40), "arena_fear": (1, 40), "farm1": (21, 30), "season1": (21, 30)}


def mob_level(mob, loc):
    """Уровень моба в этой локации или None — такого моба здесь не бывает."""
    m = re.fullmatch(r"d[gb](\d{1,2})", mob or "")
    if m:
        lv = int(m.group(1))
        lo, hi = DUNGEON_RANGE.get(loc, (0, -1))
        return lv if lo <= lv <= hi else None
    if mob in BASE_MOBS and loc in ("scrapfields", "reactor_ruins", "iron_canyon"):
        return LOC_MIN[loc]
    return None
DG_BASE = ("scrap_crawler", "rogue_drone", "sentry_bot", "war_walker")

_fights = {}         # uid -> {iid: [mob, урон, первый удар, последний удар]}
_budget = {}         # uid -> [запас урона, время]
_boss_last = {}      # (uid, mob) -> время засчитанного убийства главаря
_rejects = {}        # uid -> число отказов за текущие сутки (для админа)
_reject_day = [0]


def _js_round(x):
    return int(x + 0.5)                                   # Math.round из игры (Python round — банковское)


def mob_hp(mob):
    """Прочность моба — зеркало NPCS из игры. None — такого моба нет."""
    if mob in BASE_HP:
        return BASE_HP[mob]
    if len(mob) >= 3 and mob[:2] in ("dg", "db") and mob[2:].isdigit():
        lv = int(mob[2:])
        if not 1 <= lv <= 40:
            return None
        hp = _js_round((20 + 14 * lv + 0.25 * lv * lv) * (1.25 if DG_BASE[(lv - 1) % 4] == "war_walker" else 1))
        return hp * 10 if mob[1] == "b" else hp
    return None


def is_boss(mob):
    return mob.startswith("db")


def dps_cap(lvl):
    """Потолок засчитываемого урона в секунду — с большим запасом над PvP-пределом одного удара."""
    return DPS_K * (40 + 8 * max(1, lvl))


def on_hits(uid, lvl, hits, now=None):
    """Удары по мобам из сообщения pos: [[iid, mob, урон], ...]. Возвращает засчитанный урон."""
    if MODE == "off" or not isinstance(hits, list):
        return 0
    now = now or time.time()
    cap = dps_cap(lvl)
    tokens, t0 = _budget.get(uid, (cap * BUDGET_SECONDS, now))
    tokens = min(cap * BUDGET_SECONDS, tokens + (now - t0) * cap)
    fights = _fights.setdefault(uid, {})
    credited = 0
    for h in hits[:40]:
        try:
            iid, mob, dmg = int(h[0]), str(h[1])[:24], int(h[2])
        except (TypeError, ValueError, IndexError):
            continue
        hp = mob_hp(mob)
        if hp is None or dmg <= 0 or not 0 < iid < 2 ** 53:
            continue
        dmg = min(dmg, hp * 3)                           # регенерация моба — не повод присылать миллион
        take = min(dmg, tokens)
        if take < dmg:
            metrics.inc("mobguard.budget_cut")
        tokens -= take
        f = fights.get(iid)
        if not f or f[0] != mob:
            if len(fights) >= MAX_FIGHTS:
                fights.pop(next(iter(fights)))
            f = fights[iid] = [mob, 0, now, now]
        f[1] += take
        f[3] = now
        credited += take
    _budget[uid] = (tokens, now)
    return credited


def check_kill(uid, iid, mob, now=None):
    """Причина отказа или None, если убийство правдоподобно. Бой после проверки забывается."""
    now = now or time.time()
    hp = mob_hp(mob)
    if hp is None:
        return "unknown_mob"
    try:
        iid = int(iid)
    except (TypeError, ValueError):
        return "no_iid"
    f = _fights.get(uid, {}).pop(iid, None)
    if not f:
        return "no_hits"
    if f[0] != mob:
        return "mob_mismatch"
    if f[1] < SEEN_MIN * hp:
        return "low_damage"
    if is_boss(mob):
        last = _boss_last.get((uid, mob), 0)
        if now - last < BOSS_RESPAWN:
            return "boss_respawn"
    return None


def allow_kill(uid, iid, mob, now=None):
    """Решение по одному убийству с учётом режима. True — засчитать."""
    if MODE == "off":
        return True
    now = now or time.time()
    reason = check_kill(uid, iid, mob, now)
    if reason is None:
        if is_boss(mob):
            _boss_last[(uid, mob)] = now
        metrics.inc("mobguard.ok")
        return True
    metrics.inc("mobguard.reject." + reason)
    day = int(now // 86400)
    if day != _reject_day[0]:
        _reject_day[0] = day
        _rejects.clear()
    _rejects[uid] = _rejects.get(uid, 0) + 1
    if MODE == "on":
        return False
    if is_boss(mob) and reason != "boss_respawn":
        _boss_last[(uid, mob)] = now
    return True


def top_rejects(n=15):
    """Игроки с наибольшим числом отклонённых убийств за сутки — для статистики админа."""
    return sorted(_rejects.items(), key=lambda kv: -kv[1])[:n]


def cleanup(online_ids, now=None):
    now = now or time.time()
    for uid in list(_fights):
        fights = _fights[uid]
        for iid in [i for i, f in fights.items() if now - f[3] > FIGHT_TTL]:
            fights.pop(iid, None)
        if not fights and uid not in online_ids:
            _fights.pop(uid, None)
    for uid in [u for u in _budget if u not in online_ids]:
        _budget.pop(uid, None)
    for key in [k for k, t in _boss_last.items() if now - t > BOSS_RESPAWN]:
        _boss_last.pop(key, None)
