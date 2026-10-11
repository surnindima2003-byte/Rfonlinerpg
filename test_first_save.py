"""Первое сохранение нового аккаунта (или первое после вайпа) проверяется по-настоящему.

Раньше время для него считалось с первого входа — до часа, и без замечаний проходили 4,3 млн лома и 7200 ядер.
Запуск: python -m unittest test_first_save
"""
import os
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

WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")
START = {"scrap": 30, "cores": 0, "inv": [{"id": "kit_s", "n": 3}], "store": [], "eq": {}}


def first_save(S, since_first_seen):
    dt = min(since_first_seen, saveguard.FIRST_SAVE_SEC)
    return saveguard.jumps({}, S, dt, {}, saveguard._base_caps())


class FirstSave(unittest.TestCase):
    def test_audit_numbers_are_caught(self):
        bad = dict(START, scrap=4_320_000, cores=7_200)
        notes = first_save(bad, since_first_seen=7 * 86400)         # аккаунт старый, сохранения ещё нет
        self.assertTrue(any(n.startswith("scrap") for n in notes), notes)
        self.assertTrue(any(n.startswith("cores") for n in notes), notes)

    def test_honest_start_passes(self):
        # сразу после обучения: стартовый лом + награды Искры, ремкомплекты, 1 сфера
        ok = dict(START, scrap=30 + 550, inv=[{"id": "kit_s", "n": 5}, {"id": "sph_cu", "n": 1}])
        self.assertEqual(first_save(ok, since_first_seen=40), [])
        self.assertEqual(first_save(ok, since_first_seen=30 * 86400), [])

    def test_two_minutes_of_farm_pass(self):
        ok = dict(START, scrap=100_000, cores=50)
        self.assertEqual(first_save(ok, since_first_seen=3600), [])

    def test_wired_into_api_save(self):
        fn = WEB[WEB.index("async def api_save"):WEB.index("async def api_ack")]
        self.assertIn("min(time.time() - first, saveguard.FIRST_SAVE_SEC)", fn)
        self.assertLessEqual(saveguard.FIRST_SAVE_SEC, 300)


if __name__ == "__main__":
    unittest.main()
