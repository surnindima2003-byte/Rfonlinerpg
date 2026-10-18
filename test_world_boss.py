"""Мир, шаг 2: босс трона «Железный Наместник» (как Владыка Мира в LIBERTY, патч 17 октября).

* 60 уровень, стоит на каменной площади за кольцом колонн; общий моб Мира (прочность — на сервере).
* Возвращается через 1–2 часа; время хранится в базе, перезапуск сервера его не сбрасывает; таймер видят все.
* Добычу и опыт получает каждый, кто снёс хотя бы 1% прочности (до 30 пилотов): каждому сервер засчитывает бой.
* Добыча — как с главаря (шансы ×150), сверх того камни заточки: 4–6 медных сфер и с шансом 35% титановая.
* Удары по площади по часам: «Удар скипетром» (кольцо 230 px, 25% прочности) и ниже 50% — «Кара трона» (20%).

Запуск: python -m unittest test_world_boss
"""
import ast
import json
import re
import shutil
import subprocess
import sys
import time
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
import ledger       # noqa: E402
import mobguard     # noqa: E402
import mobworld     # noqa: E402
import progress     # noqa: E402
import worldthrone  # noqa: E402

GAME = (ROOT / "game.html").read_text(encoding="utf-8")
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")
ITEMS_SRC = (ROOT / "items.py").read_text(encoding="utf-8")
PATCHES = (ROOT / "patches.html").read_text(encoding="utf-8")
GUIDE = (ROOT / "guide.html").read_text(encoding="utf-8")
BOSS, LOC, IDX = worldthrone.TYPE, worldthrone.LOC, worldthrone.IDX


class Conn:
    def __init__(self, uid):
        self.uid = uid


class Hub:
    def __init__(self):
        self.loc, self.all, self.by_loc = [], [], {LOC: {Conn(1), Conn(2), Conn(3)}}

    def to_loc(self, loc, p, skip_uid=None):
        self.loc.append(p)

    def to_all(self, p):
        self.all.append(p)


class ServerMirrors(unittest.TestCase):
    def test_one_boss_only_in_the_world(self):
        self.assertEqual((BOSS, worldthrone.LVL, worldthrone.HP), ("wt60", 60, 500_000))
        self.assertEqual(mobguard.mob_level(BOSS, "world"), 60)
        for loc in ("sector2", "farm1", "wboss", "lobby"):
            self.assertIsNone(mobguard.mob_level(BOSS, loc))
        self.assertIsNone(mobguard.mob_level("wt59", "world"))
        self.assertEqual(mobguard.mob_hp(BOSS), worldthrone.HP)
        self.assertTrue(mobguard.is_boss(BOSS) and mobguard.is_boss("db40") and not mobguard.is_boss("wm59"))

    def test_ledger_like_a_boss(self):
        self.assertEqual(ledger.mob_info(BOSS), (60, (610, 1240), {"plate": 0.3, "chip": 0.18}, True))
        inc = ledger.kill_income(BOSS, "world")
        self.assertEqual(inc["scrap"], 1240 * (1 + ledger.LOOT_MAX))           # лом — всегда, как с главаря

    def test_gift_spheres(self):
        self.assertEqual(items.THRONE_GIFT, (("sph_cu", 4, 6, 1.0), ("sph_ti", 1, 1, 0.35)))
        lucky = types.SimpleNamespace(random=lambda: 0.0, randint=lambda a, b: b, choice=lambda s: s[0])
        with mock.patch.object(items, "random", lucky):
            self.assertEqual(items.roll_throne(), [{"kind": "sph", "id": "sph_cu", "n": 6}, {"kind": "sph", "id": "sph_ti", "n": 1}])
        unlucky = types.SimpleNamespace(random=lambda: 0.5, randint=lambda a, b: a, choice=lambda s: s[0])
        with mock.patch.object(items, "random", unlucky):
            self.assertEqual(items.roll_throne(), [{"kind": "sph", "id": "sph_cu", "n": 4}])      # медные — всегда

    def test_process_kills_treats_it_as_boss(self):
        self.assertIn("boss = mobguard.is_boss(mob)", ITEMS_SRC)                    # шансы ×BOSS_LOOT, опыт ×15
        self.assertIn("if mob == mobguard.THRONE_BOSS:", ITEMS_SRC)               # и камни заточки
        self.assertIn("for d in roll_throne():", ITEMS_SRC)
        self.assertIn("if exp_out is not None and mob != mobguard.THRONE_BOSS:", ITEMS_SRC)   # доли пати нет: добыча у каждого своя
        self.assertEqual(progress.mob_exp(60, boss=True) * items.WORLD_XP, 39519)

    def test_webserver_wiring(self):
        self.assertIn("import worldthrone", WEB)
        self.assertIn("worldthrone.tick(hub)", WEB)
        self.assertIn("conn.push(realtime.encode(worldthrone.view()))", WEB)       # при входе в игру — таймер
        self.assertIn('app.router.add_post("/api/wthrone", api_wthrone)', WEB)
        a, b = WEB.index("worldthrone.persist = save_throne"), WEB.index("await load_throne()")
        self.assertLess(a, b)
        self.assertLess(b, WEB.index("runner = web.AppRunner(app)"))              # до тиков мира и приёма игроков


