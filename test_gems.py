"""Самоцветы и Кодекс самоцветов (как в LIBERTY, патч 16 октября).

Четыре вида падают с врагов в Нижних и Глубинных цехах, Фарм-зоне и Сезонной зоне по решению сервера,
учитываются им штуками, продаются на маркете не дешевле 0,4 GRAM за штуку и сдаются в Кодекс до 500 штук
каждого вида — бонусы порогов складываются навсегда. Сервер и игра должны считать одно и то же.

Запуск: python -m unittest test_gems
Проверки внутри настоящего кода игры запускают node (если node нет — они пропускаются).
"""
import json
import os
import random
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
# items.py тянет aiohttp и базу; для этих проверок они не нужны — подменяем, если их нет
for name in ("aiohttp", "sqlalchemy", "db", "models", "metrics", "mobguard", "db_atomic"):
    try:
        __import__(name)
    except Exception:
        sys.modules[name] = mock.MagicMock()
if not hasattr(sys.modules["metrics"], "inc"):
    sys.modules["metrics"] = types.SimpleNamespace(inc=lambda *a, **k: None, gauge=lambda *a, **k: None)

import items  # noqa: E402
import pvpguard  # noqa: E402

GAME = (ROOT / "game.html").read_text(encoding="utf-8")
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")
ITEMS_SRC = (ROOT / "items.py").read_text(encoding="utf-8")


def js_block(start, end="\n};"):
    a = GAME.index(start)
    return GAME[a:GAME.index(end, a)]


def js_steps():
    """GEM_STEPS из игры → {"c": [(1, {"atk": 5}), …], "r": […]}."""
    body = js_block("const GEM_STEPS = {", "\n};")
    out = {}
    for kind, rows in re.findall(r"^\s*(\w):\[(.*)\],?$", body, re.M):
        steps = []
        for need, bon in re.findall(r"\[(\d+), \{([^}]*)\}\]", rows):
            steps.append((int(need), {k: (float(v) if "." in v else int(v)) for k, v in re.findall(r"(\w+):([\d.]+)", bon)}))
        out[kind] = steps
    return out


class ServerMatchesGame(unittest.TestCase):
    def test_ids_and_kinds(self):
        body = js_block("const GEMS = {")
        ids = re.findall(r'^\s*(gm_\w+):\{name:"[^"]+", kind:"(\w)"', body, re.M)
        self.assertEqual([i for i, _ in ids], list(items.GEMS))
        self.assertEqual(tuple(i for i, k in ids if k == "c"), items.GEMS_COMMON)
        self.assertEqual(tuple(i for i, k in ids if k == "r"), items.GEMS_RARE)

    def test_thresholds(self):
        self.assertEqual(js_steps(), {k: [(n, dict(b)) for n, b in v] for k, v in items.GEM_STEPS.items()})

    def test_constants(self):
        self.assertIn(f"const GEM_MAX = {items.GEM_MAX}, GEM_MIN_PRICE = {items.GEM_MIN_PRICE};", GAME)
        locs = json.loads(re.search(r"const GEM_LOCS = (\[[^\]]*\]);", GAME).group(1))
        self.assertEqual(tuple(locs), items.GEM_LOCS)

    def test_registered_and_tradeable(self):
        for gid in items.GEMS:
            self.assertIn(gid, items.REG_IDS)                                  # учёт штуками: маркет и Кодекс
        self.assertIn("...SHARD_IDS, ...GEM_IDS]", GAME)                       # и у телефона (REG_IDS игры)
        self.assertIn('["gem", "Самоцветы"]', GAME)                             # раздел маркета

    def test_ids_fit_balance_column(self):
        # SphereBal.item — String(12): счётчик сданных «cx:gm_emer» тоже должен влезать
        for gid in items.GEMS:
            self.assertLessEqual(len(items.GEM_CX + gid), 12)

    def test_liberty_totals(self):
        c = items.gem_bonus({"gm_emer": 500})
        r = items.gem_bonus({"gm_abys": 500})
        self.assertEqual(c, {"atk": 115, "def": 100, "hp": 3000, "rate": 0.2})
        self.assertEqual(r, {"atk": 270, "def": 210, "hp": 5000, "rate": 0.2})
        self.assertEqual(items.gem_bonus({"gm_emer": 20}), {"atk": 15, "def": 10, "hp": 0, "rate": 0.0})   # пороги 1, 10 и 20 сразу
        self.assertEqual(items.gem_bonus({}), {"atk": 0, "def": 0, "hp": 0, "rate": 0.0})
        self.assertEqual(items.gem_bm({}), 0)
        self.assertGreater(items.gem_bm({"gm_jade": 300}), items.gem_bm({"gm_jade": 100}))


