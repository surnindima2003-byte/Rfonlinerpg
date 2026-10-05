"""Защита сохранений: паки магазина, общий разовый запас, счётчик крафтов.

Запуск: python -m unittest test_saveguard_packs
"""
import os
import re
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
if "metrics" not in sys.modules:
    try:
        import metrics  # noqa: F401
    except Exception:
        m = types.ModuleType("metrics"); m.inc = lambda *a, **k: None; m.gauge = lambda *a, **k: None
        sys.modules["metrics"] = m

import saveguard  # noqa: E402


def save(scrap=1000, cores=10, inv=None, eq=None):
    return {"S": {"scrap": scrap, "cores": cores, "exp": 5, "inv": list(inv or [{"id": "kit_s", "n": 5}]),
                  "store": [], "eq": eq or {"weapon": {"id": "g_weapon_1", "g": 1, "e": 3}}}, "bm": 100}


class PackContract(unittest.TestCase):
    """PACK_GRANTS совпадает с тем, что паки дают в game.html (лом, ядра, сферы, вещи с заточкой)."""

    def test_same_contents(self):
        game = (ROOT / "game.html").read_text(encoding="utf-8")
        start = game.index("const PACKS = [\n")
        blk = game[start:game.index("\n];", start)]
        client = {}
        for line in blk.split("\n"):
            m = re.search(r'SP\("(\w+)"', line)
            if not m:
                continue
            g = {}
            n = lambda rid: sum(int(x) for x in re.findall(r'\{id:"%s", n:(\d+)\}' % rid, line))
            if n("scrap"):
                g["scrap"] = n("scrap")
            if n("cores"):
                g["cores"] = n("cores")
            if n("sph_ti") + n("sph_cu"):
                g["sph"] = n("sph_ti") + n("sph_cu")
            if re.search(r'\{id:"(gset|wcls)"', line) or '"bookcls"' in line and '"wcls"' in line:
                g["ench"] = True
            if g:
                client[m.group(1)] = g
        self.assertEqual(client, saveguard.PACK_GRANTS)


class SlackAndPurchase(unittest.TestCase):
    def setUp(self):
        saveguard.MODE = "on"
        saveguard._slack.clear()
        saveguard._purchases.clear()

    def test_rapid_saves_cannot_print_scrap(self):
        # раньше каждое сохранение давало +20 000 лома «из воздуха»: 10 сохранений подряд = +200 000.
        # Теперь лишний лом срезается: за 10 сохранений по 1 с набирается не больше запаса + скорости фарма
        uid, cur = 77, 0
        for _ in range(10):
            data, notes = saveguard.check(uid, "x", save(scrap=cur), save(scrap=cur + 19_000), 1, {})
            cur = data["S"]["scrap"]
        self.assertLessEqual(cur, saveguard.SCRAP_BASE + saveguard.SCRAP_PER_SEC * 10)

    def test_rapid_saves_cannot_print_materials(self):
        uid, cur, rejected = 78, 0, 0
        for _ in range(10):
            data, notes = saveguard.check(uid, "x", save(inv=[{"id": "chip", "n": cur}]), save(inv=[{"id": "chip", "n": cur + 55}]), 1, {})
            if data is None:
                rejected += 1
            else:
                cur += 55
        self.assertGreaterEqual(rejected, 8)

    def test_slack_refills_with_time(self):
        uid = 79
        self.assertEqual(saveguard.check(uid, "x", save(scrap=0), save(scrap=19_000), 1, {})[1], [])
        st = saveguard._slack[uid]
        st["t"] -= saveguard.BASE_REFILL_SEC                  # прошло 5 минут
        self.assertEqual(saveguard.check(uid, "x", save(scrap=19_000), save(scrap=38_000), 1, {})[1], [])

    def test_rejected_save_does_not_spend_slack(self):
        uid = 80
        saveguard.check(uid, "x", save(scrap=0, inv=[{"id": "chip", "n": 0}]),
                        save(scrap=0, inv=[{"id": "chip", "n": 9_000}]), 1, {})            # отклонено (материалы)
        self.assertEqual(saveguard.check(uid, "x", save(scrap=0), save(scrap=19_000), 1, {})[1], [])

    def test_clamped_scrap_spends_slack(self):
        uid = 85
        saveguard.check(uid, "x", save(scrap=0), save(scrap=9_000_000), 1, {})           # срезано — запас израсходован
        data, notes = saveguard.check(uid, "x", save(scrap=0), save(scrap=19_000), 1, {})
        self.assertLess(data["S"]["scrap"], 19_000)

    def test_purchase_allows_only_pack_contents(self):
        uid = 81
        saveguard.note_purchase(uid, "p_start")                # +10 000 лома
        data, notes = saveguard.check(uid, "x", save(scrap=0), save(scrap=5_000_000), 10, {})
        # раньше покупка отключала проверку целиком; теперь лом сверх пака и запаса срезается
        self.assertLessEqual(data["S"]["scrap"], 10_000 + saveguard.SCRAP_BASE + saveguard.SCRAP_PER_SEC * 10)
        saveguard._slack.clear()
        saveguard.note_purchase(uid, "p_legend")               # +1 000 000 лома
        data, notes = saveguard.check(uid, "x", save(scrap=0), save(scrap=1_000_000), 10, {})
        self.assertEqual(notes, [])
        self.assertNotIn(uid, saveguard._purchases)           # учтена один раз

    def test_purchase_with_enchanted_gear(self):
        uid = 82
        saveguard.note_purchase(uid, "d_top")                  # вещи +9
        new = save(eq={"weapon": {"id": "g_weapon_9", "g": 3, "e": 9}})
        self.assertEqual(saveguard.check(uid, "x", save(), new, 10, {})[1], [])

    def test_first_save_checked_against_empty(self):
        # сервер теперь сравнивает первое сохранение с пустым (api_save передаёт {"S": {}})
        data, notes = saveguard.check(83, "x", {"S": {}}, save(scrap=5_000_000), 60, {})
        self.assertLessEqual(data["S"]["scrap"], saveguard.SCRAP_BASE + saveguard.SCRAP_PER_SEC * 60)
        data, notes = saveguard.check(84, "x", {"S": {}}, save(scrap=30, cores=0), 60, {})
        self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main()
