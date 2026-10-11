"""Баланс классов: числа, которые сервер повторяет за game.html, и разумность самих чисел.

Сервер не верит телефону, поэтому держит у себя копии чисел баланса: множитель удара класса по пилотам (PVP_HIT_K),
наибольшее оглушение и замедление умением (PVP_SKILL_STUN, PVP_SKILL_SLOW), щиты умений на себя (SELF_SHIELD)
и долю брони в PvP (pvpguard.DEF_K). Если поменять баланс только в game.html, сервер начнёт срезать честные удары
и щиты или пропускать лишнее — эти проверки ловят расхождение. Числа игры берутся из game.html через node.
Запуск: python -m unittest test_class_balance
"""
import ast
import json
import math
import re
import shutil
import subprocess
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
if "metrics" not in sys.modules:
    try:
        import metrics  # noqa: F401
    except Exception:
        m = types.ModuleType("metrics"); m.inc = lambda *a, **k: None; m.gauge = lambda *a, **k: None
        sys.modules["metrics"] = m
import pvpguard  # noqa: E402

GAME = (ROOT / "game.html").read_text(encoding="utf-8")
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")
CDR_PASSIVES = ("cold", "cyclotron")          # пассивки, которые stat() складывает в cdr (перезарядка умений)


def block(start, end="\n};"):
    a = GAME.index(start)
    return GAME[a:GAME.index(end, a) + len(end)]


def line(start):
    a = GAME.index(start)
    return GAME[a:GAME.index("\n", a)]


def server_const(name):
    """Значение константы из webserver.py без импорта (ему нужен aiohttp)."""
    for node in ast.parse(WEB).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"в webserver.py нет {name}")


def game_data():
    js = "\n".join([
        block("const CLASSES = {"), line("const NO_CLASS = "), block("const PASSIVES = [", "\n];"), line("const PAS_MAX = "),
        block("const UNIQ_SETS = {"), line("const SK_GROW = "), block("function skillAt(s, lv){", "\n}"),
        line("const PVP_DEF_K = "), line("const pvpDefMult = "),
        """
const out = {nopk: NO_CLASS.pk, pasMax: PAS_MAX, skillMax: SKILL_MAX, classes: {}, passives: {}, defMult: []};
for (const [id, C] of Object.entries(CLASSES)) {
  out.classes[id] = {pk: C.pk, hp: C.hp, atk: C.atk, def: C.def, rate: C.rate, range: C.range, melee: C.melee,
    uniqCdr: (UNIQ_SETS[id] && UNIQ_SETS[id].bonus.cdr) || 0,
    skills: C.skills.map(s => { const top = skillAt(s, SKILL_MAX);
      return {id: s.id, type: s.type, name: s.name, desc: s.desc, lore: s.lore, cd: s.cd, cdTop: top.cd, stun: s.stun || 0,
              pstun: s.pstun || 0, slow: s.slow || 0, slowDur: s.slowDur || 0, pshield: s.pshield || 0, durTop: top.dur || 0}; })};
}
for (const p of PASSIVES) out.passives[p.id] = {cls: p.cls, per: p.per};
for (const lvl of [1, 10, 30, 50]) for (const def of [0, 100, 1000, 5000]) out.defMult.push([def, lvl, pvpDefMult(def, lvl)]);
process.stdout.write(JSON.stringify(out));
"""])
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("нет node")
    res = subprocess.run([node, "-"], input=js, capture_output=True, text=True, timeout=120)
    if res.returncode:
        raise AssertionError(res.stderr[-2000:])
    return json.loads(res.stdout)


