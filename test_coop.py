"""«Сотрудничество»: правила забега (coop.py), общие мобы зала (mobworld, mobguard, ledger), группа и вход на настоящем
коде webserver.py (как test_party) и зеркало в game.html (константы, раскладка, окно события, переводы).

Запуск: python -m unittest test_coop
"""
import asyncio
import random
import re
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
if "metrics" not in sys.modules:
    try:
        import metrics  # noqa: F401
    except Exception:
        m = types.ModuleType("metrics"); m.inc = m.gauge = m.observe = lambda *a, **k: None
        sys.modules["metrics"] = m

import coop       # noqa: E402
import elite      # noqa: E402
import ledger     # noqa: E402
import mobguard   # noqa: E402
import mobworld   # noqa: E402

GAME = (ROOT / "game.html").read_text(encoding="utf-8")
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")


def reset():
    coop._used.clear(); coop._runs.clear(); coop._open.clear(); coop._out.clear()


class Where:
    """Где пилоты: uid -> локация (None — не в сети), и кто мёртв."""
    def __init__(self):
        self.loc, self.dead = {}, set()

    def where(self, uid):
        return self.loc.get(uid)

    def is_dead(self, uid):
        return uid in self.dead


def msgs(out, t):
    return [(a[1], a[2]) for a in out if a[0] == "msg" and a[2].get("t") == t]