class Drop(unittest.TestCase):
    def test_chance_like_spheres(self):
        random.seed(7)
        lv, n = 25, 40000
        mult = 0.5 / (items.P(0.0033 + 0.000036 * (lv - 1)))      # частый — около 50%, редкий — в 10 раз реже
        got = {g: 0 for g in items.GEMS}
        for _ in range(n):
            for d in items.roll_gems(lv, mult):
                self.assertEqual((d["kind"], d["n"]), ("sph", 1))
                got[d["id"]] += 1
        for g in items.GEMS_COMMON:
            self.assertAlmostEqual(got[g] / n, 0.5, delta=0.02)
        for g in items.GEMS_RARE:
            self.assertAlmostEqual(got[g] / n, 0.05, delta=0.01)

    def test_cap_and_plain_rate(self):
        random.seed(1)
        self.assertLessEqual(sum(1 for _ in range(2000) if items.roll_gems(30, 10**9)) / 2000, 1.0)
        self.assertTrue(all(len(items.roll_gems(30, 10**9)) <= 4 for _ in range(200)))
        hits = sum(len(items.roll_gems(30)) for _ in range(20000))
        self.assertLess(hits, 10)                                  # без множителей — редкость (~1 на 24 000 убийств)

    def test_only_in_gem_locations(self):
        self.assertIn("        if loc in GEM_LOCS:", ITEMS_SRC)
        self.assertIn("for d in roll_gems(lv, base_mult):", ITEMS_SRC)
        self.assertEqual(set(items.GEM_LOCS), {"sector1", "sector2", "farm1", "season1"})


class Codex(unittest.TestCase):
    def test_give_amount(self):
        f = items.gem_give_amount
        self.assertEqual(f(1, 0, 5), 1)
        self.assertEqual(f(999, 0, 5), 5)                          # «Сдать все» — сколько учтено
        self.assertEqual(f(999, 497, 50), 3)                       # не больше 500 в Кодексе
        self.assertEqual(f(10, 500, 50), 0)
        self.assertEqual(f(3, 0, 0), 0)                            # неучтённые не принимаются
        self.assertEqual(f("x", 0, 5), 1)
        self.assertEqual(f(-4, 0, 5), 1)

    def test_endpoint_is_atomic(self):
        op = ITEMS_SRC[ITEMS_SRC.index('if op == "gem":'):ITEMS_SRC.index('if op == "craft":')]
        self.assertIn("take_spheres(s, uid, gid, k)", op)                      # списание учтённых — атомарно
        self.assertIn("SphereBal.n + k <= GEM_MAX", op)                        # счётчик не перевалит за 500 даже при гонке
        self.assertIn("given, have = min(GEM_MAX, cx.n or 0), bal.n or 0", op)  # прочитано до отката
        self.assertIn('"gemcx": gemcx', ITEMS_SRC)                             # вход: счётчики сервера


class Market(unittest.TestCase):
    def test_min_price_on_server(self):
        self.assertIn("if item[\"id\"] in items.GEMS and price < round(items.GEM_MIN_PRICE * gram.NANO) * item[\"n\"]:", WEB)

    def test_min_price_in_game(self):
        self.assertIn("if(GEMS[x.id] && price < Math.round(GEM_MIN_PRICE*n*100)/100)", GAME)


class PvpCaps(unittest.TestCase):
    def tearDown(self):
        pvpguard._gem.clear()
        pvpguard._pts.clear()

    def test_without_gems_unchanged(self):
        self.assertEqual(pvpguard.caps(1, 0), pvpguard.caps(1, 0, (0, 0)))
        hp, _, df = pvpguard.caps(1, 0)
        self.assertLess(hp, 9000)                                  # без камней предел на 1 уровне прежний — тугой

    def test_gems_raise_cap_per_player(self):
        b = items.gem_bonus({g: 500 for g in items.GEMS})
        base = pvpguard.caps(40, 0)
        more = pvpguard.caps(40, 0, (b["hp"], b["def"]))
        self.assertGreaterEqual(more[0] - base[0], int(pvpguard.GEM_K[0] * b["hp"]))
        self.assertGreaterEqual(more[2] - base[2], int(pvpguard.GEM_K[1] * b["def"]))
        self.assertEqual(more[1] - more[0] * 10, base[1] - base[0] * 10)      # CP — от прочности, как раньше

    def test_sanitize_uses_player_gems(self):
        info = {"id": 5, "mhp": 10**7, "mcp": 10**8, "hp": 10**7, "cp": 10**8, "df": 10**6}
        pvpguard.sanitize(dict(info), 30)
        plain = dict(info); pvpguard.sanitize(plain, 30)
        pvpguard.set_gems(5, 16000, 620)
        rich = dict(info); pvpguard.sanitize(rich, 30)
        self.assertGreater(rich["mhp"], plain["mhp"])
        self.assertGreater(rich["df_eff"], plain["df_eff"])

    def test_loaded_on_world_entry(self):
        self.assertIn("await items.load_gem_cx(s_g, user[\"id\"])", WEB)         # при входе в мир, своей сессией
        self.assertIn("items.gem_bm(items._gem_cx.get(user[\"id\"]))", WEB)     # и предел БМ в рейтинге


