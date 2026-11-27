"""Бой с мобами: АВТО в открытых полях сам едет к врагу в пределах экрана, а в подземельях и Мире — нет.

Раньше после зачистки ближних мобов (радиус ~320) робот с АВТО в поле просто стоял. Теперь в полях
(Ржавые поля, Руины реактора, Железный каньон) он ищет врага до AUTO_ROAM и подъезжает. В подземельях
(комнаты, проходы) и в Мире (круг АВТО у своего спота) поведение прежнее.
Запуск: python -m unittest test_combat
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(shutil.which("node"), "нет node")
class AutoRoam(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        head = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8")
        head = head[:head.index("\n", head.index("const run = "))]
        js = head + r'''
process.stdout.write(run(`(() => {
  const out = {};
  for(const loc of ["scrapfields", "sector1", "world"]){
    S.level = 45; S.cls = "guard"; S.skl = {}; S.pas = {}; S.up = {}; S.vipSpent = 200; paused = false; running = true; now = 100;
    travelRaw(loc); const sp = buildLoc(loc); locKey = loc;
    const e = enemies.find(x => !x.dead);
    for(const o of enemies) if(o !== e){ o.dead = true; o.respawn = Infinity; }     // одна цель на карте
    e.chase = false; e.hx = e.x; e.hy = e.y;
    p = {x:e.x + (e.x > W/2 ? -450 : 450), y:e.y, r:20, ang:0, aim:0, fireCd:0, dashCd:0, kitCd:0, dashT:0, dx:0, dy:0, inv:0, dead:false, deadT:0, flash:0, moving:false, target:null};
    pushOut(p, p.r);
    const d0 = Math.hypot(p.x - e.x, p.y - e.y);
    S.hp = stat().maxhp; auto = true; clearTarget && clearTarget();
    for(let i = 0; i < 40; i++){ now += 0.05; update(0.05); }
    out[loc] = {d0:Math.round(d0), d1:Math.round(Math.hypot(p.x - e.x, p.y - e.y)), tgt:!!(tgt && tgt.e === e)};
  }
  return JSON.stringify(out);
})()`));
'''
        res = subprocess.run(["node", "-", str(ROOT / "game.html")], input=js, capture_output=True, text=True, timeout=300)
        if res.returncode:
            raise AssertionError(res.stderr[-2000:])
        cls.r = json.loads(res.stdout)

    def test_field_goes_to_far_enemy(self):
        r = self.r["scrapfields"]
        self.assertGreater(r["d0"], 330)
        self.assertTrue(r["tgt"])
        self.assertLess(r["d1"], r["d0"] - 100)

    def test_dungeon_and_world_stay(self):
        for loc in ("sector1", "world"):
            self.assertFalse(self.r[loc]["tgt"], loc)


if __name__ == "__main__":
    unittest.main()
