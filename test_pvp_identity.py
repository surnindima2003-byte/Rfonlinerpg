"""Личность игрока в живом мире задаёт сервер: ник, гильдия, нижний предел уровня, запрет побега из боя.

Проверки по исходникам (webserver.py тянет aiohttp и базу) плюс чистая функция похожих букв.
Запуск: python -m unittest test_pvp_identity
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")
PVP = (ROOT / "pvp.py").read_text(encoding="utf-8")
GAME = (ROOT / "game.html").read_text(encoding="utf-8")


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


class PartyXp(unittest.TestCase):
    def test_client_amount_ignored(self):
        fn = WEB[WEB.index('elif t == "pxp":'):]
        fn = fn[:fn.index("\n\n")]
        self.assertNotIn('d.get("amount"', fn)
        self.assertIn("return", fn)

    def test_server_shares_on_verified_kills(self):
        hk = WEB[WEB.index("async def handle_kills"):]
        hk = hk[:hk.index("\nasync def ", 10)]
        self.assertIn("process_kills(s, info[\"id\"], info, d.get(\"mk\"), exp)", hk)
        self.assertIn("share_party_xp(info, sum(exp))", hk)

    def test_client_does_not_send_pxp(self):
        self.assertNotIn('netSend({t:"pxp"', GAME)


class RatingFarm(unittest.TestCase):
    def setUp(self):
        import time
        a = PVP.index("PAIR_DAILY ="); b = PVP.index("_pair_day = {}")
        c = PVP.index("def rating_allowed"); d = PVP.index("def on_hit(")
        self.ns = {"time": time}
        exec(PVP[a:b] + "_pair_day = {}\n" + PVP[c:d], self.ns)

    def test_pair_limited_per_day(self):
        ok = self.ns["rating_allowed"]
        got = [ok(1, 2, 1000, 1000, now=86400 * 10 + 5) for _ in range(5)]
        self.assertEqual(got, [True, True, False, False, False])
        self.assertTrue(ok(1, 2, 1000, 1000, now=86400 * 11 + 5))          # новые сутки
        self.assertTrue(ok(1, 3, 1000, 1000, now=86400 * 10 + 5))          # другой соперник

    def test_farmed_alt_gives_nothing(self):
        self.assertFalse(self.ns["rating_allowed"](1, 2, 1500, 1000))

    def test_wired_into_on_death(self):
        od = PVP[PVP.index("async def on_death"):]
        self.assertIn("rating_allowed(killer[\"id\"], victim[\"id\"], k.rating, v.rating, now)", od[:od.index("\nasync def ", 10)])


if __name__ == "__main__":
    unittest.main()
