"""Мир — открытая локация с 35 уровня (патч LIBERTY 17 октября «Мир», шаг 1: сама локация).

* Три края: луг (лоу-споты 40–45, монстры сами не нападают), лес за рекой (мид-споты 46–52, пачки бросаются,
  если подойти близко) и выжженные пустоши за горной грядой (топ-споты 53–59); на востоке — Трон Владыки.
* Монстры wm40…wm59 стоят на 10 спотах пачками по 3/4/5 (по ярусу спота). Раскладка у всех телефонов
  одинаковая: номер моба N — один и тот же моб у всех (общие мобы, mobworld.py).
* Опыт за монстров ×3 (и на сервере: items.WORLD_XP), самоцветы падают и в Мире, снаряжение с монстров выше
  50 ур. — как с 50-го (items.WORLD_GEAR_LV), иначе с топ-спотов вещи не падали бы вовсе.

Сервер знает монстров Мира сам (mobguard, ledger, items, progress) — здесь эти зеркала сверяются с NPCS игры.
Поведение проверяется в настоящем коде игры (тот же запуск в node, что в tools/stat_caps.js).
Запуск: python -m unittest test_world
"""
import ast
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
# items.py и progress.py тянут aiohttp и базу; для этих проверок они не нужны — подменяем, если их нет
for name in ("aiohttp", "sqlalchemy", "db", "models", "metrics", "db_atomic"):
    try:
        __import__(name)
    except Exception:
        sys.modules[name] = mock.MagicMock()
if not hasattr(sys.modules["metrics"], "inc"):
    sys.modules["metrics"] = types.SimpleNamespace(inc=lambda *a, **k: None, gauge=lambda *a, **k: None)

import items      # noqa: E402
import ledger     # noqa: E402
import mobguard   # noqa: E402
import progress   # noqa: E402
import seasonpts  # noqa: E402

GAME = (ROOT / "game.html").read_text(encoding="utf-8")
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")
ITEMS_SRC = (ROOT / "items.py").read_text(encoding="utf-8")
PATCHES = (ROOT / "patches.html").read_text(encoding="utf-8")
GUIDE = (ROOT / "guide.html").read_text(encoding="utf-8")
LEVELS = range(40, 60)


def web_set(name):
    """Множество-литерал из webserver.py (сам модуль не импортируется: ему нужны aiohttp и база)."""
    for node in ast.parse(WEB).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(name)


def spots_from_game():
    """W_SPOTS из игры: [(id, имя, ярус, ур. от, ур. до)]."""
    body = GAME[GAME.index("const W_SPOTS = ["):GAME.index("];", GAME.index("const W_SPOTS = ["))]
    return [(i, n, int(t), int(lo), int(hi)) for i, n, t, lo, hi in
            re.findall(r'\{id:"(\w+)",\s*name:"([^"]+)",\s*tier:(\d),\s*lo:(\d+),\s*hi:(\d+)', body)]