class RunRules(unittest.TestCase):
    def setUp(self):
        reset()
        self.w = Where()
        self.infos = {1: {"nick": "A", "lvl": 30}, 2: {"nick": "B", "lvl": 12}}

    def begin(self, now=1000.0):
        loc = coop.start("p1", [1, 2], self.infos, now=now)
        self.assertIn(loc, coop.LOCS)
        self.w.loc = {1: loc, 2: loc}
        coop.tick(self.w.where, self.w.is_dead, now=now + 1)              # оба в зале — отсчёт
        coop.tick(self.w.where, self.w.is_dead, now=now + 2 + coop.COUNT)   # первый этап
        r = coop.run_of_loc(loc)
        self.assertEqual((r["phase"], r["stage"]), ("fight", 1))
        return loc, r

    def kill_stage(self, loc, r, now):
        s = r["stage"]
        for i in range((s - 1) * coop.PER, s * coop.PER):
            coop.on_kill(loc, i, coop.mob_type(i), 1, now=now)

    def test_layout_of_mobs(self):
        self.assertEqual(coop.mob_type(0), "cw10")
        self.assertEqual(coop.mob_type(39), "cw10")
        self.assertEqual(coop.mob_type(40), "cw15")
        self.assertEqual(coop.mob_type(319), "cw60")
        self.assertEqual(coop.mob_type(coop.BOSS_IDX), "cb40")
        self.assertIsNone(coop.mob_type(coop.BOSS_IDX + 1))
        self.assertEqual([coop.stage_of(i) for i in (0, 40, 319, 320)], [1, 2, 8, coop.BOSS_STAGE])
        self.assertEqual(coop.BOSS_IDX, 8 * 40)

    def test_entry_checks(self):
        self.assertIn("2", coop.check_enter(1, [1], self.infos))                        # нужна пара
        self.assertIsNone(coop.check_enter(1, [1, 2], self.infos))
        self.infos[2]["lvl"] = 9
        self.assertIn("10", coop.check_enter(1, [1, 2], self.infos))
        self.infos[2]["lvl"] = 10
        self.assertIn("не в сети", coop.check_enter(1, [1, 2], {1: self.infos[1], 2: None}))
        self.assertIn("лидер", coop.check_enter(3, [1, 2], self.infos))
        self.assertIn("бою", coop.check_enter(1, [1, 2], self.infos, busy=lambda u: u == 2))
        self.infos[2]["dead"] = True
        self.assertIn("погиб", coop.check_enter(1, [1, 2], self.infos))
        self.assertIsNone(coop.check_enter(1, [1], {1: self.infos[1]}, admin=True))     # админ проверяет зал один

    def test_attempt_is_spent_once_a_day(self):
        loc = coop.start("p1", [1, 2], self.infos, now=0)
        self.assertEqual((coop.left(1), coop.left(2)), (0, 0))
        go = msgs(coop.drain(), "cp_go")
        self.assertEqual(sorted(u for u, _ in go), [1, 2])                             # лидер затягивает обоих
        self.assertIn("попытка", coop.check_enter(1, [1, 2], self.infos))
        self.assertIsNone(coop.start("p1", [1, 2], self.infos, now=1))                  # у группы уже идёт забег
        self.assertTrue(coop.member_of(loc, 2))
        self.assertFalse(coop.member_of(loc, 3))

    def test_slots(self):
        locs = [coop.start(f"p{i}", [i * 2, i * 2 + 1], now=0) for i in range(coop.SLOTS)]
        self.assertEqual(sorted(locs), sorted(coop.LOCS))
        self.assertIsNone(coop.start("extra", [98, 99], now=0))                          # все залы заняты

    def test_partner_not_coming_returns_attempts(self):
        loc = coop.start("p1", [1, 2], self.infos, now=0)
        self.w.loc = {1: loc, 2: "lobby"}
        coop.tick(self.w.where, now=coop.GATHER - 1)
        self.assertEqual(coop.run_of_loc(loc)["phase"], "gather")
        coop.drain()
        coop.tick(self.w.where, now=coop.GATHER + 1)
        out = coop.drain()
        end = msgs(out, "cpend")
        self.assertTrue(end and end[0][1]["refund"] and end[0][1]["reason"] == "gather")
        self.assertEqual((coop.left(1), coop.left(2)), (1, 1))                          # попытки вернулись
        self.assertIn(("home", 1, loc, end[0][1]["text"]), out)

    def test_stage_needs_all_forty_and_both_present(self):
        loc, r = self.begin()
        for i in range(0, 39):
            coop.on_kill(loc, i, "cw10", 1, now=1010)
        self.assertEqual(r["phase"], "fight")
        coop.on_kill(loc, 39, "cw15", 1, now=1010)                                       # чужой тип — не в счёт
        coop.on_kill(loc, 45, "cw15", 1, now=1010)                                       # моб закрытого этапа — не в счёт
        self.assertEqual(r["phase"], "fight")
        coop.on_kill(loc, 39, "cw10", 2, now=1011)
        self.assertEqual((r["phase"], r["cleared"]), ("break", 1))
        self.w.loc[2] = None                                                             # напарник отвалился — ждём
        coop.tick(self.w.where, now=1011 + coop.BREAK + 1)
        self.assertEqual(r["phase"], "break")
        self.assertTrue(coop.view(r, now=1016)["wait"])                                  # «Дождитесь напарника»
        self.w.loc[2] = loc
        coop.tick(self.w.where, now=1011 + coop.BREAK + 3)
        self.assertEqual((r["phase"], r["stage"]), ("fight", 2))
        self.assertTrue(coop.allow_hit(loc, 40, "cw15"))
        self.assertFalse(coop.allow_hit(loc, 0, "cw10"))                                 # пройденный этап
        self.assertFalse(coop.allow_hit(loc, 80, "cw20"))                                # следующий — ещё закрыт
        self.assertFalse(coop.allow_hit(loc, 40, "cw60"))                                # тип не свой

    def test_full_run_and_prize_to_one_of_two(self):
        random.seed(7)
        loc, r = self.begin()
        t = 1100.0
        for s in range(1, 9):
            self.assertEqual(r["stage"], s)
            self.kill_stage(loc, r, t)
            t += coop.BREAK + 1
            coop.tick(self.w.where, now=t)
        self.assertEqual((r["phase"], r["stage"]), ("fight", coop.BOSS_STAGE))
        self.assertTrue(coop.allow_hit(loc, coop.BOSS_IDX, "cb40"))
        coop.drain()
        coop.on_kill(loc, coop.BOSS_IDX, "cb40", 2, now=t + 30)
        out = coop.drain()
        end = msgs(out, "cpend")
        self.assertEqual(len(end), 2)
        res = end[0][1]
        self.assertTrue(res["win"])
        self.assertIn(res["prize"]["uid"], (1, 2))
        self.assertEqual((res["prize"]["item"], res["prize"]["n"], res["prize"]["cores"]), ("sph_ti", 1, 100))
        self.assertEqual(res["stage"], 8)
        self.assertEqual([a for a in out if a[0] == "end"][0][1], r)                     # webserver выдаст награду
        self.assertFalse([a for a in out if a[0] == "home"])                             # после победы — время собрать добычу
        coop.tick(self.w.where, now=t + 30 + coop.WIN_STAY + 1)
        out = coop.drain()
        self.assertEqual(sorted(a[1] for a in out if a[0] == "home"), [1, 2])
        self.assertIn(("reset", loc), out)
        self.assertIsNone(coop.run_of_loc(loc))                                          # зал свободен

    def test_prize_is_random(self):
        winners = set()
        for seed in range(30):
            reset()
            random.seed(seed)
            loc = coop.start("p", [1, 2], self.infos, now=0)
            r = coop.run_of_loc(loc)
            r.update(phase="fight", stage=coop.BOSS_STAGE)
            coop.on_kill(loc, coop.BOSS_IDX, "cb40", 1, now=1)
            winners.add(r["result"]["prize"]["uid"])
        self.assertEqual(winners, {1, 2})                                                # жребий, а не «тот, кто добил»

    def test_death_ends_for_both(self):
        loc, r = self.begin()
        coop.drain()
        coop.on_dead(loc, 2, now=1020)
        out = coop.drain()
        end = msgs(out, "cpend")
        self.assertEqual(sorted(u for u, _ in end), [1, 2])
        self.assertEqual((end[0][1]["reason"], end[0][1]["who"], end[0][1]["refund"]), ("dead", "B", False))
        homes = [a for a in out if a[0] == "home"]
        self.assertEqual([a[1] for a in homes], [1])                                     # напарник сразу уходит домой
        self.assertIn("погиб", homes[0][3])
        self.assertEqual(coop.left(1), 0)                                                # попытка после начала — не вернётся

    def test_death_seen_by_tick(self):
        loc, r = self.begin()
        self.w.dead.add(1)
        coop.tick(self.w.where, self.w.is_dead, now=1030)
        self.assertEqual(r["result"]["reason"], "dead")

    def test_partner_left(self):
        loc, r = self.begin()
        self.w.loc[2] = "lobby"
        coop.tick(self.w.where, now=1020)
        self.assertEqual(r["phase"], "fight")                                            # обрыв связи — ждём GRACE
        coop.tick(self.w.where, now=1020 + coop.GRACE)
        self.assertEqual((r["result"]["reason"], r["result"]["who"]), ("left", "B"))
        self.assertIn("вышел", msgs(coop.drain(), "cpend")[0][1]["text"])                # «напарник вышел», а не «погиб»

    def test_short_absence_is_forgiven(self):
        loc, r = self.begin()
        self.w.loc[2] = None
        coop.tick(self.w.where, now=1020)
        self.w.loc[2] = loc
        coop.tick(self.w.where, now=1020 + coop.GRACE - 1)
        coop.tick(self.w.where, now=1020 + coop.GRACE + 5)
        self.assertEqual(r["phase"], "fight")

    def test_leaving_before_first_stage_refunds(self):
        loc = coop.start("p1", [1, 2], self.infos, now=0)
        self.w.loc = {1: loc, 2: loc}
        coop.tick(self.w.where, now=1)                                                   # отсчёт
        coop.leave(2, now=2)
        r = coop.run_of_loc(loc)
        self.assertTrue(r["result"]["refund"])
        self.assertEqual((coop.left(1), coop.left(2)), (1, 1))

    def test_time_limit(self):
        loc, r = self.begin()
        coop.tick(self.w.where, now=r["fight_t"] + coop.TIME_LIMIT + 1)
        self.assertEqual(r["result"]["reason"], "time")

    def test_view_for_phone(self):
        loc, r = self.begin()
        coop.on_kill(loc, 3, "cw10", 1, now=1010)
        v = coop.view(r, now=1011)
        self.assertEqual((v["t"], v["stage"], v["kills"], v["need"], v["stages"]), ("cprun", 1, 1, 40, 8))
        self.assertGreater(v["tl"], 0)
        self.assertEqual(sorted(m["id"] for m in v["members"]), [1, 2])

    def test_partners_and_cleanup(self):
        loc, r = self.begin()
        self.assertTrue(coop.partners(1, 2))
        self.assertFalse(coop.partners(1, 3))
        coop.set_open("gone", True)
        coop._used[77] = "2000-01-01"
        coop.cleanup(set(), {"p1"})
        self.assertFalse(coop.is_open("gone"))
        self.assertNotIn(77, coop._used)


