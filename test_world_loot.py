"""Мир, шаг 3: чертежи классов и Монета удачи (патч LIBERTY 17 октября «Мир»).

* Чертежи классов (как свитки классов в LIBERTY): падают только в Мире, с монстров спотов, у каждого спота свои,
  шанс каждого — 0,003% с монстра; поднять может робот любого класса. По чертежу Кузнец собирает любую вещь
  уникального сета его класса без осколков — нужны только лом и ядра.
* Спот сервер узнаёт по номеру общего моба из боя, который засчитал сам (mobworld → mobguard.fight_idx):
  номера идут спот за спотом, как в раскладке игры (mobguard.WORLD_SPOTS). Повтор заявки и бой «своими силами»
  чертежей не дают.
* Монета удачи: с любого монстра Мира — 0,003% (с Железного Наместника ×150), сдаётся в Кодекс самоцветов рядом
  с камнями до 200 штук: 30 → +40 атаки, 80 → ещё +70, 200 → ещё +110 (всего +220).

Запуск: python -m unittest test_world_loot
"""
import asyncio
import json
import re
import shutil
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for name in ("aiohttp", "sqlalchemy", "db", "models", "metrics", "db_atomic"):
    try:
        __import__(name)
    except Exception:
        sys.modules[name] = mock.MagicMock()
if not hasattr(sys.modules["metrics"], "inc"):
    sys.modules["metrics"] = types.SimpleNamespace(inc=lambda *a, **k: None, gauge=lambda *a, **k: None)

import items        # noqa: E402
import mobguard     # noqa: E402
import mobworld     # noqa: E402
import worldthrone  # noqa: E402

GAME = (ROOT / "game.html").read_text(encoding="utf-8")
ITEMS_SRC = (ROOT / "items.py").read_text(encoding="utf-8")
PATCHES = (ROOT / "patches.html").read_text(encoding="utf-8")
GUIDE = (ROOT / "guide.html").read_text(encoding="utf-8")
LOC = mobguard.WORLD_LOC
ALWAYS = types.SimpleNamespace(random=lambda: 0.0, choice=lambda seq: seq[0], randint=lambda a, b: a)
NEVER = types.SimpleNamespace(random=lambda: 0.999999, choice=lambda seq: seq[0], randint=lambda a, b: a)


class Hub:
    def __init__(self):
        self.loc, self.all, self.by_loc = [], [], {}

    def to_loc(self, loc, p, skip_uid=None):
        self.loc.append(p)

    def to_all(self, p):
        self.all.append(p)


def kill_shared(hub, uid, idx, typ):
    """Добить общего моба Мира номер idx одним пакетом ударов — как телефон; вернуть сообщение mdie."""
    mobworld._budget.pop(uid, None)
    mobworld.on_hits({"id": uid, "loc": LOC, "lvl_cap": 59}, {"loc": LOC, "h": [[idx, typ, mobguard.mob_hp(typ)]]}, hub)
    return [m for m in hub.loc if m["t"] == "mdie" and m["i"] == idx][-1]


def reset_mobs():
    for d in (mobworld._mobs, mobworld._dirty, mobworld._own, mobworld._moved, mobworld._budget, mobguard._fights):
        d.clear()