class ServerKnowsTheWorld(unittest.TestCase):
    def test_location_is_allowed_and_pvp(self):
        self.assertIn("world", web_set("LOCS"))                          # без этого сервер не пускал бы в локацию
        self.assertNotIn("world", web_set("SAFE_LOCS"))                  # PvP-зона, как поля
        self.assertIn('LOCS[WORLD.KEY] = {name:"Мир", min:WORLD.MIN,', GAME)
        self.assertIn("const WORLD = {KEY:\"world\"", GAME)

    def test_mobs_only_in_the_world(self):
        for lv in LEVELS:
            self.assertEqual(mobguard.mob_level(f"wm{lv}", "world"), lv)
        for mob, loc in (("wm39", "world"), ("wm60", "world"), ("wm45", "sector2"), ("wm45", "farm1"),
                         ("dg40", "world"), ("db40", "world"), ("scrap_crawler", "world"), ("el30", "world"), ("wm5", "world")):
            self.assertIsNone(mobguard.mob_level(mob, loc), (mob, loc))   # в Мир не «призвать» главаря подземелья
        self.assertIsNone(mobguard.mob_hp("wm60"))
        self.assertIsNone(ledger.mob_info("wm39"))

    def test_gems_season_and_fields(self):
        self.assertIn("world", items.GEM_LOCS)
        self.assertEqual(json.loads(re.search(r"const GEM_LOCS = (\[[^\]]*\]);", GAME).group(1)), list(items.GEM_LOCS))
        self.assertEqual(seasonpts.kill_counter("world"), "kill_farm")    # «Убить монстров в полях»

    def test_xp_multiplier_matches_game(self):
        self.assertEqual(int(re.search(r'locKey === "world" \? (\d+) : 1\)', GAME).group(1)), items.WORLD_XP)
        self.assertEqual(int(re.search(r"const WORLD = \{[^}]*\bXP:(\d+)", GAME).group(1)), items.WORLD_XP)
        self.assertEqual(items.WORLD_XP, 3)
        self.assertIn("xm = WORLD_XP if loc == WORLD_LOC else 1", ITEMS_SRC)
        self.assertIn("cap = await progress.add_kill(s, uid, lv, boss, mult=xm)", ITEMS_SRC)
        self.assertIn("exp_out.append(progress.mob_exp(lv, boss) * xm)", ITEMS_SRC)      # союзникам по пати — с того же опыта

    def test_add_kill_counts_the_multiplier(self):
        got = {}

        async def fake_bump(s, uid, **inc):
            got.update(inc)
            return types.SimpleNamespace(exp=inc.get("exp", 0), base_lvl=1, bonus=0)

        with mock.patch.object(progress, "_bump", fake_bump):
            asyncio.run(progress.add_kill(None, 777, 59, mult=items.WORLD_XP))
            self.assertEqual(got["exp"], progress.mob_exp(59) * 3)
            asyncio.run(progress.add_kill(None, 777, 30))
            self.assertEqual(got["exp"], progress.mob_exp(30))
        progress.forget(777)

    def test_gear_from_top_spots(self):
        self.assertEqual(int(re.search(r"\bGEAR_LV:(\d+)", GAME).group(1)), items.WORLD_GEAR_LV)
        self.assertIn("gear_lv=min(lv, WORLD_GEAR_LV) if loc == WORLD_LOC else None", ITEMS_SRC)
        lucky = types.SimpleNamespace(random=lambda: 0.0, choice=lambda seq: seq[0], randint=lambda a, b: a)
        with mock.patch.object(items, "random", lucky):
            plain = [d for d in items.roll(59, 10**9) if d["kind"] == "gear"]
            world = [d for d in items.roll(59, 10**9, gear_lv=50) if d["kind"] == "gear"]
            lv45 = [d for d in items.roll(45, 10**9, gear_lv=45) if d["kind"] == "gear"]
            self.assertEqual(lv45, [d for d in items.roll(45, 10**9) if d["kind"] == "gear"])   # до 50 ур. — как раньше
        self.assertEqual(plain, [])                                      # вещей 49–61 ур. нет: без поправки — пусто
        self.assertEqual(sorted(d["g"] for d in world), [2, 3])          # синие и золотые, как у 50-го уровня
        need = dict(items.GEAR)
        self.assertTrue(all(40 <= need[d["id"]] <= 52 for d in world))

    def test_party_share_is_not_cut_by_the_phone(self):
        # доля союзника за одного монстра 59 ур. в Мире: 40% от ×3 — больше прежнего потолка 500
        self.assertGreater(int(progress.mob_exp(59) * items.WORLD_XP * 0.4), 500)
        self.assertIn("const a = Math.max(0, Math.min(5000, +d.amount || 0)); if(!a) return;", GAME)


