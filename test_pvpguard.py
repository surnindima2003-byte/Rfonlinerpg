"""Серверный учёт CP и прочности в PvP и проверка скорости."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pvpguard  # noqa: E402

LVL = 46


def player(uid=1, hp=1000, mhp=1000, cp=500, mcp=10000, df=20, lvl=46):
    info = {"id": uid, "hp": hp, "mhp": mhp, "cp": cp, "mcp": mcp, "df": df, "lvl": lvl, "x": 100.0, "y": 100.0, "loc": "iron_canyon"}
    pvpguard.sanitize(info, LVL)
    return info


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class PvpGuardTest(unittest.TestCase):
    def setUp(self):
        pvpguard._state.clear()
        self.clock = Clock()
        self.p = mock.patch("pvpguard._now", self.clock)
        self.p.start()
        pvpguard.PVP_MODE = "on"
        pvpguard.MOVE_MODE = "on"

    def tearDown(self):
        self.p.stop()

    def test_caps(self):
        v = player(mhp=10_000_000, hp=10_000_000, cp=10**9, mcp=10**9, df=10**6)
        max_hp, _, max_def = pvpguard.caps(LVL)
        self.assertEqual(v["mhp"], max_hp)
        self.assertLessEqual(v["cp"], v["mhp"] * 10 + pvpguard.CP_PER_POINT * pvpguard.UP_MAX)   # + улучшение «CP»
        self.assertEqual(v["df_eff"], max_def)

    def test_cp_absorbs_first(self):
        v = player(cp=500, hp=1000, df=0)
        self.assertFalse(pvpguard.on_hit(v, 300))
        self.assertEqual((v["cp"], v["hp"]), (200, 1000))
        pvpguard.on_hit(v, 400)                      # 200 в CP, 200 × 0,55 в прочность
        self.assertEqual(v["cp"], 0)
        self.assertEqual(v["hp"], 890)

    def test_god_mode_client_dies_on_server(self):
        v = player(cp=0, hp=300, df=0)
        dead = False
        for _ in range(60):
            self.clock.t += 0.5
            v["hp"], v["cp"] = 300, 0                # изменённый телефон всё время шлёт полную прочность
            pvpguard.on_report(v)
            dead = pvpguard.on_hit(v, 100) or dead
        self.assertTrue(dead)

    def test_honest_regen_allowed(self):
        v = player(cp=0, hp=1000, df=0)
        pvpguard.on_hit(v, 1000)                     # -550 на сервере
        self.clock.t += 2
        v["hp"] = 600                                # телефон: 450 + ремкомплект 150
        pvpguard.on_report(v)
        self.assertEqual(v["hp"], 600)

    def test_ally_heal_restores_cp_while_hp_full(self):
        """Лечение союзника, пока прочность полная (удары съели только CP), сервер засчитывает в CP ×10."""
        v = player(cp=5000, hp=1000, df=0)
        pvpguard.on_hit(v, 3000)                     # CP 2000, прочность 1000
        pvpguard.heal(1, 100, 1000, 10000, 10)       # союзник-Ремонтник: +100 → CP +1000
        self.assertEqual(int(pvpguard._state[1]["cp"]), 3000)
        self.clock.t += 0.2
        v["hp"], v["cp"] = 1000, 3000                # телефон показывает то же
        pvpguard.on_report(v)
        self.assertEqual((v["hp"], v["cp"]), (1000, 3000))
        self.assertFalse(pvpguard.on_hit(v, 2500))   # раньше сервер считал CP 2000 и этот удар уходил в прочность
        self.assertEqual(v["hp"], 1000)

    def test_heal_before_phone_knows(self):
        """Отчёт телефона пришёл раньше, чем он узнал о лечении: следующий отчёт с лечением всё равно принимается."""
        v = player(cp=5000, hp=1000, df=0)
        pvpguard.on_hit(v, 3000)                     # CP 2000
        pvpguard.heal(1, 100, 1000, 10000, 10)       # сервер: CP 3000
        self.clock.t += 0.1
        v["hp"], v["cp"] = 1000, 2000                # старый отчёт — без лечения
        pvpguard.on_report(v)
        self.assertEqual(v["cp"], 2000)
        self.clock.t += 0.2
        v["hp"], v["cp"] = 1000, 3000                # телефон получил «healed»
        pvpguard.on_report(v)
        self.assertEqual(v["cp"], 3000)
        self.clock.t += 0.2
        v["hp"], v["cp"] = 1000, 9000                # больше полученного лечения — не принимается
        pvpguard.on_report(v)
        self.assertEqual(v["cp"], 3000)

    def test_heal_credit_expires(self):
        v = player(cp=5000, hp=1000, df=0)
        pvpguard.on_hit(v, 3000)
        pvpguard.heal(1, 100, 1000, 10000, 10)
        v["hp"], v["cp"] = 1000, 2000
        pvpguard.on_report(v)
        self.clock.t += pvpguard.CP_CREDIT_SEC + 1
        pvpguard.on_hit(v, 1)                        # бой продолжается
        v["hp"], v["cp"] = 1000, 2999
        pvpguard.on_report(v)
        self.assertEqual(v["cp"], 1999)

    def test_self_heal_cp_x1_and_no_double_count(self):
        v = player(cp=0, hp=1000, df=0)
        pvpguard.on_hit(v, 1000)                     # прочность 450
        pvpguard.heal(1, 100, 1000, 10000, 1)        # себе: прочность +100, CP +100
        st = pvpguard._state[1]
        self.assertEqual((int(st["hp"]), int(st["cp"])), (550, 100))
        self.clock.t += 0.2
        v["hp"], v["cp"] = 550, 100
        pvpguard.on_report(v)
        self.assertEqual((v["hp"], v["cp"]), (550, 100))
        v["cp"] = 1100                               # прирост прочности уже засчитан — второй раз ×10 не даётся:
        pvpguard.on_report(v)                        # сверх отчёта — не больше самого лечения (кредит, 3 с)
        self.assertEqual(v["cp"], 200)
        self.clock.t += pvpguard.CP_CREDIT_SEC + 1
        pvpguard.on_hit(v, 1)
        v["cp"] = 1100
        pvpguard.on_report(v)
        self.assertEqual(v["cp"], 199)

    def test_heal_capped_by_max_cp(self):
        v = player(cp=9900, hp=1000, df=0, mcp=10000)
        pvpguard.on_hit(v, 100)
        pvpguard.heal(1, 500, 1000, 10000, 10)
        self.assertEqual(int(pvpguard._state[1]["cp"]), 10000)

    def test_hit_rate_honest_fast_sniper_passes(self):
        """Сильный Снайпер с «Разгоном» честно бьёт 6,7 раза в секунду — сервер не срезает (раньше пропускал 3,3)."""
        info = {"cls": "sniper"}
        ok = 0
        for _ in range(67):
            self.clock.t += 1 / 6.7
            ok += pvpguard.hit_rate_ok(info, self.clock.t)
        self.assertEqual(ok, 67)

    def test_hit_rate_cheat_guard_cut(self):
        """Страж честно бьёт не чаще 3,6 раза в секунду; ускоренный в 3 раза телефон получает не больше ~4,2."""
        info = {"cls": "guard"}
        ok = 0
        for _ in range(110):
            self.clock.t += 1 / 11
            ok += pvpguard.hit_rate_ok(info, self.clock.t)
        self.assertLessEqual(ok, 3.61 * 1.15 * 10 + pvpguard.HIT_BURST + 1)
        self.assertGreaterEqual(ok, 3.61 * 10)

    def test_hit_rate_network_bunch(self):
        """Удары после задержки сети приходят пачкой — до трёх подряд принимаются."""
        info = {"cls": "medic"}
        self.assertTrue(pvpguard.hit_rate_ok(info, self.clock.t))
        self.clock.t += 1.0
        self.assertEqual(sum(pvpguard.hit_rate_ok(info, self.clock.t) for _ in range(3)), 3)

    def test_skill_spam_cut(self):
        """Изменённый телефон шлёт «Шоковый удар» каждые 0,8 с: за 30 с проходит не больше честного числа умений,
        а оглушений — не больше, чем позволяют перезарядки двух оглушающих умений Стража."""
        info = {"cls": "guard"}
        ok = stuns = 0
        for _ in range(37):
            self.clock.t += 0.8
            a, st, _sl = pvpguard.skill_check(info, True, False, self.clock.t)
            ok += a
            stuns += st
        honest = sum(int(30 // cd) + 1 for cd, _, _ in pvpguard.SKILLS["guard"])          # 3 + 5 + 3 = 11
        self.assertLessEqual(ok, honest + 1)
        self.assertLessEqual(stuns, int(30 // (7.84 * 0.95)) + 1 + int(30 // (8.56 * 0.95)) + 1)
        self.assertLess(stuns, 37 // 3)

    def test_skill_honest_rotation_passes(self):
        """Честный Страж с наибольшим сокращением перезарядки: каждое умение по откату — всё принимается, оглушения тоже."""
        info = {"cls": "guard"}
        nxt = {i: 0.0 for i in range(3)}
        t0 = self.clock.t
        got = want = stun_got = stun_want = 0
        while self.clock.t - t0 < 120:
            self.clock.t += 0.05
            for i, (cd, st, sl) in enumerate(pvpguard.SKILLS["guard"]):
                if self.clock.t - t0 >= nxt[i]:
                    nxt[i] = self.clock.t - t0 + cd
                    a, s_ok, _ = pvpguard.skill_check(info, bool(st), bool(sl), self.clock.t)
                    got += a; want += 1; stun_got += s_ok; stun_want += bool(st)
                    break
        self.assertEqual(got, want)
        self.assertEqual(stun_got, stun_want)

    def test_skill_without_class(self):
        self.assertEqual(pvpguard.skill_check({"cls": ""}, False, False, self.clock.t), (False, False, False))

    def test_combat_ends(self):
        v = player(cp=0, hp=1000, df=0)
        pvpguard.on_hit(v, 1000)
        self.clock.t += pvpguard.COMBAT_SEC + 1
        v["hp"] = 1000
        pvpguard.on_report(v)
        self.assertEqual(v["hp"], 1000)

    def test_move_guard(self):
        i = player()
        self.assertTrue(pvpguard.check_move(i, 100, 100, "iron_canyon", False))
        self.clock.t += 0.125
        self.assertTrue(pvpguard.check_move(i, 140, 100, "iron_canyon", False))     # обычный шаг
        self.assertFalse(pvpguard.check_move(i, 3000, 100, "iron_canyon", False))   # телепорт через карту
        self.assertTrue(pvpguard.check_move(i, 3000, 100, "lobby", False))          # смена локации — можно


if __name__ == "__main__":
    unittest.main()
