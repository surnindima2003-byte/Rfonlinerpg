"""Данные гильдии без постоянного опроса: сервер сам сообщает участникам о важных изменениях (сообщение gdb).

Раньше телефон опрашивал данные гильдии раз в 3 секунды всегда: ~1,7 запроса в секунду на игрока в гильдии
и 0,3 — на игрока без гильдии (список гильдий). Теперь в фоне — только гильдия и состав, раз в минуту.
Запуск: python -m unittest test_guild_sync
"""
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = (ROOT / "webserver.py").read_text(encoding="utf-8")
GAME = (ROOT / "game.html").read_text(encoding="utf-8")


class Conn:
    def __init__(self, uid, gid):
        self.info = {"id": uid, "gid": gid}
        self.got = []

    @property
    def uid(self):
        return self.info["id"]

    def push(self, payload):
        self.got.append(payload)
        return True


def load(conns):
    ns = {"hub": types.SimpleNamespace(conns={object(): c for c in conns}),
          "realtime": types.SimpleNamespace(encode=lambda p: p)}
    a = SRC.index("GUILD_STAT_KEYS = ")
    exec(SRC[a:SRC.index("async def refresh_guild", a)], ns)
    return ns


class WhoIsNotified(unittest.TestCase):
    def setUp(self):
        self.fn = load([])["guild_change_notifies"]

    def test_own_stats_every_minute_do_not_wake_the_guild(self):
        self.assertFalse(self.fn("update", "members", "5", "5", {"xp": 10, "lvl": 20, "bm": 900, "nick": "a"}))

    def test_membership_and_guild_changes_do(self):
        self.assertTrue(self.fn("set", "members", "7", "5", {"role": "member"}))      # принят
        self.assertTrue(self.fn("delete", "members", "7", "5", {}))                   # исключён / вышел
        self.assertTrue(self.fn("update", "members", "7", "5", {"role": "officer"}))  # роль
        self.assertTrue(self.fn("update", "members", "5", "5", {"role": "member"}))   # сам сменил роль
        self.assertTrue(self.fn("update", None, None, "5", {"desc": "новое"}))        # правка гильдии
        self.assertTrue(self.fn("delete", None, None, "5", {}))                       # роспуск

    def test_requests_and_log_are_tab_only(self):
        self.assertFalse(self.fn("set", "requests", "9", "9", {}))
        self.assertFalse(self.fn("add", "log", "a1", "5", {"text": "x"}))


class Delivery(unittest.TestCase):
    def test_members_online_and_named_players_get_the_signal(self):
        a, b, c, d = Conn(1, "g1"), Conn(2, "g1"), Conn(3, "g2"), Conn(4, "")
        ns = load([a, b, c, d])
        ns["notify_guild"]("g1", [4])                      # 4 — только что исключённый (уже без гильдии)
        for conn in (a, b, d):
            self.assertEqual(conn.got, [{"t": "gdb", "g": "g1"}])
        self.assertEqual(c.got, [])


class WiredUp(unittest.TestCase):
    def test_api_db_notifies_after_commit(self):
        fn = SRC[SRC.index("async def api_db"):]
        fn = fn[:fn.index("\n\n\n")]
        self.assertLess(fn.index("await s.commit()"), fn.index("notify_guild(gid, notify)"))

    def test_client_polls_fast_only_with_guild_tab_open(self):
        mk = GAME[GAME.index("function makeRemoteDb"):]
        mk = mk[:mk.index("\n}\n")]
        self.assertNotIn("setInterval(s.run, 3000)", mk)                         # прежний опрос всегда
        self.assertIn("gdbFast()", mk)
        self.assertIn('GDB.collection("guilds").orderBy("created", "desc").limit(100).onSnapshot', GAME)
        lst = GAME[GAME.index('GDB.collection("guilds").orderBy("created", "desc").limit(100).onSnapshot'):]
        self.assertIn("{bg:0}", lst[:lst.index("\n  }, () => {}") + 40])          # список гильдий — только во вкладке
        self.assertIn('d.t === "gdb"', GAME)


if __name__ == "__main__":
    unittest.main()
