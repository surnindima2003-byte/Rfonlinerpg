"""Мир, шаг 4: АВТО держится своего спота (патч LIBERTY 17 октября «Мир»).

* В Мире АВТО бьёт только монстров в круге вокруг точки, где пилот включил АВТО или остановился; монстр, который
  сам бросился на пилота, — тоже цель, даже чуть за кругом. Цель, выбранную вручную, АВТО бьёт где угодно.
* Бить некого — АВТО подбирает добычу в круге и возвращается в центр. Упёрся в препятствие — круг встаёт на месте.
* Круг виден на земле, на мини-карте и во вкладке «Карта»; пока робот идёт под джойстиком, круг едет за ним.
* В других локациях АВТО — как раньше.

Поведение проверяется в настоящем коде игры (тот же запуск в node, что в tools/stat_caps.js): полный ход update().
Запуск: python -m unittest test_world_auto
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GAME = (ROOT / "game.html").read_text(encoding="utf-8")
PATCHES = (ROOT / "patches.html").read_text(encoding="utf-8")


@unittest.skipUnless(shutil.which("node"), "нет node")
class InGame(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        head = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8")
        head = head[:head.index("\n", head.index("const run = "))]
        check = r'''
const out = run(`(() => {
  const r = {}, toasts = []; toast = s => toasts.push(s);
  S.level = 50; S.cls = "guard"; S.skl = {}; S.pas = {}; S.up = {}; S.eq = {}; S.inv = []; S.store = []; paused = false; running = true; now = 100; vw = 390; vh = 844;
  window.NET_ADMIN = true; window.NET_GOD = true;
  const put = (x, y) => { p = {x, y, r:20, ang:0, aim:0, fireCd:0, dashCd:0, kitCd:0, dashT:0, dx:0, dy:0, inv:0, dead:false, deadT:0, flash:0, moving:false, target:null, atkT:0, stunT:0, vx:0, vy:0}; };
  const kill = e => { e.dead = true; e.respawn = 1e9; };                   // возрождение не мешает
  const tick = (n, dt) => { for(let i = 0; i < n; i++){ now += dt || 0.05; update(dt || 0.05); } };
  const fromC = o => Math.round(Math.hypot(o.x - WA.at.x, (o.y - WA.at.y)/WA.K));
  buildLoc("world");
  const tract = W_SPOTS.find(s => s.id === "tract"); put(tract.x, tract.y); S.hp = stat().maxhp;
  r.cfg = {R:WA.R, K:WA.K, OUT:WA.OUT};
  // включил АВТО — круг на месте робота; берёт ближнего монстра в круге
  setAuto(true);
  r.on = {at:[Math.round(WA.at.x - p.x), Math.round(WA.at.y - p.y)], land:WA.set === now, text:autoOnText()};
  tick(1); r.first = p.target ? {inside:waIn(p.target), d:fromC(p.target)} : null;
  // кроме одного монстра далеко (на другом споте) — никого: его АВТО не трогает
  for(const e of enemies) kill(e);
  const far = enemies.find(e => e.spot && e.spot.id === "glade"); far.dead = false; far.chase = false; clearTarget(); tick(2);
  r.farIgnored = !p.target;
  // напал сам и стоит чуть за кругом — цель; за кругом дальше WA.OUT — нет
  const atk = enemies.find(e => e.spot && e.spot.id === "tract"); atk.dead = false; atk.hp = atk.max = 1e9;
  atk.x = atk.hx = WA.at.x + WA.R + 120; atk.y = atk.hy = WA.at.y; atk.chase = true;
  r.chaserNear = autoPick(320) === atk;
  atk.x = atk.hx = WA.at.x + WA.R + WA.OUT + 40; r.chaserFar = autoPick(320) === atk;
  atk.chase = false; atk.x = atk.hx = WA.at.x + WA.R + 120; r.idleOutside = autoPick(320) === atk;
  kill(atk); clearTarget();
  // бить некого — возвращается в центр
  p.x = WA.at.x + 160; p.y = WA.at.y; tick(50); r.back = fromC(p);
  // добыча в круге — забирает, за кругом — нет; вещь при полной сумке — не идёт за ней
  drops.push({x:WA.at.x - 170, y:WA.at.y + 30, kind:"scrap", amt:7, t:0}); const s0 = S.scrap; tick(90); r.loot = S.scrap - s0; r.backAfterLoot = fromC(p);
  drops.push({x:WA.at.x + WA.R + 160, y:WA.at.y, kind:"scrap", amt:9, t:0}); const s1 = S.scrap; tick(60); r.lootOutside = S.scrap - s1; r.stayed = fromC(p); drops.length = 0;
  for(let i = 0; i < INV_MAX; i++) S.inv.push({id:"g_armor_1", n:1, g:0, e:0});
  drops.push({x:WA.at.x - 170, y:WA.at.y, kind:"g_legs_1", amt:1, t:0, g:0}); tick(40); r.fullBag = {walked:fromC(p) > 60, left:drops.length}; drops.length = 0; S.inv = [];
  tick(40);
  // своя цель ушла из круга — бросает; цель, взятую вручную, бьёт и за кругом
  const a = enemies.find(e => e.spot && e.spot.id === "tract"); a.dead = false; a.chase = false; a.hp = a.max = 1e9;
  a.x = a.hx = WA.at.x + 100; a.y = a.hy = WA.at.y; clearTarget(); tick(1); r.autoTarget = lockedEnemy() === a && tgt === WA.auto;
  a.x = a.hx = WA.at.x + WA.R + 200; tick(1); r.droppedOutside = lockedEnemy() === null;
  tgt = {kind:"enemy", e:a}; a.x = a.hx = WA.at.x + WA.R + 160; tick(1); r.manualKept = lockedEnemy() === a; tick(30); r.walksToManual = fromC(p) > 60;
  kill(a); clearTarget(); tick(80); r.backAfterManual = fromC(p);
  // джойстик: круг едет за роботом и встаёт там, где отпустил
  const c0 = {x:WA.at.x, y:WA.at.y};
  stick.id = 1; stick.x = 1; stick.y = 0; tick(20); r.follow = Math.round(Math.hypot(WA.at.x - p.x, WA.at.y - p.y)); r.moved = Math.round(WA.at.x - c0.x);
  stick.id = null; stick.x = 0; stick.y = 0; tick(1); r.placed = {d:Math.round(Math.hypot(WA.at.x - p.x, WA.at.y - p.y)), land:WA.set > 0};
  // под открытым окном джойстик не ведёт робота — и круг не сдвигается
  paused = true; stick.id = 1; stick.x = 1; const c1 = WA.at.x; tick(10); r.pausedStill = WA.at.x === c1; paused = false; stick.id = null; stick.x = 0; tick(1);
  // упёрся по пути в центр (камень, дерево) — круг встаёт на месте робота
  p.x = WA.at.x + 150; const _po = pushOut, hold = {x:p.x, y:p.y}; pushOut = (o, rr2) => { if(o === p){ o.x = hold.x; o.y = hold.y; } else _po(o, rr2); };
  tick(40); pushOut = _po; r.stuckMoved = Math.round(Math.hypot(WA.at.x - hold.x, WA.at.y - hold.y));
  // круг на земле, на мини-карте и во вкладке «Карта»
  const calls = []; const _el = ctx.ellipse; ctx.ellipse = (...a2) => { calls.push(a2.map(v => Math.round(v))); };
  WA.set = now - 1; waDraw(1.5); r.draw = calls.find(c => c[2] === WA.R && c[3] === Math.round(WA.R*WA.K)) ? "ring" : JSON.stringify(calls.slice(0, 3));
  ctx.ellipse = _el;
  let mErr = null; try{ drawMinimap(); }catch(e){ mErr = String(e); } r.mini = mErr;
  // выключил — круга нет; в других локациях АВТО прежний
  setAuto(false); r.off = WA.at; setAuto(true);
  buildLoc("scrapfields"); put(1100, 1900); tick(1); r.field = {at:WA.at, text:autoOnText()};
  const near = enemies[0]; for(const e of enemies) kill(e); near.dead = false; near.x = p.x + 200; near.y = p.y; r.fieldPick = autoPick(320) === near;
  r.toasts = toasts;
  return JSON.stringify(r);
})()`);
process.stdout.write(out + "\n");
'''
        res = subprocess.run(["node", "-", str(ROOT / "game.html")], input=head + check, capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            raise AssertionError(res.stderr[-3000:])
        cls.r = json.loads(res.stdout.strip().splitlines()[-1])

    def test_circle_size(self):
        # круг — эллипс, как споты на земле; на экране телефона (390 px) виден почти целиком
        self.assertEqual(self.r["cfg"], {"R": 220, "K": 0.86, "OUT": 200})

    def test_turned_on_here(self):
        on = self.r["on"]
        self.assertEqual(on["at"], [0, 0])
        self.assertTrue(on["land"])                                            # круг «ложится» на землю
        self.assertEqual(on["text"], "АВТО включено: бьёт монстров в круге на земле и возвращается в его центр")
        self.assertTrue(self.r["first"]["inside"])

    def test_only_inside_the_circle(self):
        self.assertTrue(self.r["farIgnored"])                                   # монстр соседнего спота
        self.assertTrue(self.r["chaserNear"])                                   # сам бросился на пилота — бьёт и за краем
        self.assertFalse(self.r["chaserFar"])                                   # но не дальше WA.OUT за кругом
        self.assertFalse(self.r["idleOutside"])                                 # стоит за кругом и не нападает — не трогает

    def test_returns_to_the_center(self):
        self.assertLessEqual(self.r["back"], 22)
        self.assertLessEqual(self.r["backAfterManual"], 22)

    def test_picks_up_loot_inside_only(self):
        self.assertEqual(self.r["loot"], 7)
        self.assertLessEqual(self.r["backAfterLoot"], 22)                      # забрал — и назад в центр
        self.assertEqual(self.r["lootOutside"], 0)
        self.assertLessEqual(self.r["stayed"], 22)
        self.assertEqual(self.r["fullBag"], {"walked": False, "left": 1})      # сумка полна — за вещью не ходит

    def test_own_target_vs_manual(self):
        self.assertTrue(self.r["autoTarget"])
        self.assertTrue(self.r["droppedOutside"])                               # свою цель за кругом бросает
        self.assertTrue(self.r["manualKept"])                                   # выбранную вручную — бьёт
        self.assertTrue(self.r["walksToManual"])

    def test_joystick_moves_the_circle(self):
        self.assertLessEqual(self.r["follow"], 8)                               # пока идёт — круг едет за ним
        self.assertGreater(self.r["moved"], 60)
        self.assertEqual(self.r["placed"], {"d": 0, "land": True})              # отпустил — круг встал здесь
        self.assertTrue(self.r["pausedStill"])

    def test_stuck_on_the_way_back(self):
        self.assertLessEqual(self.r["stuckMoved"], 2)

    def test_drawn(self):
        self.assertEqual(self.r["draw"], "ring")
        self.assertIsNone(self.r["mini"])

    def test_off_and_other_locations(self):
        self.assertIsNone(self.r["off"])
        self.assertEqual(self.r["field"], {"at": None, "text": "АВТО включено"})
        self.assertTrue(self.r["fieldPick"])                                    # в поле — ближайший, как раньше


class Wiring(unittest.TestCase):
    def test_hooks(self):
        self.assertIn('setAuto(!auto); toast(auto ? autoOnText() : "АВТО выключено");', GAME)
        self.assertIn("if(tgt && tgt.kind === \"enemy\" && WA.pickT === now && tgt.e === WA.picked) WA.auto = tgt;", GAME)
        self.assertIn("wDrawLive = function(cx, cy, t){ _dlA(cx, cy, t); waDraw(t); };", GAME)          # круг — на земле, под пилотами
        self.assertIn("ctx.ellipse(X(WA.at.x), Y(WA.at.y), WA.R*s, WA.R*WA.K*s, 0, 0, Math.PI*2)", GAME)  # мини-карта
        self.assertIn("g.ellipse(X(WA.at.x), Y(WA.at.y), WA.R*k, WA.R*WA.K*k, 0, 0, Math.PI*2)", GAME)    # вкладка «Карта»
        self.assertIn('<span><i class="wa-lg"></i>Круг АВТО</span>', GAME)

    @unittest.skipUnless(shutil.which("node"), "нет node")
    def test_translated(self):
        import test_i18n_content as tic
        strings = ["АВТО включено: бьёт монстров в круге на земле и возвращается в его центр", "АВТО включено", "АВТО выключено", "Круг АВТО",
                   "🔒 АВТО открывается с VIP 2 — смотри вкладку VIP в меню"]
        res = tic.run_node(strings)
        self.assertEqual(res["bad"], [])
        for lang in tic.LANGS:
            russian = re.compile("[ыэъёЫЭЪЁ]" if lang == "uk" else "[А-Яа-яЁё]")
            with self.subTest(lang=lang):
                self.assertEqual([f"{s!r} → {t!r}" for s, t in zip(strings, res[lang]) if russian.search(t)], [])
        self.assertEqual(res["en"][1:3], ["AUTO on", "AUTO off"])


class Pages(unittest.TestCase):
    def test_patch_notes(self):
        clean = re.sub(r"<!--.*?-->", "", PATCHES, flags=re.S)
        patches = re.findall(r'<section\b[^>]*\bdata-patch="([^"]+)"[^>]*data-title="([^"]+)"', clean)
        self.assertEqual(patches[0], ("2026-10-18-6", "АВТО держится спота"))           # самый свежий — первым
        self.assertIn('<li><a href="#p-1018-6"><time datetime="2026-10-18">18 октября 2026</time><span>АВТО держится спота</span></a></li>', PATCHES)
        self.assertIn("<h1>АВТО держится спота</h1>", PATCHES)
        hero = PATCHES[PATCHES.index('<div class="hero">'):PATCHES.index("</nav>", PATCHES.index('<div class="hero">'))]
        self.assertIn('<div class="wmap wa-plan"><svg viewBox="0 0 720 280" role="img" aria-label="Схема:', hero)   # схема круга
        for s in ("В круге — АВТО бьёт", "За кругом — не трогает", "Добыча в круге", "Назад в центр"):
            self.assertIn(s, hero)
        sec = PATCHES[PATCHES.index('id="p-1018-6"'):PATCHES.index("</section>", PATCHES.index('id="p-1018-6"'))]
        for s in ("Бьёт только внутри", "Подбирает добычу и возвращается", "Как сдвинуть круг", "отпускаете джойстик", "«ЦЕЛЬ»",
                  "мини-карте", "В других локациях АВТО работает как раньше"):
            self.assertIn(s, sec)


if __name__ == "__main__":
    unittest.main()
