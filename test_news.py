"""Окно «Обновления» (как кнопка «Обновления» в LIBERTY, патч 16 октября).

Все патчи — на странице patches.html, рядом вкладка «Гайд» (guide.html). Игра открывает их поверх себя
во встроенном режиме (?embed=1). Красная точка у «Меню» и у пункта «Обновления» — вышел патч, который игрок
ещё не открывал: сервер отдаёт id первого блока data-patch (GET /api/patches), открытый хранится в S.patchSeen.
Ссылки-якоря внутри окна листают только страницу и не сдвигают игру (в LIBERTY из-за этого пропадал крестик).

Запуск: python -m unittest test_news
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GAME = (ROOT / "game.html").read_text(encoding="utf-8")
WEB = (ROOT / "webserver.py").read_text(encoding="utf-8")
PATCHES = (ROOT / "patches.html").read_text(encoding="utf-8")
GUIDE = (ROOT / "guide.html").read_text(encoding="utf-8")


def parse_latest_patch():
    """Функция сервера webserver.parse_latest_patch — без импорта webserver (ему нужны aiohttp и база)."""
    import ast
    from html import unescape as html_unescape
    tree = ast.parse(WEB)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "parse_latest_patch")
    ns = {"re": re, "html_unescape": html_unescape}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "webserver.py", "exec"), ns)
    return ns["parse_latest_patch"]


class Server(unittest.TestCase):
    def test_routes(self):
        for route in ('add_get("/patches.html", patches_page)', 'add_get("/patches", patches_page)', 'add_get("/api/patches", api_patches)'):
            self.assertIn(route, WEB)

    def test_latest_patch_is_found(self):
        # сервер берёт первый блок section с data-patch (комментарии не в счёт) — с каждым новым патчем он другой
        from html import unescape
        tag = re.search(r"<section\b[^>]*\bdata-patch=[^>]*>", re.sub(r"<!--.*?-->", "", PATCHES, flags=re.S)).group(0)
        want = (re.search(r'data-patch="([^"]+)"', tag).group(1), unescape(re.search(r'data-title="([^"]*)"', tag).group(1)))
        self.assertEqual(parse_latest_patch()(PATCHES), want)
        self.assertRegex(want[0], r"^\d{4}-\d{2}-\d{2}(-\d)?$")       # дата; второй патч за день — с номером
        self.assertTrue(want[1])

    def test_newest_patch_goes_first(self):
        clean = re.sub(r"<!--.*?-->", "", PATCHES, flags=re.S)
        ids = re.findall(r'<section\b[^>]*\bdata-patch="([0-9A-Za-z._-]+)"', clean)
        self.assertTrue(ids)
        self.assertEqual(ids, sorted(ids, reverse=True))           # сервер берёт первый — он должен быть самым свежим

    def test_parser_edge_cases(self):
        f = parse_latest_patch()
        self.assertEqual(f('<section data-title="Новое &amp; важное" class="patch" data-patch="2026-11-01">'), ("2026-11-01", "Новое & важное"))
        self.assertEqual(f('<!-- <section data-patch="2099-01-01"> --><section data-patch="2026-11-01">'), ("2026-11-01", ""))
        self.assertEqual(f('<div data-patch="2026-12-01"></div>'), ("", ""))                    # только <section>
        self.assertEqual(f('<section data-patch="ГГГГ-ММ-ДД">'), ("", ""))                       # пример из комментария не id


class Pages(unittest.TestCase):
    def test_embed_mode(self):
        for page in (PATCHES, GUIDE):
            self.assertTrue('document.documentElement.classList.add("embed")' in page and ".embed .top{display:none}" in page)

    def test_anchors_scroll_only_the_page(self):
        for page in (PATCHES, GUIDE):
            self.assertTrue('if(href.charAt(0) === "#"){' in page and "e.preventDefault();" in page and "window.scrollTo(" in page)
            self.assertFalse(".scrollIntoView(" in page)             # внутри окна он листает и игру под ним
            self.assertFalse("location.hash =" in page)

    def test_links_inside_window_talk_to_game(self):
        for page in (PATCHES, GUIDE):
            self.assertTrue('msg.mw = "news"; window.parent.postMessage(msg, "*");' in page)
            self.assertTrue('href === "/" ? {act:"close"}' in page)    # «Играть» закрывает окно, а не грузит игру в нём

    def test_patch_page_links_resolve(self):
        ids = set(re.findall(r'\bid="([\w-]+)"', PATCHES))
        for target in re.findall(r'href="#([\w-]+)"', PATCHES):
            self.assertIn(target, ids)

    def test_gem_icons_unique(self):
        grads = re.findall(r'id="(gmi\d+\w?)"', PATCHES)
        self.assertEqual(len(grads), len(set(grads)))                 # у каждой иконки свои градиенты


class Game(unittest.TestCase):
    def test_menu_and_window(self):
        self.assertIn('data-gm="news"', GAME)
        self.assertIn('<i class="gm-new" hidden></i>', GAME)
        self.assertIn('id="newsOv"', GAME)
        self.assertIn('<iframe id="newsFr" title="Обновления"></iframe>', GAME)
        self.assertIn('MENU_OPEN.news = () => openNews("patches");', GAME)

    def test_dot_and_seen(self):
        self.assertIn('fetch("/api/patches", {cache:"no-store"})', GAME)
        self.assertIn('if(NEWS.tab === "patches" && newsFresh()){ S.patchSeen = NEWS.latest; save(); newsBadge(); }', GAME)

    def test_messages_only_from_own_frame(self):
        self.assertIn('if(!fr || e.source !== fr.contentWindow || !e.data || e.data.mw !== "news") return;', GAME)

    def test_window_over_tutorial_and_input_blocked(self):
        z = int(re.search(r"\.news-ov\{[^}]*z-index:(\d+)", GAME).group(1))
        tut = max(int(x) for x in re.findall(r"\.tut\w+\{[^}]*?z-index:(\d+)", GAME))
        self.assertGreater(z, tut)                                   # поверх обучения
        self.assertIn('$("newsOv").addEventListener(ev, e => e.stopPropagation())', GAME)   # касания не доходят до джойстика


@unittest.skipUnless(shutil.which("node"), "нет node")
class InGame(unittest.TestCase):
    """Логика точки в настоящем коде игры (тот же запуск, что в tools/stat_caps.js)."""
    @classmethod
    def setUpClass(cls):
        head = (ROOT / "tools" / "stat_caps.js").read_text(encoding="utf-8")
        head = head[:head.index("\n", head.index("const run = "))]
        check = r'''
const out = run(`(() => {
  const r = {};
  r.noServer = newsFresh();                                  // сервер ещё не ответил — точки нет
  NEWS.latest = "2026-10-18"; S.patchSeen = ""; r.fresh = newsFresh();
  openNews("guide"); r.afterGuide = newsFresh();             // гайд — не обновления: точка остаётся
  openNews("patches"); r.afterPatches = newsFresh(); r.seen = S.patchSeen; r.tab = NEWS.tab;
  NEWS.latest = "2026-11-01"; r.nextPatch = newsFresh();     // вышел новый патч — точка снова горит
  return JSON.stringify(r);
})()`);
process.stdout.write(out + "\n");
'''
        res = subprocess.run(["node", "-", str(ROOT / "game.html")], input=head + check, capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            raise AssertionError(res.stderr[-2000:])
        cls.r = json.loads(res.stdout.strip().splitlines()[-1])

    def test_dot(self):
        self.assertEqual(self.r, {"noServer": False, "fresh": True, "afterGuide": True, "afterPatches": False,
                                  "seen": "2026-10-18", "tab": "patches", "nextPatch": True})


if __name__ == "__main__":
    unittest.main()