class ServerMobs(unittest.TestCase):
    def setUp(self):
        reset()
        for d in (mobworld._mobs, mobworld._dirty, mobworld._own, mobworld._moved, mobworld._budget):
            d.clear()
        mobworld.set_rules(coop.PREFIX, coop.allow_hit, coop.on_kill)

    def test_levels_hp_and_places(self):
        for lv in coop.STAGES:
            self.assertEqual(mobguard.mob_level(f"cw{lv}", "coop3"), lv)
            self.assertIsNone(mobguard.mob_level(f"cw{lv}", "sector2"))                 # монстры события — только в зале
            self.assertEqual(mobguard.mob_hp(f"cw{lv}"), int((20 + 14 * lv + 0.25 * lv * lv) * 0.5 + 0.5))
        self.assertIsNone(mobguard.mob_level("cw11", "coop1"))
        self.assertEqual(mobguard.mob_level("cb40", "coop8"), 40)
        self.assertIsNone(mobguard.mob_level("cb40", "elite1"))
        self.assertEqual(mobguard.mob_hp("cb40"), mobguard.mob_hp("dg40") * mobguard.COOP_BOSS_HP)
        self.assertEqual(mobguard.COOP_LOCS, coop.LOCS)
        self.assertEqual(mobguard.COOP_STAGES, coop.STAGES)
        self.assertEqual(mobguard.COOP_BOSS, coop.BOSS_LVL)

    def test_ledger(self):
        for s, lv in enumerate(coop.STAGES):
            L, scrap, loot, boss = ledger.mob_info(f"cw{lv}")
            self.assertEqual((L, scrap, boss), (lv, (lv + 1, 2 * lv + 4), False))
            self.assertEqual(loot, {k: round(v * 0.6, 3) for k, v in ledger.NPCS[ledger.COOP_BASE[s]][2].items()})
        L, scrap, loot, boss = ledger.mob_info("cb40")
        dl, dscrap, dloot, dboss = ledger.mob_info("db40")
        self.assertEqual((L, scrap, loot, boss), (dl, dscrap, dloot, dboss))            # как главарь подземелья 40 ур.
        self.assertEqual(ledger.COOP_STAGES, coop.STAGES)

    def hub(self):
        self.sent = []
        return types.SimpleNamespace(to_loc=lambda loc, p, skip_uid=None: self.sent.append(p), by_loc={})

    def test_shared_mobs_follow_the_stage(self):
        hub = self.hub()
        loc = coop.start("p", [1, 2], now=0)
        r = coop.run_of_loc(loc)
        r.update(phase="fight", stage=1)
        low = {"id": 1, "loc": loc, "lvl": 10}
        hp = mobguard.mob_hp("cw10")
        mobworld.on_hits(low, {"loc": loc, "h": [[45, "cw15", 999]]}, hub)               # закрытый этап — мимо
        mobworld.on_hits(low, {"loc": loc, "h": [[5, "cw60", 999]]}, hub)                # тип не тот — мимо
        self.assertFalse([p for p in self.sent if p["t"] == "mdie"])
        mobworld.on_hits(low, {"loc": loc, "h": [[5, "cw10", hp]]}, hub)
        die = [p for p in self.sent if p["t"] == "mdie"]
        self.assertEqual((die[0]["i"], die[0]["by"]), (5, 1))
        self.assertIn(5, r["kills"])                                                     # coop.py посчитал убийство
        self.assertGreater(mobworld._mobs[loc][5]["dead_until"], time.time() + 3600)      # в зале забега не возрождается

    def test_high_stage_mob_takes_damage_from_low_pilot(self):
        hub = self.hub()
        loc = coop.start("p", [1, 2], now=0)
        coop.run_of_loc(loc).update(phase="fight", stage=8)
        mobworld.on_hits({"id": 1, "loc": loc, "lvl": 10}, {"loc": loc, "h": [[300, "cw60", 100]]}, hub)
        self.assertEqual(mobworld._mobs[loc][300]["hp"], mobguard.mob_hp("cw60") - 100)   # без этого моб «умирал» только у себя

    def test_no_regen_and_reset(self):
        hub = self.hub()
        loc = coop.start("p", [1, 2], now=0)
        coop.run_of_loc(loc).update(phase="fight", stage=1)
        mobworld.on_hits({"id": 1, "loc": loc, "lvl": 30}, {"loc": loc, "h": [[2, "cw10", 10]]}, hub)
        mobworld._mobs[loc][2]["last"] -= mobworld.REGEN_AFTER + 5
        hub.by_loc[loc] = [types.SimpleNamespace(uid=1)]
        mobworld.tick(hub)
        self.assertEqual(mobworld._mobs[loc][2]["hp"], mobguard.mob_hp("cw10") - 10)       # монстры события не лечатся
        mobworld.reset(loc)
        self.assertNotIn(loc, mobworld._mobs)

    def test_other_zones_unchanged(self):
        hub = self.hub()
        mobworld.on_hits({"id": 1, "loc": "sector2", "lvl": 10}, {"loc": "sector2", "h": [[1, "dg30", 50]]}, hub)
        m = mobworld._mobs.get("sector2", {}).get(1)
        self.assertTrue(m is None or m["hp"] == m["max"])                                # LVL_GAP вне зала забега — как раньше

    def test_webserver_wiring(self):
        self.assertIn("LOCS |= set(coop.LOCS)", WEB)
        self.assertIn("SAFE_LOCS |= set(coop.LOCS)", WEB)                               # в зале PvP нет
        self.assertIn("mobworld.set_rules(coop.PREFIX, coop.allow_hit, coop.on_kill)", WEB)
        self.assertIn("coop.tick(coop_where, coop_dead)", WEB)
        self.assertIn("await coop_pump()", WEB)
        self.assertIn("d = coop_pos_guard(d, info, conn, old_loc)", WEB)
        self.assertIn("await coop_after_pos(info, old_loc, conn)", WEB)
        self.assertIn("or coop.partners(uid, other)", WEB)                              # Ремонтник лечит напарника
        self.assertIn("coop.cleanup(online_ids, set(parties))", WEB)


