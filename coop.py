"""«Сотрудничество» (как в LIBERTY, в нашем стиле): совместный забег вдвоём.

Правила (их же показывает окно события в игре):
  1. группа ровно из 2 пилотов: «Создать группу» открывает набор, группа видна в «Открытых группах» —
     вступить может любой пилот 10+; начать забег может только лидер группы;
  2. только с 10 уровня;
  3. одна попытка в день (сутки по Москве), тратится при входе. Если вход сорвался или связь оборвалась
     до первого этапа — попытка возвращается обоим;
  4. 8 этапов по 40 монстров, уровень монстров растёт: 10, 15, 20, 25, 30, 40, 50, 60;
  5. монстры общие на двоих (прочность и смерть считает сервер — mobworld): следующий этап открывается,
     только когда напарники вдвоём зачистили текущий и оба на месте («Дождитесь напарника»);
  6. в конце — общий босс 40 уровня: 1 титановая сфера (безопасная заточка) и 100 энергоядер одному
     случайному пилоту из двоих (жребий тянет сервер, итог забега показывает, кому и что выдано);
  7. смерть любого из двоих завершает забег для обоих: напарника сразу возвращает в ангар.
На забег — не больше TIME_LIMIT. Экземпляры coop1…coop8 выдаются группам, как в элитной зоне (elite.py).

Карта забега (game.html → coopLayout): входной зал и 9 залов змейкой — 8 залов этапов и зал босса,
между ними энергобарьеры. Моб номер i: i // 40 — этап, i == BOSS_IDX — босс. Его тип у всех телефонов один
и тот же (mob_type), поэтому сервер принимает удары только по мобам текущего этапа и только своего типа.

Модуль без базы и сети: состояние в памяти (перезапуск сервера заканчивает забеги и обнуляет попытки,
как у башни и «Страха»). Сообщения игрокам модуль складывает в очередь — webserver забирает её (drain)
и рассылает; награду за босса в базу записывает webserver (coop_reward).
"""
import random
import time
from datetime import datetime, timedelta, timezone

PREFIX = "coop"
SLOTS = 8
LOCS = tuple(f"{PREFIX}{i}" for i in range(1, SLOTS + 1))
SIZE = 2
MIN_LVL = 10
TRIES = 1
STAGES = (10, 15, 20, 25, 30, 40, 50, 60)    # уровень монстров на этапах 1…8
PER = 40                                      # монстров на этапе
BOSS_LVL = 40
BOSS_TYPE = f"cb{BOSS_LVL}"
BOSS_IDX = len(STAGES) * PER                  # 320: номер босса в раскладке
BOSS_STAGE = len(STAGES) + 1                  # 9-й «этап» — бой с боссом
PRIZE = {"item": "sph_ti", "n": 1, "cores": 100}   # одному случайному из двоих
COUNT = 5            # отсчёт перед первым этапом
BREAK = 4            # передышка между этапами
GATHER = 30          # столько ждём, пока оба доберутся до зала; не собрались — попытки возвращаются
GRACE = 25           # напарник пропал из зала (обрыв связи, перезапуск игры) — столько ждём, потом «напарник вышел»
TIME_LIMIT = 40 * 60  # от начала первого этапа
WIN_STAY = 15        # после победы — время собрать добычу босса, потом всех возвращает в ангар
END_STAY = 3         # после поражения — запас, пока телефоны сами уходят в ангар
MSK = timezone(timedelta(hours=3))

REASON_TEXT = {"win": "Босс повержен", "dead": "{who} погиб — забег окончен", "left": "{who} вышел — забег окончен",
               "time": "Время забега вышло", "gather": "Напарник не пришёл — попытка возвращена"}

_used = {}       # uid -> день по Москве, когда потрачена попытка
_runs = {}       # loc -> забег
_open = {}       # pid -> время, когда лидер открыл набор
_out = []        # очередь для webserver: ("msg", uid, payload) | ("home", uid, loc, text) | ("reset", loc) | ("end", run)