class SharedBoss(unittest.TestCase):
    def setUp(self):
        mobworld._mobs.clear(); mobworld._dirty.clear(); mobworld._own.clear(); mobworld._moved.clear(); mobworld._budget.clear()
        worldthrone.st.update(until=0.0, told=True)
        self.hub = Hub()
        self.p = {u: {"id": u, "loc": LOC, "lvl_cap": 50} for u in (1, 2, 3)}

    def hit(self, uid, dmg, typ=BOSS):
        mobworld._budget.pop(uid, None)                                        # бюджет урона здесь не проверяем
        mobworld.on_hits(self.p[uid], {"loc": LOC, "h": [[IDX, typ, dmg]]}, self.hub)

    def beat(self, parts):
        """Добить босса долями прочности {uid: доля}."""
        for uid, share in parts.items():
            left = round(worldthrone.HP * share)
            while left > 0:
                d = min(left, 40_000); self.hit(uid, d); left -= d

    def test_loot_for_everyone_who_helped(self):
        self.beat({3: 0.004, 2: 0.39, 1: 0.61})
        die = [m for m in self.hub.loc if m["t"] == "mdie"][0]
        self.assertEqual(die["by"], 1)
        self.assertEqual(die["share"], [1, 2])                                 # 0,4% — мало: без добычи
        self.assertTrue(3600 <= die["back"] <= 7200)
        for uid in (1, 2):                                                    # заявка на убийство пройдёт проверку
            self.assertIsNone(mobguard.check_kill(uid, die["iid"], BOSS))
        self.assertEqual(mobguard.check_kill(3, die["iid"], BOSS), "no_hits")
        m = mobworld._mobs[LOC][IDX]
        self.assertAlmostEqual(m["dead_until"], worldthrone.st["until"], places=3)
        self.assertEqual(self.hub.all[-1], worldthrone.view())                 # таймер — всем
        self.assertGreater(self.hub.all[-1]["until"], time.time() * 1000)
        self.assertTrue(any(x["t"] == "pinfo" and "Добычу получили пилотов: 2" in x["text"] for x in self.hub.loc))

    def test_dead_boss_ignores_hits_and_returns_on_time(self):
        self.beat({1: 1.0})
        n = len([m for m in self.hub.loc if m["t"] == "mdie"])
        self.hit(2, 50_000)
        self.assertEqual(len([m for m in self.hub.loc if m["t"] == "mdie"]), n)
        self.assertEqual(mobworld._mobs[LOC][IDX]["hp"], worldthrone.HP)
        sec = dict(mobworld.state(LOC)["dead"])[IDX]                           # вошедший видит, сколько осталось
        self.assertTrue(3500 < sec <= 7200)
        worldthrone.st["until"] = mobworld._mobs[LOC][IDX]["dead_until"] = time.time() - 1
        mobworld.tick(self.hub); worldthrone.tick(self.hub)
        self.assertIn({"t": "mres", "loc": LOC, "i": IDX}, self.hub.loc)
        self.assertEqual(self.hub.all[-1], {"t": "wt", "until": 0})
        self.assertTrue(any(x["t"] == "pinfo" and "вернулся" in x["text"] for x in self.hub.loc))

    def test_restart_keeps_the_timer(self):
        until = time.time() + 1800
        worldthrone.restore(json.dumps({"until": until, "i": IDX}))
        self.assertFalse(worldthrone.alive())
        self.hit(1, 10_000)
        self.assertEqual(mobworld._mobs[LOC][IDX]["hp"], worldthrone.HP)       # не принят: босс ещё не вернулся
        self.assertIn(IDX, dict(mobworld.state(LOC)["dead"]))
        self.assertEqual(worldthrone.view()["until"], int(until * 1000))
        mobworld._mobs.clear(); worldthrone.st.update(until=0.0)
        worldthrone.restore(json.dumps({"until": time.time() - 5, "i": IDX}))    # срок прошёл — жив
        worldthrone.restore("not json")
        self.assertTrue(worldthrone.alive())
        self.assertNotIn(LOC, mobworld._mobs)

    def test_without_state_a_dead_boss_cannot_be_hit(self):
        worldthrone.st.update(until=time.time() + 600)                          # состояние мобов потеряно, таймер — нет
        self.hit(1, 10_000)
        self.assertNotIn(IDX, mobworld._mobs.get(LOC, {}))

    def test_slow_regen(self):
        self.beat({1: 0.2})                                                    # 100 000
        m = mobworld._mobs[LOC][IDX]
        m["last"] = time.time() - mobworld.REGEN_AFTER - 1                     # обычный моб уже восстановился бы
        mobworld.tick(self.hub)
        self.assertEqual(m["hp"], worldthrone.HP - 100_000)
        m["last"] = time.time() - worldthrone.REGEN_AFTER - 1
        mobworld.tick(self.hub)
        self.assertNotIn(IDX, mobworld._mobs[LOC])                            # восстановлен (здоровых сервер забывает)

    def test_regular_mobs_unchanged(self):
        mobworld.on_hits(self.p[1], {"loc": LOC, "h": [[3, "wm40", 5000]]}, self.hub)
        die = [m for m in self.hub.loc if m["t"] == "mdie"][0]
        self.assertNotIn("share", die)
        self.assertAlmostEqual(mobworld._mobs[LOC][3]["dead_until"] - time.time(), mobworld.RESPAWN, delta=1)

    def test_admin_respawn(self):
        self.beat({1: 1.0})
        worldthrone.respawn_now(self.hub)
        self.assertTrue(worldthrone.alive())
        self.assertIn({"t": "mres", "loc": LOC, "i": IDX}, self.hub.loc)
        self.assertEqual(self.hub.all[-1], {"t": "wt", "until": 0})

    def test_share_cap(self):
        self.p.update({u: {"id": u, "loc": LOC, "lvl_cap": 50} for u in range(10, 50)})
        for u in range(10, 50):
            self.hit(u, 6_000)                                                 # 40 пилотов по 1,2%
        self.beat({1: 0.6})
        die = [m for m in self.hub.loc if m["t"] == "mdie"][0]
        self.assertEqual(len(die["share"]), worldthrone.SHARE_MAX)
        self.assertEqual(die["share"][0], 1)