def load_web():
    ns = {"time": time, "asyncio": asyncio, "elite": elite, "coop": coop, "mobworld": mobworld,
          "metrics": types.SimpleNamespace(inc=lambda *a, **k: None),
          "pvpguard": types.SimpleNamespace(in_combat=lambda *a: False, heal=lambda *a: None, combat_left=lambda *a: 0)}
    a, b = WEB.index("PARTY_MAX = 4"), WEB.index("CARD_NUM = {")
    exec(WEB[a:b], ns)
    online_, sent, rewards = {}, [], []

    async def push(uid, payload):
        sent.append((uid, payload))
        return True

    async def reward(r):
        rewards.append(r["result"]["prize"])

    hub = types.SimpleNamespace(to_uid=lambda uid, p: sent.append((uid, p)), info_of=lambda uid: online_.get(uid), by_uid={}, by_loc={})
    ns.update(hub=hub, push_to_player=push, online=lambda uid: online_.get(uid), realtime=types.SimpleNamespace(encode=lambda p: p),
              in_pvp_combat=lambda i: False, coop_reward=reward)
    for uid in range(1, 7):
        online_[uid] = {"id": uid, "nick": f"p{uid}", "lvl": 20, "cls": "", "loc": "lobby", "x": 500, "y": 640, "dead": False}
    return ns, online_, sent, rewards


