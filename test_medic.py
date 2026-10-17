"""Ремонтник научился бить (4 сезон LIBERTY: «Целитель научился бить»).

* Лечащие умения бьют монстров: вспышка Ремонтного луча вокруг цели (×1,5 атаки) и Рой нанитов —
  25% атаки в секунду монстрам внутри облака. Пилотов они не задевают: PvP-баланс Ремонтника прежний.
* Рой нанитов перезаряжается 16 сек и на 1 сек быстрее с каждым уровнем — 7 сек на 10 уровне.
* АВТО применяет лечащие умения сразу по откату (раньше — только при низкой прочности): в бою, когда монстр
  рядом, и всегда, когда кто-то ранен. Выбранный союзник вне досягаемости не засыпает экран подсказками.
* Целых (полная прочность и CP) лечение не тратит: запас лечения на сервере общий на всех целей Ремонтника.

Проверки идут в настоящем коде игры (тот же запуск в node, что в tools/stat_caps.js).
Запуск: python -m unittest test_medic
"""
import json
import math
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GAME = (ROOT / "game.html").read_text(encoding="utf-8")
PATCHES = (ROOT / "patches.html").read_text(encoding="utf-8")
GUIDE = (ROOT / "guide.html").read_text(encoding="utf-8")
I18N_TEST = (ROOT / "test_i18n_content.py").read_text(encoding="utf-8")


class Contract(unittest.TestCase):
    def test_heal_skills_do_not_hit_pilots(self):
        pvp = re.search(r"const PVP_SKILLS = \[([^\]]*)\]", GAME).group(1)
        for ty in ("mend", "nanofield"):
            self.assertNotIn(f'"{ty}"', pvp)                       # PvP-обёртка их не превращает в удар по пилоту
        medic = GAME[GAME.index("  medic:{name:\"Ремонтник\""):GAME.index("\n};", GAME.index("  medic:{name:\"Ремонтник\""))]
        for sid in ("mend", "nanoswarm"):
            line = re.search(r'\{id:"%s"[^\n]*' % sid, medic).group(0)
            self.assertNotIn("pvpAs", line)

    def test_auto_runs_under_window(self):
        self.assertIn("setInterval(() => autoTurn(() => medAuto()), 400);", GAME)

    def test_guide_and_patch_notes(self):
        self.assertTrue("заодно бьют монстров" in GUIDE and "Урон слабый" not in GUIDE)     # карточка класса в гайде
        clean = re.sub(r"<!--.*?-->", "", PATCHES, flags=re.S)
        first = re.search(r'<section\b[^>]*\bdata-patch="([^"]+)"[^>]*data-title="([^"]+)"', clean)
        self.assertEqual(first.groups(), ("2026-10-18-2", "Ремонтник научился бить"))