class PagesDescribeTheWorld(unittest.TestCase):
    def test_patch_notes(self):
        clean = re.sub(r"<!--.*?-->", "", PATCHES, flags=re.S)
        patches = re.findall(r'<section\b[^>]*\bdata-patch="([^"]+)"[^>]*data-title="([^"]+)"', clean)
        self.assertIn(("2026-10-18-3", "Мир"), patches)
        self.assertIn('<li><a href="#p-1018-3"><time datetime="2026-10-18">18 октября 2026</time><span>Мир</span></a></li>', PATCHES)
        sec = PATCHES[PATCHES.index('id="p-1018-3"'):PATCHES.index("</section>", PATCHES.index('id="p-1018-3"'))]
        for _id, name, _tier, lo, hi in spots_from_game():                # таблица спотов — как в игре
            self.assertIn(f"<tr><td>{name}</td><td>{lo}–{hi}</td></tr>", sec)
        self.assertIn("втрое больше обычного", sec)
        self.assertIn("Самоцветы падают и в Мире", sec)

    def test_guide(self):
        self.assertIn('<b>Мир</b><small>Открытая локация: 10 спотов с монстрами 40–59, опыт ×3</small></div><em class="lv">УР. 35+</em>', GUIDE)
        self.assertIn('<div class="point p6"><span>Мир</span></div>', GUIDE)

    def test_codex_says_where_gems_drop(self):
        self.assertIn("Самоцветы падают с врагов в Нижних и Глубинных цехах, Фарм-зоне, Сезонной зоне и в Мире.", GAME)