class GroupAndEntry(unittest.TestCase):
    def setUp(self):
        reset()
        self.ns, self.on, self.sent, self.rewards = load_web()
        self.pushed = []
        self.conn = types.SimpleNamespace(push=lambda p: self.pushed.append(p))

    def op(self, uid, op, **kw):
        asyncio.run(self.ns["handle_coop"]({"t": "coop", "op": op, **kw}, self.on[uid], self.conn))
        return self.pushed[-1]

    def pinfo(self):
        return [p["text"] for p in self.pushed if isinstance(p, dict) and p.get("t") == "pinfo"]

    def test_create_join_enter(self):
        st = self.op(1, "create")
        pid = self.ns["member_party"][1]
        self.assertTrue(self.ns["parties"][pid]["coop"])
        self.assertTrue(coop.is_open(pid))
        self.assertEqual((st["t"], st["my"]["pid"], st["left"]), ("coop", pid, 1))
        self.assertEqual([g["pid"] for g in self.op(2, "state")["open"]], [pid])          # видна в «Открытых группах»
        self.op(2, "join", pid=pid)
        self.assertEqual(sorted(self.ns["parties"][pid]["members"]), [1, 2])
        self.assertFalse(coop.is_open(pid))                                              # вдвоём — набор закрыт
        self.op(3, "join", pid=pid)
        self.assertNotIn(3, self.ns["member_party"])                                     # третий не помещается
        self.op(2, "enter")
        self.assertIsNone(coop.run_of_pid(pid))                                          # начать может только лидер
        self.op(1, "enter")
        loc = coop.run_of_pid(pid)
        self.assertIn(loc, coop.LOCS)
        go = sorted(u for u, p in self.sent if isinstance(p, dict) and p.get("t") == "cp_go" and p["loc"] == loc)
        self.assertEqual(go, [1, 2])
        self.assertEqual(self.op(1, "state")["left"], 0)

    def test_level_and_tries(self):
        self.on[5]["lvl"] = 9
        self.op(5, "create")
        self.assertNotIn(5, self.ns["member_party"])
        self.assertIn("10", self.pinfo()[-1])
        coop._used[4] = coop._today()
        self.op(4, "create")
        self.assertIn("использована", self.pinfo()[-1])

    def test_party_cap_is_two(self):
        self.op(1, "create")
        pid = self.ns["member_party"][1]
        for u in (2, 3):
            asyncio.run(self.ns["handle_party"]({"t": "pinv", "to": u}, self.on[1]))
            asyncio.run(self.ns["handle_party"]({"t": "pacc", "to": 1}, self.on[u]))
        self.assertEqual(len(self.ns["parties"][pid]["members"]), coop.SIZE)
        self.assertFalse(coop.is_open(pid))

    def test_big_party_cannot_be_coop(self):
        for u in (2, 3):
            asyncio.run(self.ns["handle_party"]({"t": "pinv", "to": u}, self.on[1]))
            asyncio.run(self.ns["handle_party"]({"t": "pacc", "to": 1}, self.on[u]))
        self.op(1, "create")
        pid = self.ns["member_party"][1]
        self.assertFalse(self.ns["parties"][pid].get("coop"))
        self.assertIn("вдвоём", self.pinfo()[-1])

    def test_elite_and_coop_do_not_mix(self):
        self.op(1, "create")
        asyncio.run(self.ns["handle_elite"]({"t": "elite", "op": "create"}, {**self.on[1], "lvl": 30}, self.conn))
        pid = self.ns["member_party"][1]
        self.assertFalse(self.ns["parties"][pid].get("elite"))

    def test_lone_leader_keeps_group(self):
        self.op(1, "create")
        pid = self.ns["member_party"][1]
        self.op(2, "join", pid=pid)
        self.op(2, "leave")
        self.assertIn(pid, self.ns["parties"])
        self.assertTrue(coop.is_open(pid))
        self.op(1, "leave")
        self.assertNotIn(pid, self.ns["parties"])

    def test_pos_guard_and_rejoin_after_reconnect(self):
        self.op(1, "create")
        pid = self.ns["member_party"][1]
        self.op(2, "join", pid=pid)
        self.op(1, "enter")
        loc = coop.run_of_pid(pid)
        d = self.ns["coop_pos_guard"]({"loc": loc, "x": 1, "y": 1}, self.on[3], self.conn, "lobby")
        self.assertEqual(d["loc"], "lobby")                                              # чужой в зал не войдёт
        fix = [p for p in self.pushed if isinstance(p, dict) and p.get("t") == "loc_fix"]
        self.assertTrue(fix)
        d = self.ns["coop_pos_guard"]({"loc": loc, "x": 1, "y": 1}, self.on[2], self.conn, "lobby")
        self.assertEqual(d["loc"], loc)
        # связь у 2 оборвалась: сервер вывел его из пати, группа у лидера осталась
        asyncio.run(self.ns["party_leave"](2))
        self.assertNotIn(2, self.ns["member_party"])
        self.assertEqual(self.ns["parties"][pid]["members"], [1])
        self.on[2]["loc"] = loc
        asyncio.run(self.ns["coop_after_pos"](self.on[2], "lobby", self.conn))
        self.assertEqual(self.ns["member_party"].get(2), pid)                            # вернулся — снова в группе
        self.assertTrue([p for p in self.pushed if isinstance(p, dict) and p.get("t") == "cprun"])

    def test_death_in_hall_and_pump(self):
        self.op(1, "create")
        pid = self.ns["member_party"][1]
        self.op(2, "join", pid=pid)
        self.op(1, "enter")
        loc = coop.run_of_pid(pid)
        r = coop.run_of_loc(loc)
        r.update(phase="fight", stage=3)
        for u in (1, 2):
            self.on[u]["loc"] = loc
        self.on[2]["dead"] = True
        asyncio.run(self.ns["coop_after_pos"](self.on[2], loc, self.conn))
        self.sent.clear()
        asyncio.run(self.ns["coop_pump"]())
        ends = [u for u, p in self.sent if isinstance(p, dict) and p.get("t") == "cpend"]
        self.assertEqual(sorted(ends), [1, 2])
        fixes = [u for u, p in self.sent if isinstance(p, dict) and p.get("t") == "loc_fix"]
        self.assertEqual(fixes, [1])                                                     # живого напарника — сразу в ангар

    def test_win_triggers_reward(self):
        self.op(1, "create")
        pid = self.ns["member_party"][1]
        self.op(2, "join", pid=pid)
        self.op(1, "enter")
        loc = coop.run_of_pid(pid)
        coop.run_of_loc(loc).update(phase="fight", stage=coop.BOSS_STAGE)
        coop.on_kill(loc, coop.BOSS_IDX, "cb40", 1)

        async def go():
            await self.ns["coop_pump"]()
            await asyncio.sleep(0)
        asyncio.run(go())
        self.assertEqual(len(self.rewards), 1)
        self.assertIn(self.rewards[0]["uid"], (1, 2))

    def test_reward_writes_grants_and_registers_sphere(self):
        src = WEB[WEB.index("async def coop_reward(r):"):WEB.index("async def handle_party(d, info):")]
        self.assertIn('kind="item"', src)
        self.assertIn('"reg": True', src)
        self.assertIn("await items.add_spheres(s, uid, pz[\"item\"], pz[\"n\"])", src)  # учтённая сфера — можно продать
        self.assertIn('kind="cores"', src)
        self.assertIn('by_admin="coop"', src)


