"""Проверка сохранений: честная игра проходит, подделка замечается."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if "metrics" not in sys.modules:
    try:
        import metrics  # noqa: F401
    except Exception:
        m = types.ModuleType("metrics"); m.inc = lambda *a, **k: None; m.gauge = lambda *a, **k: None
        sys.modules["metrics"] = m

import saveguard  # noqa: E402


def save(scrap=1000, cores=10, inv=None, eq=None, sph=0):
    inv = list(inv or [{"id": "kit_s", "n": 5}])
    if sph:
        inv.append({"id": "sph_cu", "n": sph})
    return {"S": {"scrap": scrap, "cores": cores, "exp": 5, "inv": inv,
                  "store": [], "eq": eq or {"weapon": {"id": "g_weapon_1", "g": 1, "e": 3}}}, "bm": 100}


class SaveGuardTest(unittest.TestCase):
    def setUp(self):
        saveguard.MODE = "on"
        saveguard.recent.clear()

    def test_honest_play_passes(self):
        data, notes = saveguard.check(1, "a", save(), save(scrap=4000, cores=14), 10, {})
        self.assertEqual(notes, [])
        self.assertIsNotNone(data)

    def test_grants_raise_limit(self):
        data, notes = saveguard.check(1, "a", save(), save(scrap=200_000), 10, {"scrap": 200_000})
        self.assertEqual(notes, [])

    def test_scrap_jump_clamped_in_on_mode(self):
        # лом сверх допустимого срезается, остальное сохранение принимается (раньше отклонялось целиком)
        fix = {}
        data, notes = saveguard.check(1, "a", save(), save(scrap=5_000_000), 10, {}, fix_out=fix)
        self.assertIsNotNone(data)
        limit = 1000 + saveguard.SCRAP_PER_SEC * 10 + saveguard.SCRAP_BASE
        self.assertLessEqual(data["S"]["scrap"], limit)
        self.assertEqual(fix, {"scrap": data["S"]["scrap"]})
        self.assertTrue(any(a == "исправлено" for a, _ in notes))

    def test_sphere_jump_still_rejected(self):
        # сферы (и материалы, заточку) срезать нельзя — такое сохранение по-прежнему отклоняется
        data, notes = saveguard.check(1, "a", save(), save(sph=500), 10, {})
        self.assertIsNone(data)
        self.assertTrue(any(a == "скачок" for a, _ in notes))

    def test_shadow_mode_only_logs(self):
        saveguard.MODE = "shadow"
        data, notes = saveguard.check(1, "a", save(), save(scrap=5_000_000), 10, {})
        self.assertIsNotNone(data)
        self.assertTrue(notes)
        self.assertEqual(len(saveguard.recent), 1)

    def test_long_offline_allows_more(self):
        data, notes = saveguard.check(1, "a", save(), save(scrap=1_000_000), 3600, {})
        self.assertEqual(notes, [])

    def test_bad_structure_fixed(self):
        bad = save(eq={"weapon": {"id": "g_weapon_1", "g": 7, "e": 99}}, inv=[{"id": "kit_s", "n": -5}])
        data, notes = saveguard.check(1, "a", None, bad, 10, {})
        self.assertTrue(any(a == "структура" for a, _ in notes))
        w = data["S"]["eq"]["weapon"]
        self.assertEqual((w["e"], w["g"]), (15, 3))
        self.assertEqual(data["S"]["inv"][0]["n"], 0)

    def test_enchant_jump(self):
        data, notes = saveguard.check(1, "a", save(), save(eq={"weapon": {"id": "g_weapon_1", "g": 1, "e": 12}}), 10, {})
        self.assertIsNone(data)

    def test_first_save_without_old(self):
        data, notes = saveguard.check(1, "a", None, save(scrap=5_000_000), 10, {})
        self.assertEqual(notes, [])


class SphereTest(unittest.TestCase):
    def setUp(self):
        saveguard.MODE = "on"

    def w(self, e):
        return {"weapon": {"id": "g_weapon_1", "g": 1, "e": e}}

    def test_buying_spheres_with_scrap_ok(self):
        # купил 100 медных сфер за 15 000 лома
        data, notes = saveguard.check(1, "a", save(scrap=20_000, sph=0), save(scrap=5_000, sph=100), 10, {})
        self.assertEqual(notes, [])

    def test_spheres_from_nothing_flagged(self):
        data, notes = saveguard.check(1, "a", save(scrap=0, sph=0), save(scrap=0, sph=5000), 10, {})
        self.assertTrue(any("сферы" in b for _, b in notes))

    def test_enchant_spends_spheres_ok(self):
        # потратил 3 сферы, заточка +3
        data, notes = saveguard.check(1, "a", save(sph=10, eq=self.w(3)), save(sph=7, eq=self.w(6)), 10, {})
        self.assertEqual(notes, [])

    def test_many_items_enchanted_without_spheres(self):
        # 60 вещей разом +3 и ни одной потраченной сферы (с учётом всего лома, что мог прийти за 10 с)
        inv_old = [{"id": f"g_armor_{i}", "n": 1, "g": 1, "e": 0} for i in range(60)]
        inv_new = [{"id": f"g_armor_{i}", "n": 1, "g": 1, "e": 4} for i in range(60)]
        o, n = save(scrap=0, inv=inv_old), save(scrap=0, inv=inv_new)
        o["S"]["store"], n["S"]["store"] = [dict(x) for x in inv_old], [dict(x) for x in inv_new]   # ещё 60 вещей на складе
        data, notes = saveguard.check(1, "a", o, n, 10, {})
        self.assertTrue(any("потраченных сферах" in b for _, b in notes))

    def test_granted_spheres_count(self):
        data, notes = saveguard.check(1, "a", save(scrap=0), save(scrap=0, sph=500), 10, {"sph": 500})
        self.assertEqual(notes, [])


class MaterialJumps(unittest.TestCase):
    """Из материалов сервер собирает руны и дронов на продажу — их резкий прирост замечается."""
    def test_honest_drop_passes(self):
        old, new = save(inv=[{"id": "chip", "n": 5}]), save(inv=[{"id": "chip", "n": 30}])
        self.assertEqual([j for j in saveguard.jumps(old["S"], new["S"], 10, {}) if "chip" in j], [])

    def test_forged_materials_flagged(self):
        old, new = save(inv=[{"id": "chip", "n": 5}]), save(inv=[{"id": "chip", "n": 5000}])
        self.assertTrue(any("chip" in j for j in saveguard.jumps(old["S"], new["S"], 10, {})))


if __name__ == "__main__":
    unittest.main()