class Blueprints(unittest.TestCase):
    def test_ids_registered_and_fit_balance_column(self):
        self.assertEqual(items.BLUEPRINTS, tuple("bp_" + c for c in items.CLASS_SHARDS))   # порядок — как у классов
        for bid in items.BLUEPRINTS:
            self.assertIn(bid, items.REG_IDS)                    # учёт штуками: маркет и Кузнец
            self.assertLessEqual(len(bid), 12)

    def test_spot_table_like_liberty(self):
        # у каждого спота свои чертежи: лоу и мид — по одному, топ — по два-три; у каждого класса — два спота
        self.assertEqual(set(items.WORLD_BP), {s for s, *_ in mobguard.WORLD_SPOTS})
        for sid, n, lo, hi in mobguard.WORLD_SPOTS:
            if hi <= 52:
                self.assertEqual(len(items.WORLD_BP[sid]), 1, sid)
        self.assertEqual([len(items.WORLD_BP[s]) for s in ("eye", "cliff", "gate")], [3, 2, 2])
        for cls in items.CLASS_SHARDS:
            where = [s for s, c in items.WORLD_BP.items() if cls in c]
            self.assertEqual(len(where), 2, cls)
            self.assertTrue(any(s in ("eye", "cliff", "gate") for s in where), cls)       # второй — на топ-споте
        self.assertEqual(items.BP_PCT, 0.003)

    def test_world_spot_by_index(self):
        self.assertEqual(worldthrone.IDX, 168)
        self.assertEqual(sum(n for _, n, _, _ in mobguard.WORLD_SPOTS), worldthrone.IDX)
        self.assertEqual(items.world_spot(0), ("tract", 40, 42))
        self.assertEqual(items.world_spot(14), ("tract", 40, 42))
        self.assertEqual(items.world_spot(15), ("glade", 40, 42))
        self.assertEqual(items.world_spot(108), ("eye", 53, 56))
        self.assertEqual(items.world_spot(167), ("gate", 57, 59))
        self.assertIsNone(items.world_spot(worldthrone.IDX))                      # босс трона — не на споте
        self.assertIsNone(items.world_spot(-1))

    def test_roll_world(self):
        with mock.patch.object(items, "random", ALWAYS):
            self.assertEqual([d["id"] for d in items.roll_world(108, 53)], ["bp_guard", "bp_reaper", "bp_sniper", items.LUCK])
            self.assertEqual([d["id"] for d in items.roll_world(0, 40)], ["bp_guard", items.LUCK])
            self.assertEqual([d["id"] for d in items.roll_world(108, 40)], [items.LUCK])      # «слабый» моб под номером топ-спота
            self.assertEqual([d["id"] for d in items.roll_world(worldthrone.IDX, 60)], [items.LUCK])
            self.assertTrue(all(d["kind"] == "sph" and d["n"] == 1 for d in items.roll_world(128, 54)))
        with mock.patch.object(items, "random", NEVER):
            self.assertEqual(items.roll_world(108, 53), [])

    def test_chance_per_blueprint(self):
        import random
        random.seed(11)
        mult, n = 0.25 / items.P(items.BP_PCT), 20000                 # шанс каждого — 25%
        got = {}
        for _ in range(n):
            for d in items.roll_world(130, 54, mult):                # Чёрный утёс: Техномант и Ремонтник
                got[d["id"]] = got.get(d["id"], 0) + 1
        self.assertEqual(set(got), {"bp_techno", "bp_medic", items.LUCK})
        for k in got:
            self.assertAlmostEqual(got[k] / n, 0.25, delta=0.015)
        self.assertLess(sum(len(items.roll_world(0, 40)) for _ in range(20000)), 8)   # без множителей — ~1 на 16 000
        with mock.patch.object(items, "random", types.SimpleNamespace(random=lambda: 0.95)):
            self.assertEqual(items.roll_world(0, 40, 10**9), [])     # потолок 90%, как у сфер

    def test_uniq_by_blueprint(self):
        self.assertEqual(items.uniq_need("u_guard_armor", True), {"scrap": 120000, "cores": 500, "bp_guard": 1})
        self.assertEqual(items.uniq_need("u_medic_head", True), {"scrap": 60000, "cores": 200, "bp_medic": 1})
        self.assertEqual(items.uniq_need("u_guard_armor"), items.UNIQ["u_guard_armor"][1])           # из осколков — как было
        for uid in items.UNIQ:
            need = items.uniq_need(uid, True)
            self.assertEqual([k for k in need if k not in ("scrap", "cores")], ["bp_" + uid.split("_")[1]])
        S = {"scrap": 10**6, "cores": 10**4, "inv": [{"id": "bp_guard", "n": 1}], "store": [{"id": "bp_guard", "n": 1}]}
        self.assertIsNone(items.uniq_check(S, "u_guard_armor", 1, True))       # ограничения по уровню нет
        self.assertIsNone(items.uniq_check(S, "u_guard_armor", 40, True))
        self.assertIsNotNone(items.uniq_check(S, "u_guard_armor", 40))              # осколков нет
        self.assertIsNotNone(items.uniq_check(S, "u_reaper_armor", 40, True))       # чертёж чужого класса не подходит
        items.uniq_spend(S, "u_guard_armor", True)
        self.assertEqual((S["scrap"], S["cores"], S["inv"], S["store"]), (10**6 - 120000, 10**4 - 500, [], [{"id": "bp_guard", "n": 1}]))
        items.uniq_spend(S, "u_guard_head", True)
        self.assertEqual(S["store"], [])
        self.assertIsNotNone(items.uniq_check(S, "u_guard_legs", 40, True))          # чертежей больше нет

    def test_craft_takes_registered_blueprint(self):
        src = ITEMS_SRC[ITEMS_SRC.index("async def craft_uniq("):ITEMS_SRC.index("# ---------- для маркета ----------")]
        self.assertIn("for sh, n in uniq_need(item_id, bp).items():", src)
        self.assertIn("if sh in SHARDS or sh in BLUEPRINTS:", src)                  # чертёж — с серверного учёта, атомарно
        self.assertIn("ok, bal = await take_spheres(s, uid, sh, n)", src)
        self.assertIn("uniq_spend(S, item_id, bp)", src)
        self.assertIn('return await craft_uniq(s, uid, user, item_id, bool(body.get("bp")))', ITEMS_SRC)
        self.assertIn('const r = await regApi("craft", bp ? {id, bp:1} : {id});', GAME)

    def test_market(self):
        self.assertIn('"чертежей" if item["id"] in BLUEPRINTS', ITEMS_SRC)
        self.assertIn('"монет удачи" if item["id"] == LUCK', ITEMS_SRC)
        self.assertIn('["bp", "Чертежи"]', GAME)
        self.assertIn('...SHARD_IDS, ...GEM_IDS, ...BP_IDS]', GAME)


