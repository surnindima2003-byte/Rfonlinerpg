"""Личность игрока в живом мире задаёт сервер: ник, гильдия, нижний предел уровня, запрет побега из боя.

Проверки по исходникам (webserver.py тянет aiohttp и базу) плюс чистая функция похожих букв.
Запуск: python -m unittest test_pvp_identity
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")


def _fn(src, name):
    body = src[src.index(name):]
    return body[:body.index("\ndef ", 10) if "\ndef " in body[10:] else len(body)]


class Lookalikes(unittest.TestCase):
    def setUp(self):
        a = WEB.index("RESERVED_NICKS = {"); b = WEB.index("def valid_nick")
        self.ns = {}
        exec(WEB[a:b], self.ns)

    def test_reserved_with_lookalikes(self):
        r = self.ns["reserved_nick"]
        for n in ("admin", "Admin", "Аdmin", "аdmin", "АДМИН", "Adm1n", "a_d-m i n", "Модератор", "Moдератор", "Pilot", "пилот"):
            self.assertTrue(r(n), n)
        for n in ("Titan", "Admiral", "Сталевар", "Ghost_7"):
            self.assertFalse(r(n), n)


class ServerAuthority(unittest.TestCase):
    def test_clean_pos_ignores_client_identity(self):
        fn = WEB[WEB.index("def clean_pos"):WEB.index("PUBLIC_KEYS =")]
        for bad in ('d.get("nick"', 'd.get("gt"', 'd.get("gn"', 'd.get("gi"', 'd.get("gc"'):
            self.assertNotIn(bad, fn)
        self.assertIn("cached_floor", fn)

    def test_allies_by_guild_id(self):
        self.assertNotIn('info.get("gt") == tgt.get("gt")', WEB)
        self.assertEqual(len(re.findall(r"same_guild\(info, tgt\)", WEB)), 2)

    def test_flee_blocked(self):
        ws = WEB[WEB.index("async def ws_handler"):]
        self.assertIn("in_pvp_combat(info)", ws[:ws.index("clean_pos(d, info)")])


if __name__ == "__main__":
    unittest.main()