@unittest.skipUnless(shutil.which("node"), "нет node")
class InGame(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        head = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8")
        head = head[:head.index("\n", head.index("const run = "))]
        check = r'''
const out = run(`(() => {
  const r = {}, SENT = [], TOASTS = [];
  toast = m => TOASTS.push(String(m));
  NET.ws = {readyState:1, send: s => SENT.push(JSON.parse(s))}; NET.id = 7; NET.on = false;
  const C = CLASSES.medic, I = id => C.skills.findIndex(s => s.id === id);
  const iM = I("mend"), iN = I("nanoswarm"), iD = I("surge");
  const mob = (x, y, extra) => Object.assign({type:"war_walker", x, y, hp:1e9, max:1e9, dead:false}, extra || {});
  const lost = e => Math.round(1e9 - e.hp), cheals = () => SENT.filter(m => m.t === "cheal").map(m => m.to);
  const fired = before => C.skills.filter((s, i) => CB.cd[i] > before[i]).map(s => s.id);
  const reset = () => {
    S.cls = "medic"; S.level = 30; S.skl = {}; S.pas = {}; S.up = {}; paused = false; auto = false;
    locKey = "scrapfields"; now = 100; tgt = null; NET.party = null; NET.others = new Map();
    p = {x:500, y:500, r:14, dead:false, aim:0, ang:0, atkT:0, stunT:0, target:null};
    CB.cd = [0, 0, 0, 0]; CB.fx = []; CB.buffs = {}; MED.field = null; MED.pvpT = 0;
    S.hp = stat().maxhp; S.cp = stat().maxcp; SENT.length = 0; TOASTS.length = 0; enemies = [];
  };
  reset();
  const st = stat(); r.atk = st.atk*(1 + st.skillDmg);
  r.lv = {cd:[1,2,3,4,5,6,7,8,9,10].map(l => skillAt(C.skills[iN], l).cd), mend:[1, 10].map(l => skillAt(C.skills[iM], l)),
          swarm:[1, 10].map(l => skillAt(C.skills[iN], l))};
  r.text = [skillType(C.skills[iM]) + " · Ур. 1/10", skillText(skillAt(C.skills[iM], 1)), skillText(skillAt(C.skills[iN], 1)),
            ...skillDiff(skillAt(C.skills[iN], 1), skillAt(C.skills[iN], 2)).filter(t => /^(Перезарядка|Сила)/.test(t))];

  // вспышка луча вокруг себя: ближние монстры задеты, дальний — нет; целого себя не лечим
  enemies = [mob(560, 500), mob(500, 640), mob(800, 500)];
  useSkill(iM);
  r.selfBurst = {lost:enemies.map(lost), cd:CB.cd[iM], cheal:cheals(), rings:CB.fx.filter(f => f.type === "ring").map(f => f.r)};

  // вспышка — вокруг союзника, которого чинит луч, а не вокруг Ремонтника
  reset(); NET.party = {members:[{id:7}, {id:8}]};
  NET.others.set(8, {id:8, rx:800, ry:500, hp:50, mhp:100, cp:1000, mcp:1000, lvl:30}); tgt = {kind:"player", id:8};
  enemies = [mob(860, 500), mob(560, 500)];
  useSkill(iM);
  r.allyBurst = {lost:enemies.map(lost), cheal:cheals()};

  // союзник целый (прочность и CP полные): лечение не уходит, монстр всё равно получает урон
  reset(); NET.party = {members:[{id:7}, {id:8}]};
  NET.others.set(8, {id:8, rx:600, ry:500, hp:100, mhp:100, cp:1000, mcp:1000, lvl:30}); tgt = {kind:"player", id:8};
  enemies = [mob(640, 500)];
  useSkill(iM);
  r.fullAlly = {cheal:cheals(), lost:lost(enemies[0])};
  // у союзника не хватает CP — лечение нужно (оно восстанавливает и CP)
  reset(); NET.party = {members:[{id:7}, {id:8}]};
  NET.others.set(8, {id:8, rx:600, ry:500, hp:100, mhp:100, cp:300, mcp:1000, lvl:30}); tgt = {kind:"player", id:8};
  useSkill(iM);
  r.cpAlly = cheals();

  // себя: полная прочность, но CP не полные — лечим; раненый — лечим
  reset(); S.cp = 0; useSkill(iM); r.cpSelf = cheals();
  reset(); S.hp = Math.round(stat().maxhp/2); useSkill(iM); r.hurtSelf = cheals();

  // комната, куда пилот не зашёл: монстр не задет и подсказки «Зайди в комнату» нет
  reset(); enemies = [mob(560, 500, {room:{x:2000, y:2000, w:300, h:300}})];
  useSkill(iM);
  r.room = {lost:lost(enemies[0]), toasts:TOASTS.slice()};

  // выбран вражеский пилот рядом: лечащие умения его не бьют
  reset(); NET.others.set(9, {id:9, rx:560, ry:500, hp:100, mhp:100, cp:1000, mcp:1000, lvl:30}); tgt = {kind:"player", id:9};
  useSkill(iM); useSkill(iN);
  r.pvp = SENT.filter(m => m.t === "pvp").length;

  // Рой нанитов: раз в секунду 25% атаки монстрам в облаке, 6 сек, дальше облако гаснет
  reset(); enemies = [mob(560, 500), mob(500, 670), mob(800, 500)];
  useSkill(iN);
  const F = MED.field;
  r.field = {r:F.r, dmg:F.dmg, loc:F.loc, dur:+(F.until - now).toFixed(1), cd:CB.cd[iN]};
  let prev = 0, ticks = 0;
  for(let k = 0; k <= 40; k++){ medFieldTick(); const v = lost(enemies[0]); if(v > prev) ticks++; prev = v; now = 100 + (k + 1)*0.2; }
  r.dot = {ticks, total:lost(enemies[0]), edge:lost(enemies[1]), far:lost(enemies[2]), gone:MED.field === null};
  // переход в другую локацию гасит облако
  reset(); useSkill(iN); locKey = "reactor_ruins"; medFieldTick(); r.portal = MED.field === null;

  // АВТО: бой, монстр рядом, все целы — луч, потом рой, потом Разрядник (по одному за ход)
  reset(); auto = true; enemies = [mob(560, 500)]; p.target = enemies[0];
  r.autoFight = [];
  for(let k = 0; k < 4; k++){ const b = CB.cd.slice(); medAuto(); r.autoFight.push(fired(b).join() || "-"); }
  // АВТО: боя нет, все целы — ничего не тратим
  reset(); auto = true; medAuto(); r.autoIdle = fired([0, 0, 0, 0]);
  // АВТО: боя нет, но пилот ранен — луч
  reset(); auto = true; S.hp = Math.round(stat().maxhp*0.8); medAuto(); r.autoHurt = fired([0, 0, 0, 0]);
  // АВТО: бой, но монстр дальше вспышки и облака — сразу Разрядник
  reset(); auto = true; enemies = [mob(720, 500)]; p.target = enemies[0]; medAuto(); r.autoFar = fired([0, 0, 0, 0]);
  // АВТО: выбранный союзник далеко — без подсказок, лечение уходит раненому рядом (себе)
  reset(); auto = true; NET.party = {members:[{id:7}, {id:8}]};
  NET.others.set(8, {id:8, rx:2000, ry:500, hp:10, mhp:100, cp:0, mcp:1000, lvl:30}); tgt = {kind:"player", id:8};
  S.hp = Math.round(stat().maxhp*0.5);
  for(let k = 0; k < 3; k++) medAuto();
  r.autoFarAlly = {toasts:TOASTS.slice(), mend:CB.cd[iM] > 0, cheal:cheals()};
  // то же ручным нажатием — подсказка остаётся
  CB.cd = [0, 0, 0, 0]; auto = false; useSkill(iM); r.manualFarAlly = {toast:TOASTS.slice(-1)[0], mend:CB.cd[iM] > 0};
  return JSON.stringify(r);
})()`);
process.stdout.write(out + "\n");
'''
        res = subprocess.run(["node", "-", str(ROOT / "game.html")], input=head + check, capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            raise AssertionError(res.stderr[-2000:])
        cls.r = json.loads(res.stdout.strip().splitlines()[-1])

    def hit(self, k):
        return math.floor(self.r["atk"] * k + 0.5)                     # удар с округлением, как Math.round в hitEnemy

    def test_levels(self):
        self.assertEqual(self.r["lv"]["cd"], [16, 15, 14, 13, 12, 11, 10, 9, 8, 7])
        m1, m10 = self.r["lv"]["mend"]
        self.assertEqual((m1["mult"], m1["radius"], m1["cd"]), (1.5, 120, 7))
        self.assertEqual(m10["mult"], 1.5)                             # растёт лечение, урон вспышки — нет
        self.assertGreater(m10["v"], m1["v"])
        s1, s10 = self.r["lv"]["swarm"]
        self.assertEqual((s1["mult"], s10["mult"]), (0.25, 0.25))

    def test_texts_match_translation_check(self):
        self.assertIn("Сила 3,5% → 3,6%", self.r["text"])                  # раньше «4% → 4%»
        for s in self.r["text"]:
            self.assertIn(json.dumps(s, ensure_ascii=False), I18N_TEST)    # окно навыков проверяется на перевод

    def test_mend_burst(self):
        b = self.r["selfBurst"]
        self.assertEqual(b["lost"], [self.hit(1.5), self.hit(1.5), 0])
        self.assertEqual(b["cd"], 7)
        self.assertEqual(b["cheal"], [])                               # целого себя не лечим
        self.assertIn(120, b["rings"])
        self.assertEqual(self.r["allyBurst"], {"lost": [self.hit(1.5), 0], "cheal": [8]})
        self.assertEqual(self.r["fullAlly"], {"cheal": [], "lost": self.hit(1.5)})
        self.assertEqual(self.r["cpAlly"], [8])
        self.assertEqual(self.r["cpSelf"], [7])
        self.assertEqual(self.r["hurtSelf"], [7])

    def test_rooms_and_pilots(self):
        self.assertEqual(self.r["room"], {"lost": 0, "toasts": []})
        self.assertEqual(self.r["pvp"], 0)

    def test_nanoswarm_dot(self):
        self.assertEqual(self.r["field"], {"r": 150, "dmg": 0.25, "loc": "scrapfields", "dur": 6, "cd": 16})
        d = self.r["dot"]
        self.assertEqual(d["ticks"], 6)                                # раз в секунду, 6 сек
        self.assertEqual(d["total"], 6 * self.hit(0.25))
        self.assertEqual(d["edge"], 6 * self.hit(0.25))                # край облака с учётом размера монстра
        self.assertEqual(d["far"], 0)
        self.assertTrue(d["gone"])
        self.assertTrue(self.r["portal"])

    def test_auto(self):
        self.assertEqual(self.r["autoFight"], ["mend", "nanoswarm", "surge", "-"])
        self.assertEqual(self.r["autoIdle"], [])
        self.assertEqual(self.r["autoHurt"], ["mend"])
        self.assertEqual(self.r["autoFar"], ["surge"])
        far = self.r["autoFarAlly"]
        self.assertEqual(far["toasts"], [])
        self.assertTrue(far["mend"])
        self.assertIn(7, far["cheal"])
        self.assertEqual(self.r["manualFarAlly"], {"toast": "Союзник далеко: подойди ближе", "mend": False})


if __name__ == "__main__":
    unittest.main()