@unittest.skipUnless(shutil.which("node"), "нет node")
class InGame(unittest.TestCase):
    """Тот же запуск игры в node, что в tools/stat_caps.js: бонусы, характеристики, окно Кодекса, карта дропа."""
    @classmethod
    def setUpClass(cls):
        head = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8")
        head = head[:head.index("\n", head.index("const run = "))]
        check = r'''
const out = run(`(() => {
  const r = {};
  S.gemCx = {gm_emer:20}; r.twenty = gemBonus();
  S.gemCx = {gm_emer:500, gm_ruby:500, gm_abys:500, gm_jade:500}; r.all = gemBonus();
  S.gemCx = {gm_emer:"999", gm_ruby:-5, gm_abys:"x"}; r.dirty = GEM_IDS.map(gemCx);
  S.cls = "guard"; S.level = 50; S.max_hp = 540; S.attack = 104; S.defense = 51; S.up = {}; S.eq = {}; S.pas = {}; S.inv = []; S.store = [];
  S.gemCx = {}; const s0 = stat();
  S.gemCx = {gm_emer:500, gm_ruby:500, gm_abys:500, gm_jade:500}; const s1 = stat();
  r.delta = {atk:s1.atk - s0.atk, def:s1.def - s0.def, hp:s1.maxhp - s0.maxhp, rate:+(s1.rate - s0.rate).toFixed(2)};
  const D1 = mobDrops("dg15", "sector1"), D2 = mobDrops("scrap_crawler", "scrapfields"), D3 = mobDrops("db25", "sector2");
  r.drops = {gems:D1.gems.map(g => g.id), inItems:D1.items.filter(i => GEMS[i.id]).length, fields:D2.gems.length, boss:D3.gems[0].chance > D1.gems[0].chance};
  S.gemCx = {gm_emer:12}; S.inv = [{id:"gm_emer", n:3}];
  const h = gemCodexHtml();
  r.html = {cards:(h.match(/class="gmcard/g) || []).length, next:h.includes("До порога 20: ещё 8"), ruby:/data-gmgive="gm_ruby" data-n="1" disabled/.test(h)};
  S.gemCx = {}; S.inv = [{id:"gm_ruby", n:12}]; S.store = [{id:"gm_ruby", n:5}];
  gemGive("gm_ruby", true);
  r.offline = [gemCx("gm_ruby"), count(S.inv, "gm_ruby"), count(S.store, "gm_ruby")];
  S.gemCx = {gm_jade:499}; S.inv = [{id:"gm_jade", n:9}]; S.store = [];
  gemGive("gm_jade", true);
  r.capped = [gemCx("gm_jade"), count(S.inv, "gm_jade")];
  r.types = GEM_IDS.map(id => ITEMS[id].type + ":" + !!ITEMS[id].nosell + ":" + mkCatOf(id) + ":" + stCatOf(id));
  r.icons = GEM_IDS.every(id => icon(id).startsWith("<svg"));
  return JSON.stringify(r);
})()`);
process.stdout.write(out + "\n");
'''
        res = subprocess.run(["node", "-", str(ROOT / "game.html")], input=head + check, capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            raise AssertionError(res.stderr[-2000:])
        cls.r = json.loads(res.stdout.strip().splitlines()[-1])

    def test_bonus(self):
        self.assertEqual(self.r["twenty"], {"atk": 15, "def": 10, "hp": 0, "rate": 0})
        self.assertEqual(self.r["all"], {"atk": 770, "def": 620, "hp": 16000, "rate": 0.8})
        self.assertEqual(self.r["dirty"], [500, 0, 0, 0])

    def test_stats(self):
        # плоский бонус до множителей класса: у Стража броня ×1,45 и прочность ×1,3
        self.assertEqual(self.r["delta"], {"atk": 770, "def": 899, "hp": 20800, "rate": 0.8})

    def test_map_and_no_local_drop(self):
        d = self.r["drops"]
        self.assertEqual(d["gems"], list(items.GEMS))
        self.assertEqual(d["inItems"], 0)                     # rollDrops их не бросает: только сервер
        self.assertEqual(d["fields"], 0)
        self.assertTrue(d["boss"])

    def test_codex_window(self):
        self.assertEqual(self.r["html"], {"cards": 4, "next": True, "ruby": True})

    def test_give(self):
        self.assertEqual(self.r["offline"], [17, 0, 0])      # без сервера: сумка, затем склад
        self.assertEqual(self.r["capped"], [500, 8])         # больше 500 не принимается

    def test_item_types(self):
        self.assertEqual(self.r["types"], ["gem:true:gem:res"] * 4)
        self.assertTrue(self.r["icons"])


if __name__ == "__main__":
    unittest.main()
