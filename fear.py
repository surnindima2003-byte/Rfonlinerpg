"""«Страх» (как в LIBERTY): соло-подземелье с волнами монстров на выживание.

Монстров ведёт телефон игрока (game.html: buildArena, fearWave), их убийства сервер проверяет как обычно
(mobguard: монстры fw1…fw59 водятся только в arena_fear). Здесь — только вход:
  * с 10 уровня (уровень — серверный, тот же, что в info["lvl"]);
  * 2 попытки в сутки по Москве, попытка тратится при входе.
Без попыток сервер не переносит игрока в подземелье: телефон получает loc_fix и возвращается назад.
Переподключение посреди забега (связь пропала и вернулась) новой попыткой не считается: RETURN_SEC после
последнего сообщения из подземелья вход бесплатный. Выход в другую локацию это окно сразу закрывает.
Счётчики живут в памяти, как попытки Кровавой башни (tower.py): перезапуск сервера их обнуляет.
"""
import time
from datetime import datetime, timedelta, timezone

LOC = "arena_fear"
TRIES = 2
MIN_LVL = 10
RETURN_SEC = 90
MSK = timezone(timedelta(hours=3))

_used = {}       # uid -> [день по Москве, потрачено попыток]
_inside = {}     # uid -> время последнего сообщения из подземелья


def _today():
    return datetime.now(MSK).strftime("%Y-%m-%d")


def used(uid):
    d = _used.get(uid)
    return d[1] if d and d[0] == _today() else 0


def enter(uid, lvl, admin=False, now=None):
    """Игрок просится в подземелье. Возвращает (пустить, текст отказа)."""
    now = now or time.time()
    if now - _inside.get(uid, 0) < RETURN_SEC:
        _inside[uid] = now
        return True, ""                                   # вернулся в свой забег после обрыва связи
    if not admin and (lvl or 1) < MIN_LVL:
        return False, f"«Страх» открывается с {MIN_LVL} уровня"
    n = used(uid)
    if not admin and n >= TRIES:
        return False, "«Страх»: попытки на сегодня закончились"
    _used[uid] = [_today(), n + 1]
    _inside[uid] = now
    return True, ""


def seen_inside(uid, now=None):
    _inside[uid] = now or time.time()


def left(uid):
    """Игрок ушёл из подземелья (смерть, портал): следующий вход — новая попытка."""
    _inside.pop(uid, None)


def cleanup(now=None):
    """Раз в минуту: забываем вчерашние счётчики и давно закончившиеся забеги."""
    now = now or time.time()
    day = _today()
    for uid in [u for u, d in _used.items() if d[0] != day]:
        _used.pop(uid, None)
    for uid in [u for u, t in _inside.items() if now - t > RETURN_SEC]:
        _inside.pop(uid, None)
