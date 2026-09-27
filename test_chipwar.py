"""Chip War: очки, оспаривание, закреплённая фракция, расписание."""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chipwar  # noqa: E402

X, Y, _ = chipwar.ZONE


def p(uid, fac, dx=0, lvl=15, dead=False, loc=None):
    return {"id": uid, "fac": fac, "x": X + dx, "y": Y, "lvl": lvl, "dead": dead, "loc": loc or chipwar.LOC}


class ChipWarTest(unittest.TestCase):
    def setUp(self):
        self.w = chipwar.War()
        self.w.begin(0, 600)

    def test_single_faction_scores_capped(self):
        self.w.tick([p(i, "vex") for i in range(5)])
        self.assertEqual(self.w.sc["vex"], chipwar.PER_PLAYER_MAX)
        self.assertEqual(self.w.hold, "vex")

    def test_contested_no_points(self):
        self.w.tick([p(1, "vex"), p(2, "core")])
        self.assertEqual(self.w.hold, "contested")
        self.assertEqual(sum(self.w.sc.values()), 0)

    def test_ineligible_ignored(self):
        self.w.tick([p(1, "vex", lvl=5), p(2, "core", dead=True), p(3, "aegis", dx=1000), p(4, "", ), p(5, "vex", loc="lobby")])
        self.assertIsNone(self.w.hold)

    def test_faction_locked_for_event(self):
        self.w.tick([p(1, "vex")])
        self.w.tick([p(1, "core")])                  # подменённая фракция не засчитывается
        self.assertEqual(self.w.sc, {"aegis": 0, "vex": 2, "core": 0})

    def test_target_and_winner(self):
        done = False
        for _ in range(chipwar.TARGET):
            done = self.w.tick([p(1, "aegis"), p(2, "aegis"), p(3, "aegis")])
            if done:
                break
        self.assertTrue(done)
        self.assertEqual(self.w.finish(), "aegis")

    def test_tie_has_no_winner(self):
        self.w.sc.update(aegis=10, vex=10)
        self.assertIsNone(self.w.finish())

    def test_rewards_need_time_in_zone(self):
        for _ in range(chipwar.REWARD_MIN_SEC):
            self.w.tick([p(1, "vex")])
        self.w.tick([p(2, "core")])
        self.w.finish()
        self.assertEqual([(u, won) for u, _, _, won in self.w.rewards()], [(1, True)])

    def test_schedule_next_start(self):
        # вторник 2026-09-29 18:00 МСК = 15:00 UTC; ближайший слот «tue 19:00» — через час
        now = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc).timestamp()
        nxt = chipwar.next_start(now, "tue 19:00, sat 18:00", tz=3)
        self.assertEqual(datetime.fromtimestamp(nxt, timezone.utc), datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc))
        after = chipwar.next_start(nxt, "tue 19:00, sat 18:00", tz=3)
        self.assertEqual(datetime.fromtimestamp(after, timezone.utc), datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc))

    def test_bad_schedule(self):
        self.assertIsNone(chipwar.next_start(0, "когда-нибудь"))


if __name__ == "__main__":
    unittest.main()