class LuckyCoin(unittest.TestCase):
    def test_codex_member(self):
        self.assertEqual(items.LUCK, "gm_luck")
        self.assertIn(items.LUCK, items.GEMS)
        self.assertIn(items.LUCK, items.REG_IDS)
        self.assertNotIn(items.LUCK, items.GEMS_COMMON + items.GEMS_RARE)
        self.assertEqual((items.gem_kind(items.LUCK), items.gem_cap(items.LUCK), items.gem_cap("gm_emer")), ("l", 200, 500))
        self.assertEqual(items.GEM_STEPS["l"], [(30, {"atk": 40}), (80, {"atk": 70}), (200, {"atk": 110})])
        self.assertLessEqual(len(items.GEM_CX + items.LUCK), 12)

    def test_thresholds_stack(self):
        b = lambda n: items.gem_bonus({items.LUCK: n})["atk"]
        self.assertEqual([b(0), b(29), b(30), b(79), b(80), b(199), b(200)], [0, 0, 40, 40, 110, 110, 220])
        self.assertEqual(items.gem_bonus({items.LUCK: 200}), {"atk": 220, "def": 0, "hp": 0, "rate": 0.0})
        self.assertGreater(items.gem_bm({items.LUCK: 200}), items.gem_bm({}))         # предел БМ в рейтинге растёт

    def test_give_up_to_200(self):
        f = items.gem_give_amount
        self.assertEqual(f(999, 190, 50, items.LUCK_MAX), 10)
        self.assertEqual(f(5, 200, 50, items.LUCK_MAX), 0)
        self.assertEqual(f(999, 190, 50), 50)                                        # камни — по-прежнему до 500
        op = ITEMS_SRC[ITEMS_SRC.index('if op == "gem":'):ITEMS_SRC.index('if op == "craft":')]
        self.assertIn("k = gem_give_amount(body.get(\"n\", 1), cx.n, bal.n, cap)", op)
        self.assertIn("cx = {g: min(gem_cap(g), got.get(g, 0)) for g in GEMS}", ITEMS_SRC)           # вход в мир
        self.assertIn("gemcx = {k: min(gem_cap(k), sph.get(GEM_CX + k, 0) or 0) for k in GEMS}", ITEMS_SRC)

    def test_only_from_the_world(self):
        with mock.patch.object(items, "random", ALWAYS):
            self.assertNotIn(items.LUCK, [d["id"] for d in items.roll_gems(59, 10**9)])   # с камнями не падает
        self.assertEqual(items.LUCK_PCT, 0.003)
        import random
        random.seed(5)
        mult = 0.3 / items.P(items.LUCK_PCT)
        hits = sum(1 for _ in range(20000) if any(d["id"] == items.LUCK for d in items.roll_world(168, 60, mult)))
        self.assertAlmostEqual(hits / 20000, 0.3, delta=0.015)
        self.assertEqual(items.BOSS_LOOT, 150)                         # с Наместника — ×150, как весь ценный дроп главаря


class SpotFromServerFight(unittest.TestCase):
    def setUp(self):
        reset_mobs()
        worldthrone.st.update(until=0.0, told=True)
        self.hub = Hub()

    def tearDown(self):
        reset_mobs()
        worldthrone.st.update(until=0.0, told=True)

    def test_index_comes_from_the_server(self):
        die = kill_shared(self.hub, 5, 108, "wm53")
        self.assertIsNone(mobguard.fight_idx(5, die["iid"], "wm54"))         # другой тип — не этот бой
        self.assertIsNone(mobguard.fight_idx(6, die["iid"], "wm53"))         # чужой бой
        self.assertIsNone(mobguard.fight_idx(5, "x", "wm53"))
        self.assertEqual(mobguard.fight_idx(5, str(die["iid"]), "wm53"), 108)
        self.assertIsNone(mobguard.fight_idx(5, die["iid"], "wm53"))         # номер выдаётся один раз
        self.assertIsNone(mobguard.check_kill(5, die["iid"], "wm53"))        # а сама заявка проходит, бой забывается
        self.assertIsNone(mobguard.fight_idx(5, die["iid"], "wm53"))

    def test_guard_off_still_one_roll_per_kill(self):
        die = kill_shared(self.hub, 5, 3, "wm40")
        with mock.patch.object(mobguard, "MODE", "off"):                    # проверка выключена — бой не забывается
            self.assertTrue(mobguard.allow_kill(5, die["iid"], "wm40"))
            self.assertEqual(mobguard.fight_idx(5, die["iid"], "wm40"), 3)
            self.assertTrue(mobguard.allow_kill(5, die["iid"], "wm40"))
            self.assertIsNone(mobguard.fight_idx(5, die["iid"], "wm40"))     # но повтор заявки чертежа уже не даст

    def test_phone_hits_carry_no_index(self):
        mobguard.on_hits(7, 50, [[123, "wm53", 999]])
        self.assertIsNotNone(mobguard._fights[7].get(123))
        self.assertIsNone(mobguard.fight_idx(7, 123, "wm53"))

    def test_boss_sharers_get_the_index(self):
        mobworld._budget.clear()
        for uid, share in ((1, 0.6), (2, 0.4)):
            left = round(worldthrone.HP * share)
            while left > 0:
                d = min(left, 40_000)
                mobworld._budget.pop(uid, None)
                mobworld.on_hits({"id": uid, "loc": LOC, "lvl_cap": 50}, {"loc": LOC, "h": [[worldthrone.IDX, worldthrone.TYPE, d]]}, self.hub)
                left -= d
        die = [m for m in self.hub.loc if m["t"] == "mdie"][0]
        for uid in (1, 2):
            self.assertEqual(mobguard.fight_idx(uid, die["iid"], worldthrone.TYPE), worldthrone.IDX)