@unittest.skipUnless(shutil.which("node"), "нет node")
class InGame(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        head = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8")
        head = head[:head.index("\n", head.index("const run = "))]
        check = r'''
const out = run(`(() => {
  const r = {}, T0 = WORLD.throne;
  S.level = 50; S.cls = "guard"; S.skl = {}; S.pas = {}; S.up = {}; S.max_hp = 20000; paused = false; auto = false; now = 100; vw = 390; vh = 844;
  const put = (x, y) => { p = {x, y, r:20, dead:false, aim:0, ang:0, atkT:0, stunT:0, target:null, vx:0, vy:0, inv:0}; };
  buildLoc("world"); put(WORLD.spawn.x, WORLD.spawn.y);
  const b = wtBoss();
  r.boss = {idx:b.idx, n:enemies.length, last:enemies[enemies.length - 1] === b, spotMobs:enemies.filter(e => e.spot && !e.throne).length, type:b.type, dead:b.dead};
  r.npc = NPCS.wt60; r.wt = {IDX:WT.IDX, HP:WT.HP, LVL:WT.LVL, TYPE:WT.TYPE};
  const o = {x:b.x, y:b.y}; pushOut(o, NPCS.wt60.r); r.homeFree = Math.hypot(o.x - b.x, o.y - b.y) < 0.5;
  r.onPlaza = Math.hypot(b.x - T0.x, b.y - T0.y) < T0.R - 60;
  const st = stat(), tick = n => { for(let i = 0; i < n; i++){ now += 0.05; updateEnemies(0.05, st); } };
  put(T0.x - 260, T0.y - 200); tick(2); r.farChase = b.chase;              // у колонн, далеко от босса — стоит
  put(b.x - 200, b.y); tick(1); r.nearChase = b.chase;                     // подошёл — встаёт с трона
  put(T0.x - T0.R - 300, T0.y); tick(2); r.leftChase = b.chase;            // ушёл с площади — назад
  b.stun = 3; tick(1); r.stun = b.stun;                                    // не оглушается
  // «Удар скипетром»: по часам, кольцо 230 px вокруг босса, 25% прочности; вышел — цел
  buildLoc("world"); const B = wtBoss(); B.chase = true; put(B.x + 60, B.y); S.hp = stat().maxhp;
  const noHit = () => { B.cd = 1e9; }, _now = Date.now;
  let T = Math.floor(_now()/9000)*9000 + 9000*50; Date.now = () => T;
  const hp0 = S.hp; T -= 2000; noHit(); updateEnemies(0.016, st); r.k0 = WT.wave.k;
  T += 1500; noHit(); updateEnemies(0.016, st); r.armed = WT.wave.armed; r.k1 = +WT.wave.k.toFixed(2);
  T += 600; noHit(); updateEnemies(0.016, st); r.slam = +((hp0 - S.hp)/stat().maxhp).toFixed(3); r.flash = WT.wave.flash > 0;
  S.hp = stat().maxhp; const hp1 = S.hp; put(B.x + 300, B.y);
  T += 9000 - 1500; noHit(); updateEnemies(0.016, st); T += 1600; noHit(); updateEnemies(0.016, st); r.dodged = hp1 - S.hp;
  B.hp = B.max*0.35; T = Math.floor(T/6000)*6000 + 6000*3; WT.wave.slot = -1; noHit(); updateEnemies(0.016, st); const s6 = WT.wave.slot;
  T += 6000; noHit(); updateEnemies(0.016, st); r.rageSlot = WT.wave.slot - s6;                       // ярость: круг каждые 6 с
  // «Кара трона»: ниже 50% — круг под ногами; стоишь — 20%, ушёл — цел
  buildLoc("world"); const C = wtBoss(); C.chase = true; C.hp = C.max*0.45; put(C.x + 300, C.y); S.hp = stat().maxhp;
  T = Math.floor(_now()/7000)*7000 + 7000*80 + 3600; WT.doom.slot = -1; updateEnemies(0.016, st);
  const c0 = WT.doom.list[0]; r.doomAt = c0 ? [Math.round(c0.x - p.x), Math.round(c0.y - p.y)] : null;
  const hp2 = S.hp; now += 1.5; updateEnemies(0.016, st); r.doomHit = +((hp2 - S.hp)/stat().maxhp).toFixed(3);
  buildLoc("world"); const C2 = wtBoss(); C2.chase = true; C2.hp = C2.max*0.45; put(C2.x + 300, C2.y); S.hp = stat().maxhp;
  T += 7000; WT.doom.slot = -1; updateEnemies(0.016, st); const hp3 = S.hp; p.x += 200; now += 1.5; updateEnemies(0.016, st); r.doomDodged = hp3 - S.hp;
  C2.hp = C2.max*0.8; WT.doom.list = []; WT.doom.slot = -1; T += 7000; updateEnemies(0.016, st); r.doomHealthy = WT.doom.list.length;
  Date.now = _now;
  // сервер: добыча мне (я в share), таймер возвращения; не в share — без опыта; вернулся — на месте
  buildLoc("world"); NET.id = 7; NET.on = true; NET.mw = 2; NET.ws = {readyState:1, send:() => {}};
  S.exp = 0; S.level = 45;
  onNetExtra({t:"mdie", loc:"world", i:WT.IDX, by:9, iid:123, share:[9, 7], back:3600});
  r.share = {dead:wtBoss().dead, exp:S.exp, left:Math.round((WT.until - Date.now())/1000)};
  onNetExtra({t:"wt", until:0}); r.back = {dead:wtBoss().dead, hp:wtBoss().hp, home:wtBoss().x === WT.home.x};
  S.exp = 0; onNetExtra({t:"mdie", loc:"world", i:WT.IDX, by:9, iid:124, share:[9], back:4000}); r.noShare = {dead:wtBoss().dead, exp:S.exp};
  WT.until = Date.now() + 5000; buildLoc("world"); r.enterDead = wtBoss().dead;
  onNetExtra({t:"mstate", loc:"world", dead:[], hp:[], own:[]}); r.stateKeepsDead = wtBoss().dead;
  onNetExtra({t:"mstate", loc:"world", dead:[[WT.IDX, 1200]], hp:[], own:[]}); r.stateTimer = Math.round((WT.until - Date.now())/1000);
  r.status = [wtStatus(), (WT.until = 0, wtStatus())];
  r.left = [wtLeftText(3600*1000 + 12*60000), wtLeftText(47*60000), wtLeftText(9*60000 + 30000), wtLeftText(45000)];
  NET.on = false;
  const D = mobDrops("wt60", "world");
  r.drops = {scrap:D.scrap, ch:D.scrapChance, gear:[...new Set(D.gear.pool.map(id => ITEMS[id].lvl))].sort((a, b) => a - b), gems:D.gems.length};
  return JSON.stringify(r);
})()`);
process.stdout.write(out + "\n");
'''
        res = subprocess.run(["node", "-", str(ROOT / "game.html")], input=head + check, capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            raise AssertionError(res.stderr[-3000:])
        cls.r = json.loads(res.stdout.strip().splitlines()[-1])

    def test_server_mirrors(self):
        r, k = self.r, self.r["npc"]
        self.assertEqual(r["boss"]["idx"], worldthrone.IDX)                     # номер босса — как на сервере
        self.assertEqual(r["boss"]["spotMobs"], worldthrone.IDX)                # сразу после монстров спотов
        self.assertTrue(r["boss"]["last"])
        self.assertEqual(r["wt"], {"IDX": worldthrone.IDX, "HP": worldthrone.HP, "LVL": worldthrone.LVL, "TYPE": BOSS})
        self.assertEqual((k["hp"], k["lvl"]), (mobguard.mob_hp(BOSS), mobguard.mob_level(BOSS, "world")))
        self.assertEqual(k["exp"], progress.mob_exp(60, boss=True))
        self.assertEqual(ledger.mob_info(BOSS), (k["lvl"], tuple(k["scrap"]), k["loot"], k["boss"]))

    def test_stands_on_the_plaza(self):
        self.assertTrue(self.r["homeFree"] and self.r["onPlaza"])
        self.assertFalse(self.r["boss"]["dead"])

    def test_aggro_leash_and_no_stun(self):
        self.assertFalse(self.r["farChase"])
        self.assertTrue(self.r["nearChase"])
        self.assertFalse(self.r["leftChase"])
        self.assertEqual(self.r["stun"], 0)

    def test_scepter_slam(self):
        self.assertEqual(self.r["k0"], 0)                                       # за 2 с до удара круга нет
        self.assertTrue(self.r["armed"])
        self.assertEqual(self.r["k1"], round(1 - 0.5 / 1.2, 2))
        self.assertEqual(self.r["slam"], 0.25)
        self.assertTrue(self.r["flash"])
        self.assertEqual(self.r["dodged"], 0)
        self.assertEqual(self.r["rageSlot"], 1)

    def test_throne_judgement(self):
        self.assertEqual(self.r["doomAt"], [0, 0])                               # круг — под ногами
        self.assertEqual(self.r["doomHit"], 0.2)
        self.assertEqual(self.r["doomDodged"], 0)
        self.assertEqual(self.r["doomHealthy"], 0)                              # выше 50% прочности кары нет

    def test_loot_share_and_timer(self):
        r = self.r
        self.assertEqual(r["share"], {"dead": True, "exp": progress.mob_exp(60, boss=True) * 3, "left": 3600})
        self.assertEqual(r["back"], {"dead": False, "hp": worldthrone.HP, "home": True})
        self.assertEqual(r["noShare"], {"dead": True, "exp": 0})
        self.assertTrue(r["enterDead"] and r["stateKeepsDead"])
        self.assertEqual(r["stateTimer"], 1200)
        self.assertEqual(r["status"][1], "На троне")
        self.assertTrue(r["status"][0].startswith("Вернётся через "))
        self.assertEqual(r["left"], ["1 ч 12 мин", "47 мин", "9 мин 30 сек", "45 сек"])    # секунды — в последние 10 минут

    def test_drops_like_a_boss(self):
        d = self.r["drops"]
        self.assertEqual((d["scrap"], d["ch"], d["gear"], d["gems"]), ([610, 1240], 1, [40, 45], 5))     # 4 камня и Монета удачи


class PagesAndTexts(unittest.TestCase):
    def test_patch_notes(self):
        clean = re.sub(r"<!--.*?-->", "", PATCHES, flags=re.S)
        patches = re.findall(r'<section\b[^>]*\bdata-patch="([^"]+)"[^>]*data-title="([^"]+)"', clean)
        self.assertIn(("2026-10-18-4", "Железный Наместник"), patches)
        sec = PATCHES[PATCHES.index('id="p-1018-4"'):PATCHES.index("</section>", PATCHES.index('id="p-1018-4"'))]
        for s in ("60 уровня", "1–2 часа", "1% его прочности", "4–6 медных сфер", "35%", "Удар скипетром", "Кара трона", "500&nbsp;000"):
            self.assertIn(s, sec)

    def test_guide(self):
        self.assertIn("на троне — босс 60 ур.", GUIDE)

    @unittest.skipUnless(shutil.which("node"), "нет node")
    def test_translated(self):
        import test_i18n_content as tic
        strings = ["Железный Наместник", "На троне", "Вернётся через 1 ч 12 мин", "Вернётся через 47 мин", "Вернётся через 9 мин 30 сек", "Удар скипетром!", "Кара трона!",
                   "Железный Наместник · 60 ур.", "Трон Владыки · бить лучше группой: добыча и опыт — каждому, кто снёс 1% прочности",
                   "Вернуть босса", "Босс трона вернулся", "🏆 Железный Наместник повержен — добыча твоя",
                   worldthrone.killed_text(3, time.time() + 5040), f"⚔ {worldthrone.NAME} вернулся на Трон Владыки", "Уровень 60 · ярость"]
        res = tic.run_node(strings)
        for lang in tic.LANGS:
            russian = re.compile("[ыэъёЫЭЪЁ]" if lang == "uk" else "[А-Яа-яЁё]")
            with self.subTest(lang=lang):
                self.assertEqual([f"{s!r} → {t!r}" for s, t in zip(strings, res[lang]) if russian.search(t)], [])
        en = dict(zip(strings, res["en"]))
        self.assertEqual(en["Вернётся через 1 ч 12 мин"], "Returns in 1 h 12 min")
        self.assertEqual(en["Железный Наместник"], "Iron Viceroy")


if __name__ == "__main__":
    unittest.main()
