"""Руны, дроны, артефакты, крылья и плащи: каждый бонус из описания действительно меняет характеристики в stat().

Если добавить предмету бонус, который stat() не складывает (например, «rate» дрону), игрок увидит его в описании,
но не получит. Код игры запускается в node через ту же «песочницу», что tools/stat_caps.js.
Запуск: python -m unittest test_support_items
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent

JS = r"""
const fs = require("fs"), path = require("path");
const dir = process.argv[2];
const h = fs.readFileSync(path.join(dir, "stat_caps.js"), "utf8");
const head = h.slice(0, h.indexOf("\n", h.indexOf("const run = ")));
new Function("require", "process", "module", "__dirname", head + `
process.stdout.write(run(\`(() => {
  S.cls = "guard"; S.level = 50; S.max_hp = 540; S.attack = 104; S.defense = 51; S.eq = {}; S.pas = {}; S.up = {}; S.gemCx = {};
  const K = {atk:"atk", hp:"maxhp", def:"def", crit:"crit", spd:"spd", cpow:"critPow", rate:"rate", regen:"regen", xp:"xpUp", scrap:"loot"};
  const bad = [], n = {};
  for(const [slot, T] of [["drone", DRONES], ["art", ARTS], ["wing", WINGS], ["cloak", CLOAKS]]){
    n[slot] = 0;
    for(const id of Object.keys(T)){
      n[slot]++;
      S.inv = [{id, n:1}]; S.drone = S.art = S.wing = S.cloak = ""; S[slot] = id;
      const on = stat(); S[slot] = ""; const off = stat();
      for(const k of Object.keys(T[id].b)) if(!K[k] || on[K[k]] === off[K[k]]) bad.push(id + ":" + k);
    }
  }
  S.inv = []; n.rune = 0;
  for(const id of Object.keys(RUNES)){
    n.rune++;
    S.eq = {weapon:{id:"g_hammer_8", n:1, g:0, e:0, r:[id]}}; const on = stat();
    S.eq = {weapon:{id:"g_hammer_8", n:1, g:0, e:0}}; const off = stat();
    for(const k of Object.keys(RUNES[id].b)) if(!K[k] || on[K[k]] === off[K[k]]) bad.push(id + ":" + k);
  }
  // четвёртая одинаковая руна бонуса не даёт (RUNE_STACK)
  const eq4 = n => Object.fromEntries(["weapon", "head", "armor", "module"].slice(0, n).map(sl => [sl, {id:sl === "weapon" ? "g_hammer_8" : "g_" + (sl === "head" ? "sensor" : sl) + "_8", n:1, g:0, e:0, r:["r_atk"]}]));
  S.eq = eq4(3); const a3 = runeTotals().atk; S.eq = eq4(4); const a4 = runeTotals().atk;
  return JSON.stringify({bad, n, stack:[a3, a4, RUNES.r_atk.b.atk*RUNE_STACK]});
})()\`));`)(require, {...process, argv:[process.argv[0], "x", path.join(dir, "..", "game.html")]}, {exports:{}}, dir);
"""


class SupportItems(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("нет node")
        out = subprocess.run([node, "-", str(ROOT / "tools")], input=JS, capture_output=True, text=True, timeout=120)
        if out.returncode:
            raise AssertionError(out.stderr[-2000:])
        cls.r = json.loads(out.stdout)

    def test_every_bonus_works(self):
        self.assertEqual(self.r["bad"], [])

    def test_all_kinds_checked(self):
        for k in ("drone", "art", "wing", "cloak", "rune"):
            self.assertGreaterEqual(self.r["n"][k], 8, k)

    def test_rune_stack_limit(self):
        a3, a4, top = self.r["stack"]
        self.assertAlmostEqual(a3, top)
        self.assertAlmostEqual(a4, top)                   # 4-я одинаковая руна ничего не добавляет


if __name__ == "__main__":
    unittest.main()