class ProcessKills(unittest.TestCase):
    """Заявка на убийство в Мире — через настоящий items.process_kills (база и соседние модули — заглушки)."""

    def setUp(self):
        reset_mobs()
        items._bucket.clear()
        self.hub = Hub()

    def tearDown(self):
        reset_mobs()

    def run_kills(self, kills, rnd=ALWAYS):
        async def a_none(*a, **k):
            return None

        async def a_val(v):
            return v

        got = []

        async def add_spheres(s, owner, sid, n):
            got.append(sid)
            return {"id": sid, "n": n, "reg": True}

        async def mint_gear(s, owner, item_id, g, e=0, source="drop"):
            got.append(item_id)
            return {"id": item_id, "g": g, "e": e, "uid": "u1"}

        async def count_loot_kill(s, uid, weight):
            return 0

        async def dropup_mult(s, uid, cap):
            return 1.0

        fake = {
            "progress": types.SimpleNamespace(cap_of=lambda s, u: a_val(50), add_kill=lambda *a, **k: a_val(50), mob_exp=lambda lv, boss=False: 1),
            "pvp": types.SimpleNamespace(mob_killed=a_none),
            "funnel": types.SimpleNamespace(mark=lambda *a: None),
            "vip": types.SimpleNamespace(level=lambda *a: a_val(0), LEVELS=[], drop_mult=lambda lv: 1),
            "seasonpts": types.SimpleNamespace(kill_counter=lambda loc: None, add=a_none, has_ticket=lambda uid: a_val(False)),
            "ledger": types.SimpleNamespace(add=lambda *a: None, kill_income=lambda *a, **k: {}),
        }
        session = types.SimpleNamespace(commit=a_none)
        with mock.patch.dict(sys.modules, fake), mock.patch.object(items, "random", rnd), \
                mock.patch.object(items, "add_spheres", add_spheres), mock.patch.object(items, "mint_gear", mint_gear), \
                mock.patch.object(items, "count_loot_kill", count_loot_kill), mock.patch.object(items, "dropup_mult", dropup_mult), \
                mock.patch.object(items, "roll_soft", lambda *a, **k: []), mock.patch.object(items, "roll_pots", lambda *a, **k: []):
            asyncio.run(items.process_kills(session, 5, {"loc": LOC, "fac": None}, kills))
        return got

    def test_blueprints_and_coin_for_a_server_kill(self):
        die = kill_shared(self.hub, 5, 108, "wm53")                          # Око бездны
        got = self.run_kills([{"mob": "wm53", "loc": LOC, "iid": die["iid"]}])
        self.assertTrue({"bp_guard", "bp_reaper", "bp_sniper", items.LUCK} <= set(got))
        self.assertNotIn("bp_techno", got)
        again = self.run_kills([{"mob": "wm53", "loc": LOC, "iid": die["iid"]}])   # повтор той же заявки
        self.assertFalse(any(x in items.BLUEPRINTS or x == items.LUCK for x in again))

    def test_no_world_loot_without_server_fight(self):
        mobguard.on_hits(5, 50, [[777, "wm40", 5000]])                       # удары телефона — бой без номера
        got = self.run_kills([{"mob": "wm40", "loc": LOC, "iid": 777}])
        self.assertIn("sph_cu", got)                                          # обычный ценный дроп — как раньше
        self.assertFalse(any(x in items.BLUEPRINTS or x == items.LUCK for x in got))

    def test_spot_decides_the_blueprint(self):
        die = kill_shared(self.hub, 5, 3, "wm40")                             # Заросшая трасса — Страж
        got = self.run_kills([{"mob": "wm40", "loc": LOC, "iid": die["iid"]}])
        self.assertEqual([x for x in got if x in items.BLUEPRINTS], ["bp_guard"])
        die = kill_shared(self.hub, 5, 20, "wm40")                            # Тихая поляна — Жнец
        got = self.run_kills([{"mob": "wm40", "loc": LOC, "iid": die["iid"]}])
        self.assertEqual([x for x in got if x in items.BLUEPRINTS], ["bp_reaper"])

    def test_wiring_order(self):
        a = ITEMS_SRC.index('widx = mobguard.fight_idx(uid, k.get("iid"), mob) if loc == WORLD_LOC else None')
        b = ITEMS_SRC.index('if not mobguard.allow_kill(uid, k.get("iid"), mob):')
        self.assertLess(a, b)                                                  # номер — до проверки: она бой забывает
        self.assertIn("for d in roll_world(widx, lv, base_mult):", ITEMS_SRC)
        self.assertIn("fights[iid] = [m[\"type\"], m[\"max\"], now, now, i]", (ROOT / "mobworld.py").read_text(encoding="utf-8"))