class ServerMirrorsGame(unittest.TestCase):
    """Копии чисел баланса на сервере совпадают с game.html."""
    @classmethod
    def setUpClass(cls):
        cls.g = game_data()
        cls.C = cls.g["classes"]

    def test_pvp_hit_k(self):
        self.assertEqual(server_const("PVP_HIT_K"), {c: v["pk"] for c, v in self.C.items()})
        self.assertEqual(self.g["nopk"], 1)                                   # без класса — как у сервера по умолчанию
        self.assertIn('PVP_HIT_K.get(cls, 1.0)', WEB)

    def test_hit_cap_formula_is_the_same(self):
        self.assertIn('cap = (40 + info["lvl"] * 8) * PVP_HIT_K.get(cls, 1.0) * (4 if skill else 1)', WEB)
        self.assertIn("const pvpHitCap = skill => (40 + 8*(S.level || 1))*(classParams().pk || 1)*(skill ? 4 : 1);", GAME)
        self.assertIn("const pvpDmgK = () => Math.sqrt(classParams().pk || 1);", GAME)

    def test_skill_stun_on_pilots(self):
        caps = server_const("PVP_SKILL_STUN")
        need = {c: max(s["pstun"] for s in v["skills"]) for c, v in self.C.items()}
        self.assertEqual(set(caps), {c for c, x in need.items() if x > 0})    # у остальных классов сервер не пропускает
        for c, cap in caps.items():
            with self.subTest(cls=c):
                self.assertGreaterEqual(cap, need[c])                         # честное оглушение не срезается
                self.assertLessEqual(cap, need[c] + 0.25)                     # и предел не шире умения
        self.assertGreaterEqual(1.2, max(caps.values()))                      # телефон жертвы режет до 1,2 сек
        self.assertIn("const stn = Math.min(1.2, +d.stn || 0);", GAME)

    def test_skill_slow_on_pilots(self):
        caps = server_const("PVP_SKILL_SLOW")
        need = {c: max(s["slow"] for s in v["skills"]) for c, v in self.C.items()}
        self.assertEqual(set(caps), {c for c, x in need.items() if x > 0})
        for c, cap in caps.items():
            with self.subTest(cls=c):
                self.assertGreaterEqual(cap, need[c])
                self.assertLessEqual(cap, need[c] + 0.1)
        longest = max(s["slowDur"] or 3 for v in self.C.values() for s in v["skills"] if s["slow"])
        self.assertIn('hit["sld"] = round(max(0.5, min(sld, 3.5)), 1)', WEB)
        self.assertGreaterEqual(3.5, longest)
        self.assertIn("const sls = Math.min(0.6, +d.sls || 0);", GAME)
        self.assertGreaterEqual(0.6, max(caps.values()))

    def test_self_shields(self):
        """Щит умения на себя: сила как в игре, держится не меньше умения и не отвергает честное повторное применение."""
        caps = server_const("SELF_SHIELD")
        own = {c: [s for s in v["skills"] if s["pshield"]] for c, v in self.C.items()}
        self.assertEqual(set(caps), {c for c, sk in own.items() if sk})
        for c, (v_max, dur_max, every) in caps.items():
            with self.subTest(cls=c):
                cdr = min(0.5, sum(p["per"] / 100 * self.g["pasMax"] for pid, p in self.g["passives"].items()
                                   if pid in CDR_PASSIVES and p["cls"] in (None, c)) + self.C[c]["uniqCdr"])
                for s in own[c]:
                    self.assertEqual(v_max, s["pshield"])
                    self.assertGreaterEqual(dur_max, s["durTop"])                # на 10 уровне умения щит не обрывается раньше
                    self.assertLessEqual(dur_max, s["durTop"] + 1)
                    fastest = s["cdTop"] * (1 - cdr)                              # перезарядка при всех сокращениях
                    self.assertLessEqual(every, fastest - 0.5, f"{s['id']}: честный повтор через {fastest:.2f} с")
                    self.assertGreaterEqual(every, fastest * 0.75)               # но и не спам

    def test_armor_share_is_the_same(self):
        self.assertEqual(pvpguard.DEF_K, int(re.search(r"const PVP_DEF_K = (\d+);", GAME).group(1)))
        for df, lvl, mult in self.g["defMult"]:
            self.assertAlmostEqual(pvpguard.def_mult(df, lvl), mult, places=9)


class ClassNumbersAreSane(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = game_data()
        cls.C = cls.g["classes"]

    def test_pvp_multipliers_only_redistribute(self):
        """pk перераспределяет урон между классами, а не поднимает общий уровень: среднее геометрическое ≈ 1."""
        geo = math.exp(sum(math.log(v["pk"]) for v in self.C.values()) / len(self.C))
        self.assertAlmostEqual(geo, 1.0, delta=0.03)
        for c, v in self.C.items():
            self.assertTrue(0.6 <= v["pk"] <= 1.6, c)

    def test_roles(self):
        tank = max(self.C, key=lambda c: self.C[c]["hp"] * self.C[c]["def"])
        self.assertEqual(tank, "guard")                                         # танк — самый живучий
        for c in ("sniper", "techno", "medic"):
            self.assertFalse(self.C[c]["melee"], c)
            self.assertGreaterEqual(self.C[c]["range"], 200, c)
        for c in ("guard", "reaper", "ghost", "glyph"):
            self.assertTrue(self.C[c]["melee"], c)
        for c, v in self.C.items():
            for k in ("hp", "atk", "def"):
                self.assertTrue(0.8 <= v[k] <= 1.5, f"{c}.{k} = {v[k]}")

    def test_pilots_are_stunned_shorter_than_mobs(self):
        for v in self.C.values():
            for s in v["skills"]:
                if s["pstun"]:
                    self.assertLess(s["pstun"], s["stun"], s["id"])
                    self.assertLessEqual(s["pstun"], 1.0, s["id"])               # контроль в PvP короткий

    def test_every_class_has_full_kit(self):
        for c, v in self.C.items():
            with self.subTest(cls=c):
                self.assertEqual(len(v["skills"]), 4)
                for s in v["skills"]:
                    self.assertTrue(s["name"] and s["desc"] and s["lore"] and s["cd"] > 0, s["id"])
                self.assertEqual(sum(1 for p in self.g["passives"].values() if p["cls"] == c), 2)   # по две своих пассивки

    def test_every_skill_and_passive_has_icon(self):
        art = set(re.findall(r"(?m)^\s+(\w+):\s?I\s?=>", GAME))
        glyphs = set(re.findall(r"(?m)^\s+(\w+):`", block("const PAS_GLYPH = {")))
        for v in self.C.values():
            for s in v["skills"]:
                self.assertIn(s["id"], art)
        for pid in self.g["passives"]:
            self.assertIn(pid, glyphs)


if __name__ == "__main__":
    unittest.main()
