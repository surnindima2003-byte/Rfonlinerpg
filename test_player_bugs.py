"""Баги, которые видят игроки: продажа торговцу не съедает учтённые сферы, вкладка после вайпа не теряет
прогресс молча, «Сессия устарела» не показывается вытесненной вкладке, в призовом рейтинге нет сырого ника Telegram,
баланс GRAM показан одинаково везде.

Запуск: python -m unittest test_player_bugs
Проверка продажи запускает настоящую функцию из game.html в node (если node нет — этот тест пропускается).
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GAME = (ROOT / "game.html").read_text(encoding="utf-8")
SEASON = (ROOT / "seasonpts.py").read_text(encoding="utf-8")


def _line(start):
    a = GAME.index(start)
    return GAME[a:GAME.index("\n", a)]


class TraderSale(unittest.TestCase):
    def run_js(self, inv, store, reg, n):
        node = shutil.which("node")
        if not node:
            self.skipTest("нет node")
        a = GAME.index("function regUseSph(")
        fn = GAME[a:GAME.index("\nfunction regGone", a)]
        js = "\n".join([
            "const ITEMS = {sph_ti:{type:'ench'}};",
            _line("const count = (list, id) =>"),
            f"const S = {{inv:{json.dumps(inv)}, store:{json.dumps(store)}}};",
            f"const REG = {{on:true, sph:{{sph_ti:{reg}}}}};",
            "const sent = []; function regApi(op, b){ sent.push(b); }",
            fn,
            f"regUseSph('sph_ti', {n});",
            "console.log(JSON.stringify({left: REG.sph.sph_ti, sent}));",
        ])
        out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_unregistered_go_first(self):
        # 10 сфер, из них 4 учтённых (продаются за GRAM). Продаём 5 — учтённые не трогаем
        r = self.run_js([{"id": "sph_ti", "n": 10}], [], 4, 5)
        self.assertEqual(r["left"], 4)
        self.assertEqual(r["sent"], [])

    def test_registered_only_when_needed(self):
        # продаём 8 из 10: неучтённых 6, значит учтённых уходит 2
        r = self.run_js([{"id": "sph_ti", "n": 10}], [], 4, 8)
        self.assertEqual(r["left"], 2)
        self.assertEqual(r["sent"], [{"sph": {"sph_ti": 2}}])

    def test_store_counts_as_unregistered_stock(self):
        # в сумке 3, на складе 5, учтённых 3 (реестр общий на сумку и склад): неучтённых 5 — продажа 3 их и тратит
        r = self.run_js([{"id": "sph_ti", "n": 3}], [{"id": "sph_ti", "n": 5}], 3, 3)
        self.assertEqual(r["left"], 3)

    def test_called_before_removal(self):
        a = GAME.index("else if(d.sell !== undefined){")
        part = GAME[a:GAME.index("else if(d.st)", a)]
        self.assertLess(part.index("regUseSph("), part.index("removeItem(S.inv"))


class StaleEpoch(unittest.TestCase):
    def test_push_handles_stale_epoch(self):
        a = GAME.index("async function push(force)")
        push = GAME[a:GAME.index("setInterval(() => push(false)", a)]
        self.assertIn('includes("stale epoch")', push)
        self.assertIn("staleEpoch()", push)
        fn = GAME[GAME.index("function staleEpoch()"):GAME.index("const api = NET.api")]
        self.assertIn("serverEpoch = null", fn)          # сохранения больше не уходят
        self.assertIn("location.reload()", fn)           # загрузка увидит новую эпоху и начнёт заново


class CloseReason(unittest.TestCase):
    def test_replaced_tab_is_not_session_expired(self):
        a = GAME.index("ws.onclose = ev =>")
        h = GAME[a:GAME.index("};", a)]
        self.assertIn('includes("too many")', h)
        self.assertLess(h.index("replacedByOther()"), h.index("sessionExpired()"))
        rt = (ROOT / "realtime.py").read_text(encoding="utf-8")
        self.assertIn('old.close(CLOSE_REPLACED, "too many connections")', rt)
        self.assertIn("function replacedByOther()", GAME)


class SeasonNames(unittest.TestCase):
    def test_ranking_uses_public_name(self):
        nicks = SEASON[SEASON.index("async def _nicks"):SEASON.index("async def top(")]
        self.assertIn("_public(b, c)", nicks)
        self.assertNotIn('b or c or "Пилот"', SEASON)
        self.assertIn('"nick": _public("", h.nick)', SEASON)         # итоги прошлых сезонов тоже

    def test_fallback_without_webserver(self):
        ns = {}
        a = SEASON.index("def _public(")
        exec(SEASON[a:SEASON.index("\n\n\n", a)], ns)
        sys.modules.setdefault("webserver", None)                  # импорт webserver не удастся
        try:
            self.assertEqual(ns["_public"]("", "Администратор"), "Пилот")
            self.assertEqual(ns["_public"]("Rex", "Администратор"), "Rex")
        finally:
            if sys.modules.get("webserver") is None:
                sys.modules.pop("webserver", None)


class GramFormat(unittest.TestCase):
    """Баланс GRAM: один формат (запятая, округление вниз) в HUD, магазине и кошельке."""
    def run_js(self, expr):
        node = shutil.which("node")
        if not node:
            self.skipTest("нет node")
        a = GAME.index("function gramShow(")
        fn = GAME[a:GAME.index("\nconst gemSvg", a)]
        out = subprocess.run([node, "-e", fn + "\nconsole.log(JSON.stringify(" + expr + "));"], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_same_digits_everywhere(self):
        hud, wallet = self.run_js("[gramShow(12.3456), gramHtml(12.3456)]")
        self.assertEqual(hud, "12,34")                      # вниз, а не 12,35
        self.assertTrue(wallet.startswith("12<span>,3456</span>"), wallet)

    def test_float_edge_and_thousands(self):
        r = self.run_js("[gramShow(0.29), gramShow(0.3), gramShow(1234.5), gramShow(5, 4), gramHtml(0.29)]")
        self.assertEqual(r[0], "0,29")
        self.assertEqual(r[1], "0,3")
        self.assertEqual(r[2].replace("\u00a0", " "), "1 234,5")
        self.assertEqual(r[3], "5")
        self.assertTrue(r[4].startswith("0<span>,2900</span>"), r[4])

    def test_all_balance_spots_use_it(self):
        self.assertIn('hudText("hGram", typeof gramBal === "function" ? gramShow(gramBal())', GAME)
        self.assertIn("Баланс: <b>${gramShow(bal)}</b>", GAME)
        self.assertIn("Доступно: <b style=\"display:inline\">${gramShow(Wl.gram, 4)}", GAME)
        self.assertNotIn("toFixed(7)", GAME[GAME.index("function gramHtml"):GAME.index("const gemSvg")])


class Toasts(unittest.TestCase):
    """Тосты: не поверх заголовков и меню, время показа по длине текста, закрываются касанием."""
    def test_duration_by_length(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("нет node")
        fn = _line("const toastMs = msg =>")
        out = subprocess.run([node, "-e", fn + "\nconsole.log(JSON.stringify([toastMs('ok'), toastMs('x'.repeat(40)), toastMs('x'.repeat(500))]));"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        short, mid, long = json.loads(out.stdout)
        self.assertGreaterEqual(short, 2200)               # было 1,9 с на любое сообщение
        self.assertEqual(mid, 2200 + 60 * 40)
        self.assertEqual(long, 7000)

    def test_placement_and_tap(self):
        fn = GAME[GAME.index("function toast(msg){"):GAME.index("// ---- иконки предметов ----")]
        self.assertIn("placeToast(t)", fn)
        self.assertIn("toastMs(msg)", fn)
        self.assertIn('addEventListener("click"', fn)
        place = GAME[GAME.index("function placeToast(t)"):GAME.index("function toast(msg){")]
        for part in ('"tut-top"', '#sheet .card', '"gmList"', '"#gmToggle"', '".nav"'):
            self.assertIn(part, place)
        css = _line(".toast{position:fixed")
        self.assertIn("width:max-content", css)                # длинный текст — на всю ширину, а не узкой колонкой
        self.assertIn("max-width:calc(100% - 32px)", css)
        self.assertIn("pointer-events:auto", _line(".toast.show{"))


class CompactHud(unittest.TestCase):
    """Узкий экран (до 400 px): HUD компактнее, миникарта меньше, цифры опыта на месте."""
    def block(self):
        a = GAME.index("@media (max-width:400px){\n.hud{right:92px}")
        return GAME[a:GAME.index("</style>", a)]

    def test_compact_rules(self):
        b = self.block()
        self.assertIn(".h2-top{display:contents}", b)
        self.assertIn(".h2-bars{grid-column:2;grid-row:2", b)
        self.assertNotIn(".bar.xp span{display:none}", b)          # цифры опыта остаются (так решили)
        self.assertIn("max(var(--hb, 120px), 104px)", b)           # «Меню» не наезжает на название локации

    def test_minimap_smaller_only_on_narrow(self):
        self.assertIn("const size = vw <= 400 ? 72 : 92", GAME)


class PortalTouch(unittest.TestCase):
    """Портал открывается касанием (без кнопки сверху), но не сразу после появления, не повторно и не в бою."""
    def test_touch_logic(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("нет node")
        a = GAME.index("const PORTAL_KINDS = ")
        block = GAME[a:GAME.index("// окно портала выхода", a)]
        js = "\n".join([
            "let opened = [], sheetOpen = false, firing = false, auto = false, now = 10, locKey = 'scrapfields';",
            "const S = {pvpT: 0}, npcs = [{kind:'back', x:1000, y:1060}];",
            "let p = {x:1000, y:1000, dead:false};",
            "const $ = id => ({classList:{contains: () => sheetOpen}});",
            "const UI = {portal: () => opened.push('portal')}; function openBackPortal(){ opened.push('back'); }",
            block,
            "const at = (x, y) => { p.x = x; p.y = y; portalTouch(); return opened.length; };",
            "const r = {};",
            "r.spawn = at(1000, 1000);                 // появился у портала (60 px) — не открываем",
            "r.stillThere = at(1000, 1040);",
            "r.away = at(1000, 800);",
            "r.touch = at(1000, 1050);                 // отъехал и наехал — открываем",
            "r.standing = at(1000, 1055);              // стоит на портале после закрытия — не повторяем",
            "at(1000, 800); firing = true; r.fight = at(1000, 1050); firing = false;",
            "at(1000, 800); S.pvpT = 99; r.pvp = at(1000, 1050); S.pvpT = 0;",
            "at(1000, 800); sheetOpen = true; r.sheet = at(1000, 1050); sheetOpen = false;",
            "at(1000, 800); r.again = at(1000, 1050);",
            "locKey = 'lobby'; npcs[0] = {kind:'portal', x:0, y:0}; r.newLoc = at(10, 10);",
            "at(0, 300); r.lobby = at(0, 20);",
            "console.log(JSON.stringify({r, opened}));",
        ])
        out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        d = json.loads(out.stdout)
        r = d["r"]
        self.assertEqual((r["spawn"], r["stillThere"], r["away"]), (0, 0, 0))
        self.assertEqual(r["touch"], 1)
        self.assertEqual(r["standing"], 1)
        self.assertEqual((r["fight"], r["pvp"], r["sheet"]), (1, 1, 1))     # в бою и поверх окна — не открываем
        self.assertEqual(r["again"], 2)
        self.assertEqual(r["newLoc"], 2)                                    # новая локация: сначала отъехать
        self.assertEqual(r["lobby"], 3)
        self.assertEqual(d["opened"], ["back", "back", "portal"])

    def test_no_top_button_for_portals(self):
        a = GAME.index("// ближайший NPC для кнопки «Поговорить»")
        part = GAME[a:GAME.index("classUpdate(dt);", a)]
        self.assertIn("!PORTAL_KINDS.includes(n.kind)", part)
        self.assertIn("portalTouch();", part)
        self.assertNotIn('tutNear("portal") ? "#talkBtn"', GAME)
        self.assertNotIn('tutNear("back") ? "#talkBtn"', GAME)
        back = GAME[GAME.index("function openBackPortal()"):GAME.index("function openBackPortal()") + 800]
        self.assertIn('data-go="lobby"', back)                             # выход — по кнопке в окне, а не сразу


class Shop(unittest.TestCase):
    """Магазин: 3 вкладки вместо 8 кнопок, ни один раздел не потерян; вместо «Мало» — «Пополнить, не хватает …»."""
    def test_every_section_is_in_a_tab(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("нет node")
        harness = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8").split("\nconst out = JSON.parse")[0]
        js = "process.argv[2] = " + json.dumps(str(ROOT / "game.html")) + ";\n" + harness + "\nconsole.log(run(`JSON.stringify({tabs:[...new Set(PACKS.map(p => p.tab))], groups:SHOP_GROUPS.map(g => g.secs.map(s => s[0])), need:[gramNeed(13.65), gramNeed(0.001), gramNeed(2.6449)]})`));"
        out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-1500:])
        d = json.loads(out.stdout.strip().splitlines()[-1])
        in_groups = [k for g in d["groups"] for k in g]
        self.assertEqual(len(d["groups"]), 3)
        self.assertEqual(sorted(set(d["tabs"])), sorted(in_groups))          # каждый раздел в какой-то вкладке, ровно один раз
        self.assertEqual(len(in_groups), len(set(in_groups)))
        self.assertEqual(d["need"], ["13,65", "0,01", "2,65"])                # не хватает — с округлением вверх

    def test_topup_instead_of_low(self):
        card = GAME[GAME.index("function shopCard(pk, bal)"):GAME.index("function renderShop()")]
        self.assertIn('data-shop="topup"', card)
        self.assertIn("не хватает ${gramNeed(price - bal)}", card)
        self.assertNotIn('"Мало"', card)
        shop = GAME[GAME.index("function renderShop()"):GAME.index("// ===================== СЕЗОН")]
        self.assertIn("SHOP_GROUPS.map(g =>", shop)
        self.assertIn('"#shopSec-" + sec', shop)                             # ссылка на раздел прокручивает к нему


class Market(unittest.TestCase):
    """Маркет: разделы — ровно то, что можно продать; у каждого лота виден продавец."""
    def test_categories_match_tradable_items(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("нет node")
        harness = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8").split("\nconst out = JSON.parse")[0]
        js = ("process.argv[2] = " + json.dumps(str(ROOT / "game.html")) + ";\n" + harness +
              "\nconsole.log(run(`JSON.stringify({cats:MK_CATS.map(c => c[0]), sell:[...GEAR_IDS, ...REG_IDS].map(id => [id, mkCatOf(id)]),"
              " notSell:[...BOOK_IDS.slice(0, 3), 'wire', 'chip', 'plate'].map(id => mkCatOf(id))})`));")
        out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-1500:])
        d = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertNotIn("book", d["cats"])
        self.assertNotIn("mat", d["cats"])
        bad = [(i, c) for i, c in d["sell"] if c not in d["cats"]]
        self.assertEqual(bad, [])                                         # дроны, крылья, плащи — в своих разделах
        self.assertEqual({c for i, c in d["sell"] if i.startswith(("d_", "wg_", "ck_"))}, {"drone", "look"})
        self.assertTrue(all(c not in d["cats"] for c in d["notSell"]))

    def test_seller_shown_for_gear(self):
        row = GAME[GAME.index("function mkRow(lot, mine)"):GAME.index("// сравнение артефактов")]
        self.assertIn('class="mk-seller"', row)
        self.assertNotIn('it.type === "gear" ? gearDesc(x.id, x.g, x.e) : "@"', row)   # раньше у снаряжения продавца не было
        self.assertIn("MK_CATS.filter(([k]) => k === \"all\" || k === mkCat || cats[k])", GAME)


if __name__ == "__main__":
    unittest.main()