class GameMirror(unittest.TestCase):
    def test_constants(self):
        m = re.search(r"const COOP = \{MIN:(\d+), SIZE:(\d+), TRIES:(\d+), SLOTS:(\d+), PER:(\d+), PACK:(\d+), STAGES:\[([\d, ]+)\], BOSS_LV:(\d+), BOSS_HP:(\d+),\s*"
                      r"PRIZE:\{item:\"(\w+)\", n:(\d+), cores:(\d+)\}, TIME:(\d+)", GAME)
        self.assertIsNotNone(m)
        mn, size, tries, slots, per, pack = map(int, m.groups()[:6])
        self.assertEqual((mn, size, tries, slots, per), (coop.MIN_LVL, coop.SIZE, coop.TRIES, coop.SLOTS, coop.PER))
        self.assertEqual(per % pack, 0)
        self.assertEqual(tuple(int(x) for x in m.group(7).split(",")), coop.STAGES)
        self.assertEqual((int(m.group(8)), int(m.group(9))), (coop.BOSS_LVL, mobguard.COOP_BOSS_HP))
        self.assertEqual((m.group(10), int(m.group(11)), int(m.group(12))), (coop.PRIZE["item"], coop.PRIZE["n"], coop.PRIZE["cores"]))
        self.assertEqual(int(m.group(13)) * 60, coop.TIME_LIMIT)
        base = re.search(r"const COOP_BASE = \[([^\]]+)\];", GAME).group(1)
        self.assertEqual(tuple(re.findall(r'"(\w+)"', base)), ledger.COOP_BASE)

    def test_mob_formulas(self):
        self.assertIn("const coopHp = L => Math.round((20 + 14*L + 0.25*L*L)*0.5);", GAME)
        self.assertIn("hp:m.hp*COOP.BOSS_HP", GAME)
        self.assertIn("scrap:[m.scrap[0]*10, m.scrap[1]*10]", GAME)
        self.assertIn("scrap:[L + 1, 2*L + 4]", GAME)
        self.assertIn('NPCS["cw" + L]', GAME)
        self.assertIn('NPCS["cb" + COOP.BOSS_LV]', GAME)

    def test_layout_order_matches_server(self):
        blk = GAME[GAME.index("{ const _bdCp = buildDungeon;"):GAME.index("// ---- состояние ----", GAME.index("{ const _bdCp = buildDungeon;"))]
        a, b = blk.index("COOP.STAGES.forEach((L, s) =>"), blk.index('bt = "cb" + COOP.BOSS_LV')
        self.assertLess(a, b)                                                            # сначала 8×40 монстров, босс — последним
        self.assertIn("enemies.forEach((e, i) => e.idx = i);", blk)
        self.assertIn("coopPacks(rm, s)", blk)
        self.assertIn('/^coop[1-9]$/', GAME)

    def test_event_is_live(self):
        line = re.search(r'\{id:"coop"[^\n]+', GAME).group(0)
        self.assertNotIn("soon", line)
        self.assertIn('when:"Доступно всегда"', line)
        self.assertIn('else if(d.ev === "coop" && typeof openCoop === "function") openCoop();', GAME)
        self.assertIn('ev.id === "coop" ? coopPill()', GAME)

    def test_rules_in_window(self):
        for text in ("Создайте группу или войдите в чужую — начать забег может только лидер группы",
                     "Только с ${COOP.MIN} уровня", "Одна попытка в день, тратится при входе",
                     "${COOP.STAGES.length} этапов по ${COOP.PER} монстров, уровень растёт: ${COOP.STAGES.join(\", \")}",
                     "Монстры общие на двоих: следующий этап открывается, только когда вы вдвоём зачистите текущий",
                     "В конце — общий босс ${COOP.BOSS_LV} уровня: 1 титановая сфера (безопасная заточка) и ${pz.cores} энергоядер одному случайному пилоту",
                     "Смерть любого из двоих завершает забег для обоих",
                     "Если вход сорвался или связь оборвалась до первого этапа — попытка вернётся",
                     "Дождитесь напарника"):
            self.assertIn(text, GAME)

    def test_coop_grant_waits_for_space(self):
        blk = GAME[GAME.index("function applyGrant(g){"):GAME.index("// ---- вход ----", GAME.index("function applyGrant(g){"))]
        a = blk.index('if(g.by === "coop" && g.kind === "item"')
        self.assertLess(a, blk.index("S.grantsDone.push(gid)"))                          # не подтверждаем, пока нет места
        self.assertIn("награда придёт при следующем входе", blk)


