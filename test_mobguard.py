"""Учёт боя с мобами: убийство засчитывается только после ударов, которые видел сервер."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mobguard  # noqa: E402


class MobGuardTest(unittest.TestCase):
    def setUp(self):
        mobguard._fights.clear()
        mobguard._budget.clear()
        mobguard._boss_last.clear()
        mobguard._rejects.clear()
        mobguard.MODE = "on"

    def test_hp_matches_game_formula(self):
        self.assertEqual(mobguard.mob_hp("scrap_crawler"), 20)
        self.assertEqual(mobguard.mob_hp("dg1"), 34)                 # round(20 + 14 + 0.25)
        self.assertEqual(mobguard.mob_hp("dg4"), round((20 + 56 + 4) * 1.25))   # шагоход ×1,25
        self.assertEqual(mobguard.mob_hp("db4"), mobguard.mob_hp("dg4") * 10)
        self.assertIsNone(mobguard.mob_hp("dg41"))
        self.assertIsNone(mobguard.mob_hp("dragon"))

    def test_kill_without_hits_rejected(self):
        self.assertFalse(mobguard.allow_kill(1, 5, "scrap_crawler", now=100))

    def test_kill_after_hits_accepted_once(self):
        mobguard.on_hits(1, 5, [[5, "scrap_crawler", 25]], now=100)
        self.assertTrue(mobguard.allow_kill(1, 5, "scrap_crawler", now=101))
        self.assertFalse(mobguard.allow_kill(1, 5, "scrap_crawler", now=101))   # второй раз тот же бой — нет

    def test_low_damage_rejected(self):
        mobguard.on_hits(1, 5, [[7, "sentry_bot", 10]], now=100)
        self.assertEqual(mobguard.check_kill(1, 7, "sentry_bot", now=101), "low_damage")

    def test_mob_type_must_match(self):
        mobguard.on_hits(1, 5, [[8, "scrap_crawler", 25]], now=100)
        self.assertEqual(mobguard.check_kill(1, 8, "war_walker", now=101), "mob_mismatch")

    def test_damage_budget_limits_instant_boss(self):
        hp = mobguard.mob_hp("db40")
        mobguard.on_hits(1, 1, [[9, "db40", hp]], now=100)          # уровень 1 не наносит 12 тыс. урона мгновенно
        self.assertEqual(mobguard.check_kill(1, 9, "db40", now=100), "low_damage")

    def test_boss_respawn(self):
        hp = mobguard.mob_hp("db2")
        mobguard.on_hits(1, 40, [[10, "db2", hp]], now=1000)
        self.assertTrue(mobguard.allow_kill(1, 10, "db2", now=1000))
        mobguard.on_hits(1, 40, [[11, "db2", hp]], now=1100)
        self.assertFalse(mobguard.allow_kill(1, 11, "db2", now=1100))   # тот же главарь через 100 с

    def test_shadow_mode_counts_but_allows(self):
        mobguard.MODE = "shadow"
        self.assertTrue(mobguard.allow_kill(2, 1, "rogue_drone", now=100))
        self.assertEqual(dict(mobguard.top_rejects()), {2: 1})

    def test_garbage_ignored(self):
        self.assertEqual(mobguard.on_hits(1, 5, [["x"], None, [1, "dragon", 5], [-1, "dg1", 5]], now=1), 0)

    def test_cleanup_forgets_old_fights(self):
        mobguard.on_hits(3, 5, [[1, "dg1", 5]], now=100)
        mobguard.cleanup(set(), now=100 + mobguard.FIGHT_TTL + 1)
        self.assertNotIn(3, mobguard._fights)


if __name__ == "__main__":
    unittest.main()