def _today():
    return datetime.now(MSK).strftime("%Y-%m-%d")


def _now(now):
    return time.time() if now is None else now


# ---------- попытки ----------
def used(uid):
    return 1 if _used.get(uid) == _today() else 0


def left(uid):
    return max(0, TRIES - used(uid))


# ---------- раскладка мобов ----------
def mob_type(i):
    """Тип моба номер i в зале забега (как coopBuild в игре) или None."""
    if not isinstance(i, int) or i < 0:
        return None
    if i < BOSS_IDX:
        return f"cw{STAGES[i // PER]}"
    return BOSS_TYPE if i == BOSS_IDX else None


def stage_of(i):
    """Этап моба номер i: 1…8, BOSS_STAGE для босса, None — такого моба нет."""
    if not isinstance(i, int) or i < 0 or i > BOSS_IDX:
        return None
    return BOSS_STAGE if i == BOSS_IDX else i // PER + 1


def is_loc(loc):
    return loc in LOCS


# ---------- группы ----------
def set_open(pid, on=True, now=None):
    if on:
        _open.setdefault(pid, _now(now))
    else:
        _open.pop(pid, None)


def is_open(pid):
    return pid in _open


def open_groups():
    """Открытые группы по времени открытия набора: [(pid, время)]."""
    return sorted(_open.items(), key=lambda kv: kv[1])


# ---------- забеги ----------
def run_of_loc(loc):
    return _runs.get(loc)


def run_of_pid(pid):
    for loc, r in _runs.items():
        if r["pid"] == pid and r["phase"] != "done":
            return loc
    return None


def run_of_uid(uid):
    for r in _runs.values():
        if uid in r["members"] and r["phase"] != "done":
            return r
    return None


def member_of(loc, uid):
    r = _runs.get(loc)
    return bool(r and uid in r["members"] and r["phase"] != "done")


def partners(a, b):
    """Напарники по текущему забегу (лечение Ремонтника работает и без пати)."""
    r = run_of_uid(a)
    return bool(r and a != b and b in r["members"])


def check_enter(leader, members, infos, admin=False, busy=lambda uid: False):
    """Можно ли лидеру начать забег. infos — uid -> info (None, если не в сети). Текст отказа или None."""
    if not admin and len(members) != SIZE:
        return f"Нужна группа из {SIZE} пилотов"
    if leader not in members:
        return "Начать забег может только лидер группы"
    for uid in members:
        i = infos.get(uid)
        nick = (i or {}).get("nick") or "Напарник"
        if not i:
            return f"{nick} не в сети"
        if not admin and int(i.get("lvl") or 1) < MIN_LVL:
            return f"{nick}: нужен {MIN_LVL} уровень"
        if not admin and left(uid) < 1:
            return f"{nick}: попытка на сегодня уже использована"
        if i.get("dead"):
            return f"{nick} сейчас погиб — подождите возрождения"
        if i.get("loc") in LOCS or i.get("loc") in ("arena_fear", "tower") or str(i.get("loc")).startswith("elite"):
            return f"{nick} сейчас в другом событии"
        if run_of_uid(uid):
            return f"{nick} уже в забеге"
        if busy(uid):
            return f"{nick} в бою с игроком — подождите"
    return None


def start(pid, members, infos=None, now=None, need=None, admin=False):
    """Выдать группе свободный зал и потратить попытки. Возвращает loc или None (все залы заняты)."""
    now = _now(now)
    if run_of_pid(pid):
        return None
    loc = next((l for l in LOCS if l not in _runs), None)
    if not loc:
        return None
    infos = infos or {}
    spent = []
    day = _today()
    for uid in members:
        if _used.get(uid) != day:
            _used[uid] = day
            spent.append(uid)                                   # кому вернуть попытку, если вход сорвётся
    _runs[loc] = {"loc": loc, "pid": pid, "members": list(members), "need": need or SIZE, "admin": bool(admin),
                  "nick": {u: str((infos.get(u) or {}).get("nick") or "Пилот")[:16] for u in members},
                  "t0": now, "phase": "gather", "until": now + GATHER, "stage": 0, "cleared": 0, "kills": set(),
                  "away": {}, "fight_t": 0.0, "spent": spent, "result": None, "sent": 0.0, "dirty": True}
    _open.pop(pid, None)
    _out.append(("reset", loc))                                 # общие мобы зала — с чистого листа
    for uid in members:
        _out.append(("msg", uid, {"t": "cp_go", "loc": loc}))
    return loc


