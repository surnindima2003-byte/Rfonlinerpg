"""Статические контракты единственного HTML-клиента и серверного каталога.

Тесты намеренно используют только стандартную библиотеку: их можно запускать в
том же минимальном окружении, где разворачивается бот.
"""

import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GAME = (ROOT / "game.html").read_text(encoding="utf-8")
ITEMS_SERVER = (ROOT / "items.py").read_text(encoding="utf-8")


def js_array_body(name: str) -> str:
    match = re.search(rf"const {name} = \[(.*?)\n\];", GAME, re.S)
    if not match:
        raise AssertionError(f"Не найден JS-массив {name}")
    return match.group(1)


class GearCatalogContractTests(unittest.TestCase):
    def test_every_tier_has_an_icon_palette(self):
        tiers = re.findall(r"\{n:\d+, lvl:\d+,", js_array_body("TIERS"))
        palettes = re.findall(r"\{M:\[", js_array_body("TIER_MAT"))
        self.assertEqual(len(tiers), 10)
        self.assertEqual(
            len(palettes),
            len(tiers),
            "Каждому поколению снаряжения нужна палитра, иначе icon() ломает UI",
        )

    def test_server_and_client_share_the_same_tiers(self):
        match = re.search(r"TIER_LVL = (\{[^\n]+\})", ITEMS_SERVER)
        self.assertIsNotNone(match)
        server_tiers = ast.literal_eval(match.group(1))
        client_tiers = {
            int(number): int(level)
            for number, level in re.findall(
                r"\{n:(\d+), lvl:(\d+),", js_array_body("TIERS")
            )
        }
        self.assertEqual(server_tiers, client_tiers)

    def test_removed_legacy_items_are_not_in_the_live_catalog(self):
        live_catalog = re.search(
            r"const ITEMS = \{(.*?)\n\};\n// =+ СНАРЯЖЕНИЕ", GAME, re.S
        ).group(1)
        legacy = {
            "st_head", "sensor1", "sensor2", "st_armor", "plate1", "plate2",
            "st_module", "servo", "shieldgen", "st_core", "reactor1", "reactor2",
            "st_legs", "tracks1", "tracks2", "st_weapon", "laser1", "laser2",
            "plasma",
        }
        self.assertFalse(legacy.intersection(re.findall(r"^\s*(\w+):\{", live_catalog, re.M)))


class ButtonContractTests(unittest.TestCase):
    PAYLOAD_ATTRIBUTES = {"id", "n", "name", "url", "v", "lay"}

    def test_each_button_action_is_referenced_by_a_handler(self):
        """Ловит кнопку с новым data-action, для которой забыли обработчик."""
        tags = re.findall(r"<button\b[^>]*>", GAME, re.S)
        attributes = {
            attr
            for tag in tags
            for attr in re.findall(r"data-([a-z][a-z0-9-]*)\s*=", tag)
        } - self.PAYLOAD_ATTRIBUTES
        missing = []
        for attr in sorted(attributes):
            camel = re.sub(r"-([a-z])", lambda m: m.group(1).upper(), attr)
            references = (
                rf"\b(?:b|d)\.{re.escape(camel)}\b",
                rf"\.dataset\.{re.escape(camel)}\b",
                rf"\[data-{re.escape(attr)}(?:=|\])",
            )
            if not any(re.search(pattern, GAME) for pattern in references):
                missing.append(attr)
        self.assertEqual(missing, [], f"Кнопки без найденного обработчика: {missing}")


if __name__ == "__main__":
    unittest.main()