@unittest.skipUnless(shutil.which("node"), "нет node")
class InGame(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        head = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8")
        head = head[:head.index("\n", head.index("const run = "))]
        check = r'''
const out = run(`(() => {
  const r = {}, toasts = [], chat = [];
  toast = s => toasts.push(s); chatAnnounce = s => chat.push(s);
  S.level = 50; S.cls = "guard"; S.name = "Nova"; S.skl = {}; S.pas = {}; S.up = {}; S.eq = {}; S.inv = []; S.store = []; paused = false; auto = false; now = 100; vw = 390; vh = 844;
  const put = (x, y) => { p = {x, y, r:20, dead:false, aim:0, ang:0, atkT:0, stunT:0, target:null, vx:0, vy:0}; };
  buildLoc("world"); put(WORLD.spawn.x, WORLD.spawn.y);
  r.layout = enemies.map(e => [e.throne ? "throne" : e.spot.id, NPCS[e.type].lvl, e.idx]);
  r.spots = W_SPOTS.map(s => ({id:s.id, bp:s.bp}));
  r.bpIds = BP_IDS;
  r.items = BP_IDS.map(id => ({id, name:ITEMS[id].name, type:ITEMS[id].type, nosell:!!ITEMS[id].nosell, cls:ITEMS[id].bp, desc:ITEMS[id].desc,
    mk:mkCatOf(id), st:stCatOf(id), reg:REG_IDS.includes(id), svg:icon(id).startsWith("<svg"), big:bpIcon(id, true).includes("<animate")}));
  r.bpNeed = Object.fromEntries(UNIQ_IDS.map(id => [id, BP_NEED(id)]));
  // Кузнец: по чертежу — без осколков; чертёж чужого класса не подходит
  S.scrap = 1000000; S.cores = 10000; S.inv = [{id:"bp_guard", n:1}]; S.store = [{id:"bp_guard", n:1}, {id:"sh_magma", n:500}];
  r.can = {own:uniqCan("u_guard_armor", true), shards:uniqCan("u_guard_armor"), other:uniqCan("u_reaper_armor", true)};
  SM.sel = "u:u_guard_armor"; const h = smDetail();
  r.detail = {block:h.includes("Или по чертежу"), btn:h.includes('data-ucraft="u_guard_armor" data-bp="1" >Собрать по чертежу'), shardBtn:h.includes('data-ucraft="u_guard_armor" disabled>Не хватает ресурсов')};
  r.tileCan = / can"/.test(smTile("u_guard_armor", "u"));
  r.shardsTab = (() => { const t = smShards(); return {head:t.includes("Чертежи классов"), cards:(t.match(/bp-card/g) || []).length, where:t.includes("Мир: Заросшая трасса, Око бездны")}; })();
  const gear = id => S.inv.filter(x => x.id === id).length;          // count() считает только стопки, вещи — штуками
  craftUniq("u_guard_armor", null, true);
  r.crafted = {item:gear("u_guard_armor"), bp:count(S.inv, "bp_guard") + count(S.store, "bp_guard"), scrap:S.scrap, cores:S.cores, magma:count(S.store, "sh_magma")};
  craftUniq("u_guard_head", null, true); r.second = gear("u_guard_head");
  toasts.length = 0; craftUniq("u_guard_legs", null, true); r.third = {item:gear("u_guard_legs"), toast:toasts.slice()};
  SM.sel = "u:u_guard_legs"; const h2 = smDetail(); r.noBp = {btn:h2.includes(">Нет чертежа</button>"), note:h2.includes("Чертёж Стража падает в Мире: Заросшая трасса, Око бездны.")};
  // Кодекс: Монета удачи — пятая карточка, до 200, пороги складываются
  S.gemCx = {}; S.inv = [{id:"gm_luck", n:250}]; S.store = [];
  gemGive("gm_luck", true); r.coin = {cx:gemCx("gm_luck"), left:count(S.inv, "gm_luck")};
  const lb = n => { S.gemCx = {gm_luck:n}; return gemBonus().atk; };
  r.luckAtk = [lb(29), lb(30), lb(80), lb(999)]; r.luckCap = gemCx("gm_luck");
  S.eq = {}; S.inv = []; S.max_hp = 540; S.attack = 104; S.defense = 51; S.gemCx = {}; const s0 = stat(); S.gemCx = {gm_luck:200}; const s1 = stat(); r.statAtk = s1.atk - s0.atk;
  const cx = gemCodexHtml(); r.codex = {cards:(cx.match(/class="gmcard/g) || []).length, luckMax:cx.includes('class="gmcard max luck"'), of200:cx.includes("из 200"),
    hint:cx.includes("Монета удачи падает только в Мире"), last:cx.indexOf("<b>Монета удачи</b>") > cx.indexOf("<b>Нефритовый резонатор</b>")};
  // карта: монета и чертежи — только в Мире; у монстра — чертежи всех спотов, где он стоит
  const D53 = mobDrops("wm53", "world"), DB = mobDrops("wt60", "world"), DS = mobDrops("dg15", "sector1");
  r.map = {coin53:D53.gems.find(g => g.id === "gm_luck").chance, bps53:D53.bps.map(b => b.id + "@" + b.spot), bp49:mobDrops("wm49", "world").bps.map(b => b.id),
    boss:DB.gems.find(g => g.id === "gm_luck").chance, bossBps:DB.bps.length, sector:DS.gems.some(g => g.id === "gm_luck") || !!DS.bps};
  locKey = "world"; mapOpenType = "wm53"; const mc = mobCard("wm53"); r.mobCard = mc.includes("Чертежи классов") && mc.includes("Чертёж Стража") && mc.includes("Монета удачи");
  r.spotCards = W_SPOTS.map(s => (wSpotBp(s).match(/Чертёж [А-Яа-яё]+/g) || []));
  r.mineGold = wSpotBp(W_SPOTS[0]).includes('<i class="me">');
  // находка: надпись, уведомление и общий чат
  toasts.length = 0; put(100, 100);
  pickNote({kind:"bp_ghost", amt:1}); pickNote({kind:"gm_luck", amt:1}); pickNote({kind:"gm_emer", amt:1});
  r.pick = {toasts:toasts.slice(), chat:chat.slice()};
  // на земле: чертёж и монета рисуются, остальное — как раньше
  drops = [{x:10, y:10, kind:"bp_medic", amt:1, t:0.3}, {x:20, y:20, kind:"gm_luck", amt:1, t:0.7}, {x:30, y:30, kind:"gm_ruby", amt:1, t:0.2}, {x:40, y:40, kind:"scrap", amt:5, t:0}];
  let err = null; try{ drawDrops(); }catch(e){ err = String(e); } r.draw = {err, left:drops.length};
  return JSON.stringify(r);
})()`);
process.stdout.write(out + "\n");
'''
        res = subprocess.run(["node", "-", str(ROOT / "game.html")], input=head + check, capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            raise AssertionError(res.stderr[-3000:])
        cls.r = json.loads(res.stdout.strip().splitlines()[-1])

    # ---- сервер знает раскладку Мира так же, как игра ----
    def test_layout_mirror(self):
        lay = self.r["layout"]
        self.assertTrue(all(i == x[2] for i, x in enumerate(lay)))           # номер общего моба = место в раскладке
        self.assertEqual(lay[-1][0], "throne")
        self.assertEqual(len(lay) - 1, worldthrone.IDX)
        blocks = []
        for sid, lv, _ in lay[:-1]:
            if blocks and blocks[-1][0] == sid:
                b = blocks[-1]; b[1] += 1; b[2] = min(b[2], lv); b[3] = max(b[3], lv)
            else:
                blocks.append([sid, 1, lv, lv])
        self.assertEqual([tuple(b) for b in blocks], list(mobguard.WORLD_SPOTS))   # спот за спотом, все уровни спота
        for i, (sid, lv, _) in enumerate(lay[:-1]):
            spot = items.world_spot(i)
            self.assertEqual(spot[0], sid)
            self.assertTrue(spot[1] <= lv <= spot[2])

    def test_spot_blueprints_mirror(self):
        self.assertEqual({s["id"]: tuple(s["bp"]) for s in self.r["spots"]}, items.WORLD_BP)
        self.assertEqual(self.r["bpIds"], list(items.BLUEPRINTS))
        self.assertEqual(self.r["bpNeed"], {uid: items.uniq_need(uid, True) for uid in items.UNIQ})

    def test_items(self):
        gen = dict(re.findall(r'(\w+):"([^"]+)"', GAME[GAME.index("const CX_CLS_GEN = {"):GAME.index("};", GAME.index("const CX_CLS_GEN = {"))]))
        for it in self.r["items"]:
            with self.subTest(it=it["id"]):
                self.assertEqual(it["name"], "Чертёж " + gen[it["cls"]])
                self.assertEqual((it["type"], it["nosell"], it["mk"], it["st"], it["reg"], it["svg"], it["big"]), ("bp", True, "bp", "res", True, True, True))
                self.assertIn("без осколков · падает в Мире · продаётся на маркете за GRAM", it["desc"])

    # ---- Кузнец ----
    def test_smith(self):
        r = self.r
        self.assertEqual(r["can"], {"own": True, "shards": False, "other": False})
        self.assertEqual(r["detail"], {"block": True, "btn": True, "shardBtn": True})
        self.assertTrue(r["tileCan"])                                        # плитка горит: можно собрать по чертежу
        self.assertEqual(r["shardsTab"], {"head": True, "cards": 7, "where": True})
        self.assertEqual(r["crafted"], {"item": 1, "bp": 1, "scrap": 1000000 - 120000, "cores": 10000 - 500, "magma": 500})   # осколки не тронуты
        self.assertEqual(r["second"], 1)                                     # второй чертёж — со склада
        self.assertEqual(r["third"]["item"], 0)
        self.assertEqual(r["third"]["toast"], ["Нет чертежа: Чертёж Стража"])
        self.assertEqual(r["noBp"], {"btn": True, "note": True})

    # ---- Монета удачи ----
    def test_coin(self):
        r = self.r
        self.assertEqual(r["coin"], {"cx": 200, "left": 50})                # больше 200 Кодекс не берёт
        self.assertEqual(r["luckAtk"], [0, 40, 110, 220])
        self.assertEqual(r["luckCap"], 200)
        self.assertEqual(r["statAtk"], 220)                                 # плоская атака до множителей класса (у Стража атака ×1)
        self.assertEqual(r["codex"], {"cards": 5, "luckMax": True, "of200": True, "hint": True, "last": True})

    def test_map(self):
        m = self.r["map"]
        self.assertAlmostEqual(m["coin53"], items.P(items.LUCK_PCT))
        self.assertAlmostEqual(m["boss"], items.P(items.LUCK_PCT) * items.BOSS_LOOT)
        self.assertEqual(m["bossBps"], 0)
        self.assertFalse(m["sector"])
        self.assertEqual(m["bps53"], ["bp_guard@Око бездны", "bp_reaper@Око бездны", "bp_sniper@Око бездны", "bp_techno@Чёрный утёс", "bp_medic@Чёрный утёс"])
        self.assertEqual(m["bp49"], ["bp_medic", "bp_glyph", "bp_ghost"])   # 49 ур. стоит на трёх спотах
        self.assertTrue(self.r["mobCard"])
        names = {"guard": "Стража", "reaper": "Жнеца", "sniper": "Снайпера", "techno": "Техноманта", "ghost": "Призрака", "glyph": "Глифоносца", "medic": "Ремонтника"}
        self.assertEqual(self.r["spotCards"], [["Чертёж " + names[c] for c in items.WORLD_BP[s]] for s, *_ in mobguard.WORLD_SPOTS])
        self.assertTrue(self.r["mineGold"])                                  # чертёж своего класса — золотом

    def test_pickup_and_ground(self):
        p = self.r["pick"]
        self.assertEqual(p["toasts"], ["📐 Чертёж Призрака: по нему Кузнец соберёт вещь сета без осколков",
                                       "🪙 Монета удачи — сдай её в Кодекс → «Самоцветы»", "💎 Самоцвет: Изумрудная линза — сдай его в Кодекс"])
        self.assertEqual(p["chat"], ["Nova нашёл в Мире: Чертёж Призрака", "Nova нашёл в Мире: Монета удачи"])
        self.assertEqual(self.r["draw"], {"err": None, "left": 4})
        self.assertIn('["bp", "Чертежи классов"]', GAME)                     # админ может выдать чертёж (проверка на сервере)


class Translated(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "нет node")
    def test_every_language(self):
        import test_i18n_content as tic
        gen = re.findall(r':"([^"]+)"', GAME[GAME.index("const BP_OF = {"):GAME.index("};", GAME.index("const BP_OF = {"))])
        sets = re.findall(r'\bset:"([^"]+)"', GAME[GAME.index("const UNIQ_SETS = {"):GAME.index("\n};", GAME.index("const UNIQ_SETS = {"))])
        strings = ["Чертёж " + g for g in gen] + [f"Кузнец соберёт по нему любую вещь сета «{s}» без осколков · падает в Мире · продаётся на маркете за GRAM" for s in sets] + [
            "Монета удачи", "Монета с зубчатым краем из казны Владыки: кто носит её в корпусе, бьёт вернее · Сдаётся в Кодекс → «Самоцветы» за вечный бонус · продаётся на маркете за GRAM",
            "🪙 Монета удачи — сдай её в Кодекс → «Самоцветы»", "📐 Чертёж Ремонтника: по нему Кузнец соберёт вещь сета без осколков", "Nova нашёл в Мире: Монета удачи",
            "Собираются из Осколков или по Чертежу класса. Осколки падают с врагов от 15 уровня, чертежи — в Мире. Вещи сильнее Звёздного поколения и дают бонус сета.",
            "Или по чертежу", "Собрать по чертежу", "Нет чертежа", "Нет чертежа: Чертёж Призрака", "Чертёж Стража падает в Мире: Заросшая трасса, Око бездны. Его можно купить на маркете.",
            "Чертежи классов", "Сет «Цитадель»", "Мир: Каменный круг, Преддверие трона", "📐 Чертежи: 3", "Чертежи", "Чертёж Глифоносца (Склепы энергопийц)",
            "Падают только в Мире, у каждого спота свои, — шанс каждого 0,003% с монстра. По чертежу Кузнец соберёт любую вещь сета этого класса без осколков: нужны только лом и ядра. "
            "Чертежи продаются и покупаются на маркете за GRAM.",
            "Только в Мире · 0,003% с монстра, с Наместника чаще", "из 200", "До порога 80: ещё 45 · +70 АТК", "Порог 30 · Монета удачи: +40 АТК", "Собрано максимум: 200",
            "Монета удачи падает только в Мире, с любого монстра, и сдаётся рядом с самоцветами — до 200 штук. Её пороги тоже складываются.",
            "Сдать можно только монеты, учтённые сервером: выпавшие в Мире или купленные на маркете", "Нет в сумке и на складе: Монета удачи",
            # ответы сервера
            "Нет учтённых монет: сдать можно только выпавшие в Мире или купленные на маркете",
            "Нет учтённого чертежа: он должен выпасть в Мире или быть куплен на маркете",
            "Учтённых чертежей только 0: остальные нельзя продать за GRAM", "Учтённых монет удачи только 2: остальные нельзя продать за GRAM",
            "За GRAM можно продавать только снаряжение, сферы, осколки, самоцветы и чертежи с мобов, руны, дронов, артефакты, крылья и плащи"]
        self.assertEqual(len(gen), 7)
        self.assertEqual(len(sets), 7)
        res = tic.run_node(strings)
        self.assertEqual(res["bad"], [])
        for lang in tic.LANGS:
            russian = re.compile("[ыэъёЫЭЪЁ]" if lang == "uk" else "[А-Яа-яЁё]")
            with self.subTest(lang=lang):
                self.assertEqual([f"{s!r} → {t!r}" for s, t in zip(strings, res[lang]) if russian.search(t)], [])
        en = dict(zip(strings, res["en"]))
        self.assertEqual(en["Чертёж Стража"], "Guardian blueprint")
        self.assertEqual(en["Порог 30 · Монета удачи: +40 АТК"], "Threshold 30 · Lucky Coin: +40 ATK")

    def test_server_texts_are_the_ones_translated(self):
        for s in ("Нет учтённых монет: сдать можно только выпавшие в Мире или купленные на маркете",
                  "Нет учтённого чертежа: он должен выпасть в Мире или быть куплен на маркете",
                  "За GRAM можно продавать только снаряжение, сферы, осколки, самоцветы и чертежи с мобов, руны, дронов, артефакты, крылья и плащи"):
            self.assertIn(s, ITEMS_SRC)
            self.assertIn(f'"{s}": [', GAME)


class Pages(unittest.TestCase):
    def test_patch_notes(self):
        clean = re.sub(r"<!--.*?-->", "", PATCHES, flags=re.S)
        patches = re.findall(r'<section\b[^>]*\bdata-patch="([^"]+)"[^>]*data-title="([^"]+)"', clean)
        self.assertIn(("2026-10-18-5", "Чертежи классов и Монета удачи"), patches)                 # порядок патчей проверяет test_news
        self.assertIn('<li><a href="#p-1018-5"><time datetime="2026-10-18">18 октября 2026</time><span>Чертежи классов и Монета удачи</span></a></li>', PATCHES)
        sec = PATCHES[PATCHES.index('id="p-1018-5"'):PATCHES.index("</section>", PATCHES.index('id="p-1018-5"'))]
        self.assertEqual(sec.count('<span class="gem bp"'), 7)                                    # плитки чертежей и монеты — в патче
        self.assertIn('aria-label="Монета удачи"', sec)
        names = {"guard": "Стража", "reaper": "Жнеца", "sniper": "Снайпера", "techno": "Техноманта", "ghost": "Призрака", "glyph": "Глифоносца", "medic": "Ремонтника"}
        a = GAME.index("const W_SPOTS = [")
        spot_names = dict(re.findall(r'\{id:"(\w+)",\s*name:"([^"]+)"', GAME[a:GAME.index("];", a)]))
        for cls, gen in names.items():                                       # таблица «где падает» — как в игре
            row = re.search(r'<tr style="--c:[^"]+"><td><span class="bpn"><svg.*?</svg>Чертёж ' + gen + r'</span></td><td>(.*?)</td></tr>', sec)
            self.assertIsNotNone(row, cls)
            where = re.findall(r"<span>([^<]+) <small>", row.group(1))
            self.assertEqual(where, [spot_names[s] for s, c in items.WORLD_BP.items() if cls in c])
        for s in ("0,003%", "в 150 раз чаще", "0,45%", "<tr><td>30</td><td>+40 атаки</td></tr>", "<tr><td>80</td><td>+70 атаки</td></tr>",
                  "<tr><td>200</td><td>+110 атаки</td></tr>", "<td>Всего</td><td>+220 атаки</td>", "до 200 штук", "без осколков", "«Чертежи»"):
            self.assertIn(s, sec)
        boss = PATCHES[PATCHES.index('id="p-1018-4"'):PATCHES.index("</section>", PATCHES.index('id="p-1018-4"'))]
        self.assertIn('<figure class="boss"><img src="data:image/png;base64,', boss)   # спрайт босса переехал в его патч

    def test_icon_ids_unique(self):
        ids = re.findall(r'\bid="((?:gmi|hbp|sbp)\d+\w?)"', PATCHES)
        self.assertEqual(len(ids), len(set(ids)))

    def test_guide(self):
        self.assertIn("опыт ×3, чертежи классов и Монета удачи, на троне — босс 60 ур.", GUIDE)


if __name__ == "__main__":
    unittest.main()