def allow_hit(loc, i, typ):
    """Правило для mobworld: удар засчитывается только по живому этапу и своему типу моба."""
    r = _runs.get(loc)
    if not r or r["phase"] != "fight" or mob_type(i) != typ:
        return False
    return stage_of(i) == r["stage"]


def on_kill(loc, i, typ, winner=None, now=None):
    """mobworld: моб забега убит (лут и опыт — тому, кто нанёс больше урона)."""
    r = _runs.get(loc)
    if not r or r["phase"] != "fight" or stage_of(i) != r["stage"] or mob_type(i) != typ:
        return
    r["kills"].add(i)
    r["dirty"] = True
    if r["stage"] == BOSS_STAGE:
        _finish(r, "win", now=now)
    elif len(r["kills"]) >= PER:
        _stage_clear(r, _now(now))


def _stage_clear(r, now):
    r["cleared"] = r["stage"]
    r["phase"], r["until"] = "break", now + BREAK
    r["dirty"] = True
    _send_view(r, now)


def _next_stage(r, now):
    r["stage"] += 1
    r["kills"] = set()
    r["phase"], r["until"] = "fight", 0.0
    if not r["fight_t"]:
        r["fight_t"] = now                                      # время забега — с первого этапа
    r["dirty"] = True
    _send_view(r, now)


def on_dead(loc, uid, now=None):
    """Сервер увидел смерть пилота в зале забега."""
    r = _runs.get(loc)
    if r and uid in r["members"] and r["phase"] in ("count", "fight", "break"):
        _finish(r, "dead", who=uid, now=now)


def leave(uid, now=None):
    """Пилот сам вышел из забега (портал выхода)."""
    r = run_of_uid(uid)
    if r:
        _finish(r, "left", who=uid, now=now)
    return r


def _finish(r, reason, who=None, now=None):
    if r["phase"] == "done":
        return
    now = _now(now)
    refund = reason == "gather" or (reason == "left" and r["stage"] == 0)     # до первого этапа попытка возвращается
    if refund:
        for uid in r["spent"]:
            if _used.get(uid) == _today():
                _used.pop(uid, None)
    res = {"reason": reason, "who": r["nick"].get(who, "") if who else "", "who_id": who or 0,
           "stage": r["stage"] if reason == "win" else r["cleared"], "win": reason == "win", "refund": refund, "prize": None}
    if reason == "win":
        w = random.choice(sorted(r["members"]))
        res["prize"] = {"uid": w, "nick": r["nick"].get(w, "Пилот"), **PRIZE}
        res["stage"] = len(STAGES)
    r["result"], r["phase"] = res, "done"
    r["until"] = now + (WIN_STAY if reason == "win" else END_STAY)
    r["dirty"] = True
    text = REASON_TEXT[reason].format(who=res["who"] or "Напарник")
    for uid in r["members"]:
        _out.append(("msg", uid, {"t": "cpend", "loc": r["loc"], **res, "text": text, "me": uid,
                                  "prize": dict(res["prize"]) if res["prize"] else None}))
        if reason != "win" and uid != who:
            _out.append(("home", uid, r["loc"], text))           # напарник сразу уходит в ангар
    _out.append(("end", r))