@unittest.skipUnless(shutil.which("node"), "нет node")
class InGame(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        head = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8")
        head = head[:head.index("\n", head.index("const run = "))]
        check = r'''
const out = run(`(() => {
  const r = {};
  S.level = 45; S.cls = "guard"; S.skl = {}; S.pas = {}; S.up = {}; paused = false; auto = false; now = 100; vw = 390; vh = 844;
  const put = (x, y) => { p = {x, y, r:20, dead:false, aim:0, ang:0, atkT:0, stunT:0, target:null, vx:0, vy:0}; };
  const sp = buildLoc("world"); put(sp.x, sp.y);
  r.loc = {key:locKey, W, H, min:LOCS.world.min, safe:!!LOCS.world.safe, arena:!!LOCS.world.arena, hidden:!!LOCS.world.hidden,
           portal:npcs.map(n => n.kind), spawnFree:Math.hypot(sp.x - npcs[0].x, sp.y - npcs[0].y) > 100};
  r.npcs = {};
  for(let L = 40; L <= 59; L++){ const k = NPCS["wm" + L]; r.npcs["wm" + L] = {lvl:k.lvl, hp:k.hp, exp:k.exp, scrap:k.scrap, loot:k.loot, name:k.name, world:!!k.world}; }
  r.spots = W_SPOTS.map(s => ({id:s.id, tier:s.tier, lo:s.lo, hi:s.hi, packs:s.packs}));
  r.tiers = W_TIERS.map(t => ({name:t.name, pack:t.pack, aggro:t.aggro}));
  r.count = enemies.length;
  const packs = {}; for(const e of enemies) (packs[e.pack] = packs[e.pack] || []).push(e);
  r.packs = Object.values(packs).map(l => ({n:l.length, spot:l[0].spot.id, types:[...new Set(l.map(e => e.type))].length, lv:NPCS[l[0].type].lvl}));
  r.idx = enemies.every((e, i) => e.idx === i);
  let stuck = 0; for(const e of enemies){ const o = {x:e.x, y:e.y}; pushOut(o, NPCS[e.type].r); if(Math.hypot(o.x - e.x, o.y - e.y) > 0.5) stuck++; }
  r.stuck = stuck;
  const sig = () => enemies.map(e => e.type + ":" + e.x + "," + e.y + ":" + e.pack).join("|");
  const s1 = sig(); buildLoc("lobby"); buildLoc("world"); r.same = s1 === sig();

  // река и гряда: из воды и со скал выталкивает на берег, на мосту и перевале — можно стоять
  const at = (x, y) => { const o = {x, y}; pushOut(o, 20); return o; };
  const ry = 1000, B = WORLD.river.bridges[1], P = WORLD.ridge.passes[1];
  const w1 = at(wRiverX(ry), ry), w2 = at(wRiverX(B), B), g1 = at(wRidgeX(ry), ry), g2 = at(wRidgeX(P), P);
  r.water = {out:Math.abs(w1.x - wRiverX(w1.y)) - WORLD.river.w/2, bridge:[w2.x - wRiverX(B), w2.y - B]};
  r.ridge = {out:Math.abs(g1.x - wRidgeX(g1.y)) - WORLD.ridge.w/2, pass:[g2.x - wRidgeX(P), g2.y - P]};
  const e1 = at(-40, 1400), e2 = at(W + 60, H + 60); r.edges = [e1.x, e2.x, e2.y];
  // по главной дороге через мост и перевал проходится без препятствий
  let o = {x:WORLD.spawn.x, y:1400};
  const road = W_ROADS[0]; let blocked = 0;
  for(let i = 1; i < road.length; i++){ const [x1, y1] = road[i]; for(let n = 0; n < 400 && Math.hypot(x1 - o.x, y1 - o.y) > 4; n++){
    const d = Math.hypot(x1 - o.x, y1 - o.y), ox = o.x, oy = o.y; o.x += (x1 - o.x)/d*4; o.y += (y1 - o.y)/d*4; pushOut(o, 20);
    if(Math.hypot(o.x - ox, o.y - oy) < 2) blocked++; } }
  r.road = {end:[Math.round(o.x), Math.round(o.y)], blocked};

  // лоу-спот: подошёл вплотную — стоят; ударил одного — бросается вся пачка
  const st = stat(), tick = n => { for(let i = 0; i < n; i++){ now += 0.05; updateEnemies(0.05, st); } };
  buildLoc("world"); now = 100;
  const low = enemies.find(e => e.spot.tier === 0), lowPack = enemies.filter(e => e.pack === low.pack);
  put(low.x + NPCS[low.type].r + 30, low.y); tick(4);
  r.lowNear = lowPack.filter(e => e.chase).length;
  hitEnemy(low, 1); r.lowHit = [lowPack.filter(e => e.chase).length, lowPack.length];
  // мид-спот: подошёл близко — бросается вся пачка; издалека — нет
  buildLoc("world"); now = 100;
  const mids = enemies.filter(e => e.spot.tier === 1), mid = mids[0], midPack = enemies.filter(e => e.pack === mid.pack);
  put(mid.x + NPCS[mid.type].r + 40, mid.y); tick(1);
  r.midNear = [midPack.filter(e => e.chase).length, midPack.length];
  buildLoc("world"); now = 100;
  const far = enemies.find(e => e.spot.tier === 2), farPack = enemies.filter(e => e.pack === far.pack);
  put(far.x, far.y - 260); tick(2);
  r.topFar = farPack.filter(e => e.chase).length;
  // увёл монстра от спота дальше поводка — бросает погоню и уходит на своё место
  buildLoc("world"); now = 100;
  const lm = enemies.find(e => e.spot.tier === 1), s0 = lm.spot;
  lm.chase = true; lm.x = s0.x; lm.y = s0.y + s0.r + WORLD.LEASH + 40; put(lm.x, lm.y + 60);
  tick(1); r.leash = lm.chase;
  const d0 = Math.hypot(lm.x - lm.hx, lm.y - lm.hy); put(lm.x + 300, lm.y + 200); tick(20);
  r.goesHome = Math.hypot(lm.x - lm.hx, lm.y - lm.hy) < d0;

  // опыт: в Мире ×3, в поле — как обычно
  buildLoc("world"); S.exp = 0; const k1 = enemies[0];
  killEnemy(k1); r.xp = {world:S.exp, base:NPCS[k1.type].exp};
  locKey = "scrapfields"; S.exp = 0; killEnemy(Object.assign({}, enemies[1], {type:k1.type, dead:false})); r.xp.field = S.exp; locKey = "world";

  // дроп: вещи с монстра 59 ур. — как с 50-го, самоцветы есть
  const D = mobDrops("wm59", "world");
  r.gear59 = [...new Set(D.gear.pool.map(id => ITEMS[id].lvl))].sort((a, b) => a - b);
  r.per59 = D.gear.per;
  r.gems = mobDrops("wm45", "world").gems.map(g => g.id);

  // подписи пачек: ближние — первыми, наехавшие друг на друга и на своего робота — уходят под пачку или прячутся
  buildLoc("world");
  const mill = W_SPOTS.find(s => s.id === "mill"), mp = [...new Set(enemies.filter(e => e.spot === mill).map(e => e.pack))];
  const pk = id => enemies.filter(e => e.pack === id), A = pk(mp[0]), Bp = pk(mp[1]), C = pk(mp[2]);
  Bp.forEach((e, i) => { e.x = A[i % A.length].x + 46; e.y = A[i % A.length].y + 10; });     // пачка B встала вплотную к A
  const top = l => l.reduce((a, e) => e.y < a.y ? e : a), bot = l => l.reduce((a, e) => e.y > a.y ? e : a);
  put(top(C).x, top(C).y + 22);                                                              // робот прямо под пачкой C
  const drawn = []; nameText = (text, x, y) => drawn.push([text, x, y]); wTextW = s => s.length*6 + 8;
  const cx = p.x - vw/2, cy = p.y - vh/2;
  wDrawLive(cx, cy, 0);
  r.spotLbl = {lbl:WORLD.lbl, ringBottom:mill.y + mill.r*0.86, mill:[mill.x, mill.y]};
  drawn.length = 0;
  const busy = wBusy(cx, cy).concat(WORLD.lbl);
  r.me = busy[0];
  wPackLabels(cx, cy, busy);
  r.labels = drawn;
  // где оказалась подпись пачки: над ней, под ней или спрятана
  const place = l => {
    const n = l.length, sx = l.reduce((a, e) => a + e.x, 0), x0 = Math.min(...l.map(e => e.x)), x1 = Math.max(...l.map(e => e.x)), rv = NPCS[l[0].type].r*MOB_K;
    const lx = m => n > 1 && x0 > m.x - 120 && x1 < m.x + 120 ? sx/n : m.x;
    const at = (x, y) => drawn.some(d => Math.abs(d[1] - x) < 0.01 && Math.abs(d[2] - y) < 0.01);
    return at(lx(top(l)), top(l).y - rv - 20) ? "above" : at(lx(bot(l)), bot(l).y + rv*1.1 + 24) ? "below" : "hidden";
  };
  r.place = {A:place(A), B:place(Bp), C:place(C)};
  return JSON.stringify(r);
})()`);
process.stdout.write(out + "\n");
'''
        res = subprocess.run(["node", "-", str(ROOT / "game.html")], input=head + check, capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            raise AssertionError(res.stderr[-3000:])
        cls.r = json.loads(res.stdout.strip().splitlines()[-1])

    # ---- сервер считает монстров Мира так же, как игра ----
    def test_server_mirrors(self):
        for lv in LEVELS:
            k = self.r["npcs"][f"wm{lv}"]
            with self.subTest(lv=lv):
                self.assertTrue(k["world"])
                self.assertEqual(k["lvl"], lv)
                self.assertEqual(mobguard.mob_hp(f"wm{lv}"), k["hp"])
                self.assertEqual(progress.mob_exp(lv), k["exp"])
                self.assertEqual(ledger.mob_info(f"wm{lv}"), (lv, tuple(k["scrap"]), k["loot"], False))
                inc = ledger.kill_income(f"wm{lv}", "world")
                self.assertTrue(inc["scrap"] > 0 and inc["cores"] > 0)

    # ---- раскладка ----
    def test_location(self):
        L = self.r["loc"]
        self.assertEqual((L["key"], L["W"], L["H"], L["min"]), ("world", 7200, 2800, 35))
        self.assertFalse(L["safe"] or L["arena"] or L["hidden"])          # в списке портала и PvP-зона
        self.assertEqual(L["portal"], ["back"])
        self.assertTrue(L["spawnFree"])

    def test_spots_and_packs(self):
        r = self.r
        self.assertEqual(len(r["spots"]), 10)
        self.assertEqual([(s["id"], s["tier"], s["lo"], s["hi"]) for s in r["spots"]], [(i, t, lo, hi) for i, _n, t, lo, hi in spots_from_game()])
        self.assertEqual([t["pack"] for t in r["tiers"]], [3, 4, 5])
        self.assertEqual([t["aggro"] for t in r["tiers"]], [False, True, True])
        by_id = {s["id"]: s for s in r["spots"]}
        self.assertEqual(len(r["packs"]), sum(s["packs"] for s in r["spots"]))     # на каждом споте — все его пачки
        self.assertEqual(r["count"], sum(s["packs"] * r["tiers"][s["tier"]]["pack"] for s in r["spots"]))
        for pk in r["packs"]:
            s = by_id[pk["spot"]]
            self.assertEqual(pk["n"], r["tiers"][s["tier"]]["pack"])
            self.assertEqual(pk["types"], 1)                                  # в пачке монстры одного вида
            self.assertTrue(s["lo"] <= pk["lv"] <= s["hi"], pk)
        for s in r["spots"]:                                                  # на споте есть и младший, и старший уровень
            lvs = {pk["lv"] for pk in r["packs"] if pk["spot"] == s["id"]}
            self.assertEqual((min(lvs), max(lvs)), (s["lo"], s["hi"]))

    def test_same_for_everyone(self):
        self.assertTrue(self.r["same"])          # номер моба N — один и тот же моб у всех телефонов
        self.assertTrue(self.r["idx"])
        self.assertEqual(self.r["stuck"], 0)     # ни один монстр не стоит в дереве, постройке, воде или скале

    def test_river_ridge_and_edges(self):
        r = self.r
        self.assertGreaterEqual(r["water"]["out"], 20 - 0.5)                  # из воды — на берег
        self.assertEqual(r["water"]["bridge"][0], 0)                          # на мосту стоять можно
        self.assertLessEqual(abs(r["water"]["bridge"][1]), 116 / 2)
        self.assertGreaterEqual(r["ridge"]["out"], 20 - 0.5)                  # со скал — вниз
        self.assertEqual(r["ridge"]["pass"][0], 0)                            # перевал проходим
        self.assertGreaterEqual(r["edges"][0], 20)
        self.assertLessEqual(r["edges"][1], 7200 - 20)
        self.assertLessEqual(r["edges"][2], 2800 - 20)
        self.assertEqual(r["road"]["blocked"], 0)                             # главная дорога: от портала до Преддверия
        self.assertLess(abs(r["road"]["end"][0] - 6500) + abs(r["road"]["end"][1] - 1400), 5)

    # ---- поведение пачек ----
    def test_low_spot_waits_for_a_hit(self):
        self.assertEqual(self.r["lowNear"], 0)
        self.assertEqual(self.r["lowHit"][0], self.r["lowHit"][1])

    def test_mid_and_top_spots_charge_up_close(self):
        self.assertEqual(self.r["midNear"][0], self.r["midNear"][1])
        self.assertEqual(self.r["topFar"], 0)

    def test_leash(self):
        self.assertFalse(self.r["leash"])
        self.assertTrue(self.r["goesHome"])

    # ---- опыт и дроп ----
    def test_triple_xp(self):
        xp = self.r["xp"]
        self.assertEqual(xp["field"], xp["base"])
        self.assertEqual(xp["world"], round(xp["base"] * 3))

    def test_drops(self):
        self.assertEqual(self.r["gear59"], [40, 45])                           # вещи старших поколений
        self.assertEqual([round(v * 1e6) for v in self.r["per59"]], [round(v * 1e6) for v in items.gear_per(50)])
        self.assertEqual(sorted(self.r["gems"]), sorted(items.GEMS))

    # ---- подписи ----
    def test_spot_name_lies_under_the_ring(self):
        sl = self.r["spotLbl"]
        mill = [b for b in sl["lbl"] if b[0] < sl["mill"][0] < b[2]]
        self.assertTrue(mill)
        self.assertGreater(mill[0][1], sl["ringBottom"])                       # под кольцом: пачки стоят внутри

    def test_crowded_labels_move_or_hide(self):
        pl = self.r["place"]
        self.assertEqual(pl["C"], "below")                                     # над пачкой — ник своего робота
        self.assertIn("above", (pl["A"], pl["B"]))                             # из двух слипшихся пачек одна подписана сверху,
        self.assertNotEqual(pl["A"], pl["B"])                                  # другая — снизу или без подписи

    def test_pack_labels_do_not_overlap(self):
        labels = self.r["labels"]                                              # подпись пачки — имя, затем строка уровня
        names, lvls = labels[0::2], labels[1::2]
        self.assertGreaterEqual(len(names), 3)
        self.assertEqual(len(names), len(lvls))
        w = lambda s: len(s) * 6 + 8                                           # wTextW в проверке
        rects = []
        for (t, x, y), (t2, x2, y2) in zip(names, lvls):
            self.assertEqual((x, y - 13), (x2, y2))
            self.assertRegex(t, r" ×[345]$")
            ww = max(w(t), w(t2))
            rects.append([x - ww / 2, y - 23, x + ww / 2, y + 4])
        hit = lambda a, b: a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]
        for i, a in enumerate(rects):
            self.assertFalse(hit(a, self.r["me"]), "подпись на своём роботе")
            for b in rects[i + 1:]:
                self.assertFalse(hit(a, b), (a, b))


@unittest.skipUnless(shutil.which("node"), "нет node")
class Translated(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_i18n_content as tic
        body = GAME[GAME.index("const W_ZONES = {"):GAME.index("}}};", GAME.index("const W_ZONES = {"))]
        zones = re.findall(r'name:"([^"]+)", sub:"([^"]+)"', body)
        tiers = re.findall(r'\{name:"([^"]+)", pack:(\d)', GAME[GAME.index("const W_TIERS = ["):GAME.index("];", GAME.index("const W_TIERS = ["))])
        prefix = re.findall(r'"([^"]+)"', GAME[GAME.index("const W_PREFIX = ["):GAME.index("];", GAME.index("const W_PREFIX = ["))])
        nouns = re.findall(r'\["\w+","([^"]+)"\]', GAME[GAME.index("const DG_BASE = ["):GAME.index("\n", GAME.index("const DG_BASE = ["))])
        cls.mobs = [prefix[lv - 40] + " " + nouns[(lv - 1) % 4] for lv in LEVELS]
        desc = re.search(r'LOCS\[WORLD\.KEY\] = \{[^}]*desc:"([^"]+)"', GAME.replace("\n  desc", " desc")).group(1)
        cls.strings = [desc, "Открытый мир · PvP-зона", "Трон Владыки", "Споты", "Мир · врагов: 168"] + [z for pair in zones for z in pair] + cls.mobs
        cls.strings += [m + " ×4" for m in cls.mobs[:3]]
        for _id, name, tier, lo, hi in spots_from_game():
            tname, pack = tiers[tier]
            sub = f"Ур. {lo}–{hi} · {tname} · пачки по {pack}"
            cls.strings += [name, sub, sub + " · " + ("бросаются, если подойти близко" if tier else "сами не нападают")]
        cls.res = tic.run_node(cls.strings)
        cls.langs = tic.LANGS

    def test_every_language(self):
        self.assertEqual(len(self.mobs), 20)
        for lang in self.langs:
            russian = re.compile("[ыэъёЫЭЪЁ]" if lang == "uk" else "[А-Яа-яЁё]")
            with self.subTest(lang=lang):
                self.assertEqual([f"{s!r} → {t!r}" for s, t in zip(self.strings, self.res[lang]) if russian.search(t)], [])

    def test_names_read_naturally(self):
        en, es = dict(zip(self.strings, self.res["en"])), dict(zip(self.strings, self.res["es"]))
        self.assertEqual(en["Ур. 57–59 · Топ-спот · пачки по 5"], "Lv. 57–59 · Top spot · packs of 5")
        self.assertEqual(en["Заросший Шагоход ×4"], "Overgrown Walker ×4")
        self.assertEqual(es["Заросший Шагоход"], "Caminante enmalezado")          # прилагательное после существительного
        self.assertEqual(en["Открытый мир · PvP-зона"], "Open world · PvP zone")
        self.assertEqual(self.res["bad"], [])


if __name__ == "__main__":
    unittest.main()
