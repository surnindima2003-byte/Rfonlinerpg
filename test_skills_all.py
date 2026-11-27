"""Все 28 умений 7 классов: каждое срабатывает без ошибок на 1 и 10 уровне, атакующие наносят урон
(и на 10 уровне больше, чем на 1), перезарядка ставится и с уровнем не растёт, баффы включаются.
Проверка в настоящем коде игры (тот же запуск в node, что tools/stat_caps.js).
Запуск: python -m unittest test_skills_all
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DAMAGE = {"strike", "aoe", "leap", "volley", "burst", "phase", "siren", "gravity", "chain", "siphon", "mend"}
BUFFS = {"buff": None, "shieldcast": "shield"}


@unittest.skipUnless(shutil.which("node"), "нет node")
class AllSkills(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        head = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8")
        head = head[:head.index("\n", head.index("const run = "))]
        js = head + r'''
process.stdout.write(run(`(() => { const out = [];
 for(const cls of Object.keys(CLASSES)){ for(const lvSk of [1, 10]){ for(let i = 0; i < 4; i++){
  S.level = 40; S.cls = cls; S.skl = {}; for(const s of CLASSES[cls].skills) S.skl[s.id] = lvSk; S.pas = {}; S.up = {};
  paused = false; running = true; now = 100; if(typeof NC !== "undefined") NC.rune = null;
  travelRaw("scrapfields"); buildLoc("scrapfields"); locKey = "scrapfields";
  const e = enemies[0]; for(const o of enemies) if(o !== e){ o.dead = true; o.respawn = Infinity; }
  e.dead = false; e.hp = 1e9; e.max = 1e9; e.stun = 0; e.chase = false;
  p = {x:e.x + 90, y:e.y, r:20, ang:Math.PI, aim:Math.PI, fireCd:0, dashCd:0, kitCd:0, dashT:0, dx:0, dy:0, inv:0, dead:false, deadT:0, flash:0, moving:false, target:null};
  S.hp = stat().maxhp * 0.5; for(let k = 0; k < 4; k++) CB.cd[k] = 0; CB.buffs = {}; tgt = {kind:"enemy", e}; let err = null;
  const hp0 = e.hp;
  try{ useSkill(i); for(let k = 0; k < 40; k++){ now += 0.05; update(0.05); } }catch(x){ err = String(x && x.stack || x).split("\\n").slice(0, 2).join(" "); }
  const sk = CLASSES[cls].skills[i];
  out.push({cls, i, id:sk.id, type:sk.type, lv:lvSk, dmg:Math.round(hp0 - e.hp), cd:CB.cd[i] || 0, buffs:Object.keys(CB.buffs), err});
 }}}
 return JSON.stringify(out); })()`));
'''
        res = subprocess.run(["node", "-", str(ROOT / "game.html")], input=js, capture_output=True, text=True, timeout=300)
        if res.returncode:
            raise AssertionError(res.stderr[-2000:])
        cls.rows = json.loads(res.stdout)
        cls.by = {(r["id"], r["lv"]): r for r in cls.rows}

    def test_28_skills(self):
        self.assertEqual(len({r["id"] for r in self.rows}), 28)

    def test_no_errors(self):
        self.assertEqual([(r["id"], r["lv"], r["err"]) for r in self.rows if r["err"]], [])

    def test_damage_and_growth(self):
        for r in self.rows:
            if r["type"] in DAMAGE and r["lv"] == 1 and r["id"] != "mend":       # у «Ремонтного луча» растёт лечение, вспышка — постоянная
                hi = self.by[(r["id"], 10)]
                self.assertGreater(r["dmg"], 0, r["id"])
                self.assertGreater(hi["dmg"], r["dmg"], r["id"])

    def test_cooldowns(self):
        for r in self.rows:
            if r["lv"] == 1 and r["type"] != "recall":                     # руна возврата: первое нажатие — 0,5 с
                hi = self.by[(r["id"], 10)]
                self.assertGreater(r["cd"], 0, r["id"])
                self.assertLessEqual(hi["cd"], r["cd"], r["id"])

    def test_buffs_turn_on(self):
        for r in self.rows:
            if r["type"] in BUFFS:
                self.assertTrue(r["buffs"], r["id"])


if __name__ == "__main__":
    unittest.main()
