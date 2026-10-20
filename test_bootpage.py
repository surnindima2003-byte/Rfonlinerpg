"""Загрузка на медленном интернете (bootpage.py): маленькая первая страница + части кода и стилей.

Запуск: python -m unittest test_bootpage
"""
import json
import re
import unittest
from pathlib import Path

import bootpage

ROOT = Path(__file__).resolve().parent
GAME = (ROOT / "game.html").read_text(encoding="utf-8")


def manifest(shell):
    return json.loads(re.search(r"var PARTS = (\[.*?\]), TG", shell).group(1))


class Build(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shell, cls.parts = bootpage.build(GAME)
        cls.m = manifest(cls.shell)

    def test_code_and_styles_survive_exactly(self):
        # склеенные части — ровно тот код и те стили, что в game.html
        js = "".join(self.parts[p["u"][3:]] for p in self.m if not p.get("css"))
        css = "".join(self.parts[p["u"][3:]] for p in self.m if p.get("css"))
        self.assertEqual(js, GAME[GAME.rfind("<script>") + 8:GAME.rfind("</script>")])
        self.assertEqual(css, re.search(r"<style>(.*?)</style>", GAME, re.S).group(1))

    def test_sizes_match_what_loader_checks(self):
        for p in self.m:
            text = self.parts[p["u"][3:]]
            self.assertEqual(p["n"], len(text.encode("utf-16-le")) // 2)      # длина строки в JS (эмодзи — 2)
            self.assertEqual(p["b"], len(text.encode("utf-8")))
            self.assertRegex(p["u"][3:], bootpage.PART_RE)

    def test_first_page_is_small_and_has_loader_and_guard(self):
        self.assertLess(len(self.shell.encode("utf-8")), 40_000)
        self.assertLess(self.shell.find('id="boot"'), self.shell.find("MW_BOOT") + 1)   # экран загрузки — в начале
        self.assertIn("window.MW_BOOT = 1", self.shell)
        self.assertIn('addEventListener("load",chk)', self.shell)
        self.assertNotIn('<script src="https://telegram.org', self.shell)              # Telegram грузит загрузчик
        self.assertIn('"https://telegram.org/js/telegram-web-app.js"', self.shell)
        self.assertIn("media=\"print\" onload=\"this.media='all'\"", self.shell)          # шрифты не держат экран
        self.assertEqual(self.shell.count("<script>"), 2)                                # сторож и загрузчик

    def test_parts_are_reasonable(self):
        self.assertGreater(len(self.m), 10)
        self.assertTrue(all(p["b"] <= bootpage.PART_BYTES * 1.2 for p in self.m if not p.get("css")))

    def test_unexpected_page_is_served_whole(self):
        self.assertIsNone(bootpage.build("<html><body>нет скрипта</body></html>"))
        self.assertIsNone(bootpage.build("<html><body><script>var a=1</script></body></html>"))   # слишком мал


if __name__ == "__main__":
    unittest.main()