COOP_SCREENS = [
    "Сотрудничество", "Совместное прохождение вдвоём", "Начать забег", "Ждём напарника · 1/2", "нет попытки",
    "Группа в сборе — начинайте забег", "Группа в сборе — забег начинает лидер", "В пати 3 пилота — нужно ровно 2",
    "Не все готовы: нужен 10 уровень, попытка и быть в сети",
    "Создайте группу или войдите в чужую — начать забег может только лидер группы", "Одна попытка в день, тратится при входе",
    "8 этапов по 40 монстров, уровень растёт: 10, 15, 20, 25, 30, 40, 50, 60",
    "Монстры общие на двоих: следующий этап открывается, только когда вы вдвоём зачистите текущий",
    "В конце — общий босс 40 уровня: 1 титановая сфера (безопасная заточка) и 100 энергоядер одному случайному пилоту",
    "Смерть любого из двоих завершает забег для обоих", "Если вход сорвался или связь оборвалась до первого этапа — попытка вернётся",
    "На забег — 40 минут", "Этапы", "Этап 3", "40 монстров · ур. 10", "Босс · ур. 40", "Награда за босса",
    "Достаётся одному из двоих по жребию — в итогах забега видно, кому и что выдано",
    "Опыт и добыча с каждого монстра — тому из двоих, кто нанёс ему больше урона. Монстр сильнее тебя на 12+ уровней опыта и добычи не даёт.",
    "Сдвоенный Колосс", "Патрульный Краулер", "Сторожевой Дрон", "Заградительный Страж", "Штурмовой Шагоход", "Таранный Краулер",
    "Осадный Дрон", "Карательный Страж", "Опустошающий Шагоход", "Сбор группы", "Старт забега", "Босс · Сдвоенный Колосс",
    "Этап 3 пройден", "Этап 3/8", "Возврат через 12 с", "Дождитесь напарника", "Этап 4 откроется через", "Барьер к боссу откроется через",
    "⚔ Этап 3 из 8 · монстры 20 уровня", "☠ Барьер пал: Сдвоенный Колосс ждёт в последнем зале!",
    "👑 Сдвоенный Колосс повержен! Награда по жребию: Nova — это ты!", "Итоги забега", "Забег окончен", "Пройдено этапов: ",
    "🎲 Награда по жребию", "Это ты!", "Попытка возвращена — можно попробовать ещё раз сегодня.", "Выход из зала",
    "Забег ещё не начался: если уйти сейчас, попытка вернётся обоим.", "Забег закончится для обоих, а попытка не вернётся.", "Покинуть забег",
    "Барьер откроется, когда вы вдвоём зачистите этап 3", "Барьер к боссу откроется после этапа 8", "Барьер откроется, когда начнётся забег",
    "сильнее тебя на 12+ ур.: без награды", "забег окончен для обоих · возврат в ангар…", "Сейсмический удар!", "Уровень 40 · ярость",
    "В зал «Сотрудничества» группу заводит лидер: События → Сотрудничество", "👥 Лидер ведёт группу в зал «Сотрудничества»",
    "Сотрудничество: награда за босса +100 ядер", "Сотрудничество: награда за босса — Титановая сфера заточки",
    "Сотрудничество: Титановая сфера заточки не поместилась — освободи место в сумке или на складе, награда придёт при следующем входе",
    # сообщения сервера (coop.py, webserver.handle_coop)
    "Ты уже в забеге", "«Сотрудничество» открывается с 10 уровня", "Попытка на сегодня уже использована", "Сначала дождись возрождения",
    "Сначала выйди из группы элитной зоны", "В «Сотрудничество» идут вдвоём: в твоей пати больше 2 пилотов", "В группе уже двое",
    "Начать забег может только лидер группы", "Все залы «Сотрудничества» заняты — попробуйте через минуту",
    "В зал «Сотрудничества» группу заводит лидер", "Сначала выйди из группы «Сотрудничества»", "Нужна группа из 2 пилотов",
    "Nova: попытка на сегодня уже использована", "Nova сейчас в другом событии", "Nova уже в забеге", "Nova: нужен 10 уровень",
    "Nova погиб — забег окончен", "Nova вышел — забег окончен", "Время забега вышло", "Напарник не пришёл — попытка возвращена",
    "Забег окончен — возврат в ангар", "Босс повержен", "Вернуться в забег", "👥 Возвращение в забег «Сотрудничества»",
    "Nova · нет в зале · 18 с",
]