def view(r, now=None, inside=None):
    """Состояние забега для телефона (плашка этапа, барьеры, отсчёт, «Дождитесь напарника»)."""
    now = _now(now)
    inside = inside if inside is not None else r.get("_in", set())
    need = 1 if r["stage"] == BOSS_STAGE else PER
    kills = len(r["kills"]) if r["phase"] == "fight" else need if r["phase"] == "break" else 0
    wait = r["phase"] in ("gather", "count", "break") and len([u for u in r["members"] if u in inside]) < r["need"]
    v = {"t": "cprun", "loc": r["loc"], "phase": r["phase"], "stage": r["stage"], "cleared": r["cleared"],
         "stages": len(STAGES), "boss": r["stage"] == BOSS_STAGE, "kills": kills, "need": need, "wait": wait,
         "left": int(max(0.0, r["until"] - now) * 1000) if r["until"] else 0,
         "tl": int(max(0.0, r["fight_t"] + TIME_LIMIT - now) * 1000) if r["fight_t"] else TIME_LIMIT * 1000,
         "grace": GRACE,
         "members": [{"id": u, "nick": r["nick"].get(u, "Пилот"), "in": u in inside,
                      "away": 0 if u in inside or u not in r["away"] else int(now - r["away"][u])} for u in r["members"]]}
    if r["result"]:
        v["result"] = r["result"]
    return v


def _send_view(r, now):
    v = view(r, now)
    for uid in r["members"]:
        _out.append(("msg", uid, v))
    r["sent"], r["dirty"] = now, False


def tick(where, dead=lambda uid: False, now=None):
    """Раз в секунду. where(uid) — локация пилота в сети или None; dead(uid) — пилот сейчас мёртв."""
    now = _now(now)
    for loc, r in list(_runs.items()):
        if r["phase"] == "done":
            if now >= r["until"]:
                for uid in r["members"]:
                    if where(uid) == loc:
                        _out.append(("home", uid, loc, "Забег окончен — возврат в ангар"))
                _out.append(("reset", loc))
                _runs.pop(loc, None)
            continue
        inside = {u for u in r["members"] if where(u) == loc}
        if inside != r.get("_in"):
            r["_in"], r["dirty"] = inside, True
        for uid in r["members"]:
            if uid in inside:
                r["away"].pop(uid, None)
            else:
                r["away"].setdefault(uid, now)
        if r["phase"] != "gather":
            gone = next((u for u in r["members"] if now - r["away"].get(u, now) >= GRACE), None)
            if gone is not None:
                _finish(r, "left", who=gone, now=now)
                continue
            died = next((u for u in inside if dead(u)), None)
            if died is not None and r["phase"] in ("count", "fight", "break"):
                _finish(r, "dead", who=died, now=now)
                continue
        full = len(inside) >= r["need"]
        if r["phase"] == "gather":
            if full:
                r["phase"], r["until"], r["dirty"] = "count", now + COUNT, True
            elif now >= r["until"]:
                _finish(r, "gather", now=now)
                continue
        elif r["phase"] == "count":
            if not full:
                r["until"] = max(r["until"], now + 1)                # ждём напарника: отсчёт не кончается
            elif now >= r["until"]:
                _next_stage(r, now)
        elif r["phase"] == "break":
            if not full:
                r["until"] = max(r["until"], now + 1)
            elif now >= r["until"]:
                _next_stage(r, now)
        elif r["phase"] == "fight" and r["fight_t"] and now - r["fight_t"] >= TIME_LIMIT:
            _finish(r, "time", now=now)
            continue
        if r["dirty"] or now - r["sent"] >= 2:
            _send_view(r, now)


def drain():
    """Забрать накопленные сообщения и действия (webserver рассылает их в тике мира)."""
    out = _out[:]
    _out.clear()
    return out


def cleanup(online_ids, parties_alive, now=None):
    """Раз в минуту: вчерашние попытки и распавшиеся группы."""
    day = _today()
    for uid in [u for u, d in _used.items() if d != day]:
        _used.pop(uid, None)
    for pid in [p for p in _open if p not in parties_alive]:
        _open.pop(pid, None)
