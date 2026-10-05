"""Роли по Telegram id (закрепление username), срок initData, очистка временных данных.

Запуск: python -m unittest test_security
"""
import asyncio
import logging
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")


def roles(admin_names=("boss",), admin_ids=()):
    ns = {"time": time, "asyncio": asyncio, "log": logging.getLogger("t"), "json": __import__("json"),
          "ADMIN_USERNAMES": set(admin_names), "ADMIN_IDS": set(admin_ids)}
    a = WEB.index('ROLE_PINS_KEY = "role_pins"'); b = WEB.index("async def load_role_pins")
    c = WEB.index("def admin_ids():"); d = WEB.index("def _parse_init")
    exec(WEB[a:b] + WEB[c:d], ns)
    saved = []

    async def save():
        saved.append(dict(ns["_role_pins"]))
    ns["save_role_pins"] = save
    return ns


class RolePins(unittest.TestCase):
    def test_username_pinned_to_first_id(self):
        ns = roles()
        ok = ns["role_ok"]
        self.assertTrue(ok("boss", 111, {"boss"}, set()))          # админ зашёл первым — закрепили
        self.assertTrue(ok("boss", 111, {"boss"}, set()))
        self.assertFalse(ok("boss", 999, {"boss"}, set()))         # тот же username у другого человека — нет прав
        self.assertEqual(ns["admin_ids"](), {111})

    def test_admin_ids_always_win(self):
        ns = roles(admin_ids=(555,))
        self.assertTrue(ns["role_ok"]("whoever", 555, {"boss"}, {555}))
        self.assertFalse(ns["role_ok"]("whoever", 556, {"boss"}, {555}))
        self.assertIn(555, ns["admin_ids"]())

    def test_auth_uses_role_ok_and_max_age(self):
        fn = WEB[WEB.index("def auth(init_data: str):"):WEB.index("def auth_expired")]
        self.assertIn("role_ok(username, u.id, ADMIN_USERNAMES, ADMIN_IDS)", fn)
        self.assertIn("INITDATA_MAX_AGE", fn)
        self.assertNotIn("7 * 86400", WEB)
        self.assertNotIn('"admin": username in ADMIN_USERNAMES', WEB)

    def test_backup_by_ids(self):
        b = (ROOT / "backup.py").read_text(encoding="utf-8")
        fn = b[b.index("async def send_to_admins"):b.index("async def backup_loop")]
        self.assertIn("admin_ids()", fn)
        self.assertNotIn("GameSave.username", fn)


class SessionExpiry(unittest.TestCase):
    def test_server_and_client(self):
        self.assertIn("session expired", WEB)
        self.assertIn("code=4003", WEB)
        game = (ROOT / "game.html").read_text(encoding="utf-8")
        self.assertIn("ev.code === 4003", game)
        self.assertIn('t.includes("session expired")', game)


class Cleanup(unittest.TestCase):
    def test_all_wired(self):
        loop = WEB[WEB.index("async def cleanup_loop"):WEB.index("async def start_web")]
        for call in ("items.cleanup(online_ids)", "progress.cleanup(online_ids)", "seasonpts.cleanup(online_ids)",
                     "stats.cleanup()", "vip.cleanup()", "worldboss.cleanup()", "tower.cleanup()", "special_quests.cleanup()"):
            self.assertIn(call, loop)

    def test_season_cleanup_waits_for_flush(self):
        src = (ROOT / "seasonpts.py").read_text(encoding="utf-8")
        fn = src[src.index("def cleanup(online_ids)"):]
        self.assertIn("_flush_lock.locked()", fn)


class SaveRejectHandling(unittest.TestCase):
    def test_server_passes_fix_and_reason(self):
        fn = WEB[WEB.index("async def api_save"):WEB.index("async def api_ack")]
        self.assertIn("fix_out=fix", fn)
        self.assertIn('"what": what', fn)
        self.assertIn('{"ok": True, "fix": fix}', fn)

    def test_client_applies_fix_and_explains_reject(self):
        game = (ROOT / "game.html").read_text(encoding="utf-8")
        self.assertIn("if(r && r.ok && r.fix)", game)
        self.assertIn("Сервер не принял сохранение", game)
        self.assertIn("rejUntil = Date.now() + 60000", game)
        self.assertIn("adSaves(adStats.saves)", game)


class ClientErrors(unittest.TestCase):
    def test_dt_never_negative(self):
        game = (ROOT / "game.html").read_text(encoding="utf-8")
        self.assertIn("dt = Math.max(0, Math.min(0.05, raw/1000))", game)
        self.assertIn("data-aderrclear", game)
        self.assertIn('e.message === "Script error." && !e.filename', game)


if __name__ == "__main__":
    unittest.main()