class Translated(unittest.TestCase):
    def test_every_language(self):
        import test_i18n_content as ti
        res = ti.run_node(COOP_SCREENS)
        for lang in ti.LANGS:
            russian = re.compile("[ыэъёЫЭЪЁ]" if lang == "uk" else "[А-Яа-яЁё]")
            with self.subTest(lang=lang):
                self.assertEqual([f"{s!r} → {t!r}" for s, t in zip(COOP_SCREENS, res[lang]) if russian.search(t)], [])
        self.assertEqual(res["bad"], [])
        en = dict(zip(COOP_SCREENS, res["en"]))
        self.assertEqual(en["Этап 3/8"], "Stage 3/8")
        self.assertEqual(en["⚔ Этап 3 из 8 · монстры 20 уровня"], "⚔ Stage 3 of 8 · level 20 monsters")

    def test_dictionary_is_loaded_before_index(self):
        # словарь, добавленный внутри buildTrRe (после сборки индекса), находил только целые строки:
        # «Этап 3/8», «40 монстров · ур. 10» и фразы с ником оставались по-русски
        a = GAME.index("function buildTrRe(){")
        body = GAME[a:GAME.index("\n}\n", a)]
        self.assertNotIn("Object.assign(I18N", body)
        self.assertLess(GAME.index('Object.assign(I18N, {"Сдвоенный Колосс"'), a)

    def test_server_texts_are_listed(self):
        # каждая фраза из coop.REASON_TEXT и отказы handle_coop проверены выше
        for t in coop.REASON_TEXT.values():
            probe = t.format(who="Nova")
            self.assertTrue(probe in COOP_SCREENS or probe.replace("Nova", "Напарник") in COOP_SCREENS or t == "Босс повержен", probe)


if __name__ == "__main__":
    unittest.main()
