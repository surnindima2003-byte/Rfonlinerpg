"""Награды сюжетных и ежедневных квестов через сервер (quests.py).

* таблицы сюжета, заданий дня и формулы наград совпадают с game.html;
* сюжетный квест — один раз, «достигни N уровня» — только при допустимом сервером уровне;
* ежедневные — не больше трёх разных заданий в день, бонус — после трёх и один раз;
* дата телефона принимается только ±1 день от даты сервера;
* телефон просит награду у сервера (в Telegram), а в демо вне Telegram выдаёт сам, как раньше.
Запуск: python -m unittest test_quests
"""
import json
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for name in ("aiohttp", "sqlalchemy", "db", "models", "metrics", "db_atomic"):
    try:
        __import__(name)
    except Exception:
        sys.modules[name] = mock.MagicMock()

import quests  # noqa: E402

GAME = (ROOT / "game.html").read_text(encoding="utf-8")
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")


def day(shift=0):
    d = datetime.now(timezone.utc) + timedelta(days=shift)
    return f"{d.year}-{d.month}-{d.day}"


@unittest.skipUnless(shutil.which("node"), "нет node")
class MirrorGame(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        head = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8")
        head = head[:head.index("\n", head.index("const run = "))]
        js = head + r'''
process.stdout.write(run(`JSON.stringify({
  story: STORY_FLAT.map(q => [q.t, q.n, q.r.xp || 0, q.r.scrap || 0, q.r.kit || 0]),
  daily: Object.fromEntries(Object.entries(QUEST_TYPES).map(([k, q]) => [k, q.min || 0])),
  rew: Object.keys(QUEST_TYPES).flatMap(k => [1, 7, 33, 50].map(lv => [k, lv, qReward(k, lv)]))
})`));
'''
        res = subprocess.run(["node", "-", str(ROOT / "game.html")], input=js, capture_output=True, text=True, timeout=300)
        if res.returncode:
            raise AssertionError(res.stderr[-2000:])
        cls.g = json.loads(res.stdout)

    def test_story(self):
        self.assertEqual([list(x) for x in quests.STORY], self.g["story"])

    def test_daily_types(self):
        self.assertEqual(quests.DAILY, self.g["daily"])

    def test_daily_rewards(self):
        for k, lv, r in self.g["rew"]:
            self.assertEqual(quests.daily_reward(k, lv), r, (k, lv))

    def test_bonus_formula(self):
        self.assertIn("const rw = {sph:1, cores:5, scrap:100 + (S.level || 1)*20};", GAME)
        self.assertEqual(quests.bonus_reward(10), {"sph": 1, "cores": 5, "scrap": 300})


class Rules(unittest.TestCase):
    def test_story_once_and_level(self):
        st = {"si": 0, "days": {}}
        self.assertIsNone(quests.check_story(st, 0, 1))
        st["si"] = 5
        self.assertEqual(quests.check_story(st, 4, 50), "Награда уже получена")
        self.assertEqual(quests.check_story(st, 5, 2), "Нужен 3 уровень")        # «Опытный боец»: 3 уровень
        self.assertIsNone(quests.check_story(st, 5, 3))
        self.assertEqual(quests.check_story(st, 45, 50), "Нет такого квеста")
        self.assertEqual(quests.check_story(st, True, 50), "Нет такого квеста")

    def test_daily_limits(self):
        st, d = {"si": 0, "days": {}}, day()
        self.assertEqual(quests.check_daily(st, d, "boss", 2), "Задание откроется с 3 уровня")
        self.assertEqual(quests.check_daily(st, d, "fly", 10), "Нет такого задания")
        self.assertEqual(quests.check_bonus(st, d), "Сначала забери награды трёх заданий")
        for k in ("kill", "loot", "boss"):
            self.assertIsNone(quests.check_daily(st, d, k, 10))
            st["days"][d]["t"].append(k)
        self.assertEqual(quests.check_daily(st, d, "kill", 10), "Награда уже получена")
        self.assertEqual(quests.check_daily(st, d, "wave", 10), "Сегодня уже получены награды за три задания")
        self.assertIsNone(quests.check_bonus(st, d))
        st["days"][d]["b"] = True
        self.assertEqual(quests.check_bonus(st, d), "Награда уже получена")
        self.assertIsNone(quests.check_daily(st, day(1), "kill", 10))       # другой день — свои три задания

    def test_day_window(self):
        self.assertEqual(quests.parse_day(day()), day())
        self.assertIsNotNone(quests.parse_day(day(-1)))
        self.assertIsNotNone(quests.parse_day(day(1)))
        self.assertIsNone(quests.parse_day(day(3)))
        self.assertIsNone(quests.parse_day("вчера"))
        self.assertEqual(quests._clean_days({day(): {}, day(-5): {}}), {day(): {}})

    def test_credit_goes_to_ledger_and_saves(self):
        got = []
        with mock.patch("ledger.add", lambda uid, inc: got.append(("led", inc))), \
             mock.patch("saveguard.note_grant", lambda uid, g: got.append(("save", g))):
            quests._credit(7, quests.bonus_reward(10))
        self.assertEqual(got, [("led", {"scrap": 300, "cores": 5}), ("save", {"scrap": 300, "cores": 5, "sph": 1})])


class Wiring(unittest.TestCase):
    def test_server(self):
        self.assertIn("import quests", WEB)
        self.assertIn("quests.setup(app, read_auth)", WEB)

    def test_client(self):
        self.assertIn('NET.api("/api/quests/claim", body)', GAME)
        self.assertIn('qSrv({kind:"daily", day:q.day, type:t.id})', GAME)
        self.assertIn('qSrv({kind:"bonus", day:q.day})', GAME)
        self.assertIn('qSrv({kind:"story", idx})', GAME)
        self.assertEqual(GAME.count("qDailyClaim(+b.dataset.qc"), 2)
        self.assertNotIn("t.got = true; qGive(t.r); save(); toast", GAME)        # старая выдача без сервера


if __name__ == "__main__":
    unittest.main()
