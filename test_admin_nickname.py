"""Смена позывного админом: клиент ходит через /api/admin/grant (kind: name), сервер это понимает,
старый адрес /api/admin/name оставлен для закэшированных клиентов. Защита от возврата ошибки 404."""
import re
import unittest
from pathlib import Path


def _root():
    p = Path(__file__).resolve().parent
    for cand in (p, p.parent):
        if (cand / "game.html").exists():
            return cand
    raise FileNotFoundError("game.html")


ROOT = _root()
GAME = (ROOT / "game.html").read_text(encoding="utf-8")
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")


class AdminNicknameContract(unittest.TestCase):
    def test_client_uses_grant_route(self):
        self.assertRegex(GAME, r'api\("/api/admin/grant",\s*\{kind:\s*"name"')
        self.assertNotIn('api("/api/admin/name"', GAME)

    def test_server_handles_name_kind_before_grant_kinds(self):
        body = WEB[WEB.index("async def api_admin_grant"):]
        body = body[:body.index("\nasync def ", 10)]
        self.assertLess(body.index('kind == "name"'), body.index("GRANT_KINDS"))
        self.assertIn("admin_set_name(user, body)", body)

    def test_legacy_route_kept(self):
        self.assertIn('add_post("/api/admin/name", api_admin_name)', WEB)

    def test_validation_present(self):
        fn = WEB[WEB.index("async def admin_set_name"):]
        fn = fn[:fn.index("\nasync def ", 10)]
        for part in ("valid_nick(name)", "RESERVED_NICKS", "func.lower(GameSave.nick) == name.lower()", '"rename"'):
            self.assertIn(part, fn)


if __name__ == "__main__":
    unittest.main()
