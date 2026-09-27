"""Смерть засчитывается только после серверных ударов убийцы и только один раз."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pvp  # noqa: E402


class PvpDedupeTest(unittest.TestCase):
    def setUp(self):
        pvp._hits.clear()
        pvp._deaths.clear()

    def test_no_hits_no_kill(self):
        self.assertFalse(pvp.claim_death(2, 1))

    def test_single_credit(self):
        pvp.record_hit(1, 2, 50)
        self.assertTrue(pvp.claim_death(2, 1))
        pvp.record_hit(1, 2, 50)
        self.assertFalse(pvp.claim_death(2, 1))          # повтор сразу после — дубль

    def test_old_hits_expire(self):
        with mock.patch("pvp.time.time", return_value=1000.0):
            pvp.record_hit(1, 2, 50)
        with mock.patch("pvp.time.time", return_value=1000.0 + pvp.HIT_WINDOW + 1):
            self.assertFalse(pvp.claim_death(2, 1))

    def test_cleanup(self):
        with mock.patch("pvp.time.time", return_value=1000.0):
            pvp.record_hit(1, 2, 50)
            pvp._pair_t[(1, 2)] = 1000.0
        with mock.patch("pvp.time.time", return_value=5000.0):
            pvp.cleanup(set())
        self.assertEqual(pvp._hits, {})
        self.assertEqual(pvp._pair_t, {})


if __name__ == "__main__":
    unittest.main()
