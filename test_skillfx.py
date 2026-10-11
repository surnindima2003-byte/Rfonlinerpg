"""Проверка пакетов эффектов умений (skillfx.clean): соседям уходит только картинка рядом с заклинателем."""
import unittest

import skillfx


def caster(**kw):
    return {"id": 7, "loc": "scrapfields", "x": 1000, "y": 1000, **kw}


class SkillFx(unittest.TestCase):
    def test_full_packet_passes(self):
        d = {"s": "aoe1",
             "f": [["r", 1000, 1000, 120, 0.45, "#E0694A"], ["s", 1050, 990, 1.2, 0.2, "#FFB26B"], ["t", 1000, 1000, 1200, 1000, 0.3, "#9B7BFF"]],
             "b": [[1000, 974, 1180, 990, 0.12, 1, "#C77DFF"]],
             "x": [[1180, 976, "КРИТ -340", "#F2C14B"], [1000, 960, "+55", "#7BC98A"], [1100, 950, "ИЗ ТЕНИ", "#FF5FA2"]],
             "g": [["field", 1000, 1000, 140, 6], ["buff", 7, "def", 8], ["echo", 990, 1000, 6, 0.5], ["rune", 1000, 1000, 10]]}
        out = skillfx.clean(d, caster(), 100.0)
        self.assertEqual(out["t"], "sfx")
        self.assertEqual((out["from"], out["loc"], out["s"]), (7, "scrapfields", "aoe1"))
        self.assertEqual(len(out["f"]), 3)
        self.assertEqual(out["b"], [[1000, 974, 1180, 990, 0.12, 1, "#C77DFF"]])
        self.assertEqual(len(out["x"]), 3)
        self.assertEqual(len(out["g"]), 4)

    def test_far_points_and_garbage_dropped(self):
        d = {"f": [["r", 9000, 9000, 120, 0.4, "#ffffff"],          # на другом конце карты
                   ["r", 1000, 1000, "big", 0.4, "#ffffff"],         # не число
                   ["t", 1000, 1000, 1000, 5000, 0.3, "#ffffff"],    # хвост следа далеко
                   ["q", 1000, 1000, 10, 0.4, "#ffffff"],            # неизвестный вид
                   ["r", 1000, 1000, 1e9, 99, "red; x"]],            # огромные числа и плохой цвет — обрезаем
             "b": [[1000, 1000, 1000, 1000, 0.1, 0]],                # короткая запись
             "g": [["buff", True, "def", 5], ["buff", 7, "DEF<b>", 5], ["nuke", 1000, 1000, 5]]}
        out = skillfx.clean(d, caster(), 100.0)
        self.assertEqual(out["f"], [["r", 1000, 1000, 600, 2.0, skillfx.DEFAULT_COLOR]])
        self.assertNotIn("b", out)
        self.assertNotIn("g", out)

    def test_texts_only_numbers_and_skill_words(self):
        d = {"x": [[1000, 1000, "-120", "#ffffff"], [1000, 1000, "Продаю аккаунт t.me/x", "#ffffff"],
                   [1000, 1000, "УРОВЕНЬ 50", "#ffffff"], [1000, 1000, "Щит-контур", "#6EE7A8"], [1000, 1000, 5, "#ffffff"]]}
        out = skillfx.clean(d, caster(), 100.0)
        self.assertEqual([x[2] for x in out["x"]], ["-120", "Щит-контур"])

    def test_list_caps(self):
        d = {"f": [["r", 1000, 1000, 50, 0.3, "#ffffff"]] * 100, "b": [[1000, 1000, 1010, 1010, 0.1, 0, "#ffffff"]] * 100}
        out = skillfx.clean(d, caster(), 100.0)
        self.assertEqual((len(out["f"]), len(out["b"])), (skillfx.MAX_F, skillfx.MAX_B))

    def test_rate_limit_with_burst(self):
        info, d = caster(), {"f": [["r", 1000, 1000, 50, 0.3, "#ffffff"]]}
        sent = sum(1 for _ in range(20) if skillfx.clean(d, info, 100.0))
        self.assertEqual(sent, int(skillfx.BURST))                   # залп в одно мгновение — только запас
        self.assertIsNotNone(skillfx.clean(d, info, 100.0 + 1 / skillfx.RATE + 0.001))   # дальше — по RATE в секунду
        burst3 = [skillfx.clean(d, caster(), 200.0 + k * 0.12) for k in range(3)]       # очередь Снайпера проходит целиком
        self.assertTrue(all(burst3))

    def test_empty_bad_skill_id_and_dead(self):
        self.assertIsNone(skillfx.clean({}, caster(), 1.0))
        self.assertIsNone(skillfx.clean({"s": "<script>"}, caster(), 1.0))
        self.assertEqual(skillfx.clean({"s": "nanofield"}, caster(), 1.0)["s"], "nanofield")
        self.assertIsNone(skillfx.clean({"s": "aoe1", "f": [["r", 1000, 1000, 50, 0.3, "#ffffff"]]}, caster(dead=True), 1.0))

    def test_client_matches_server(self):
        import re
        from pathlib import Path
        game = (Path(__file__).resolve().parent / "game.html").read_text(encoding="utf-8")
        words = re.search(r"const SFX_WORDS = new Set\(\[(.*?)\]\)", game).group(1)
        self.assertEqual(set(re.findall(r'"([^"]+)"', words)), skillfx.WORDS)   # надписи умений — одинаковые списки
        a = game.index("const _useSfx = useSkill;")
        after = [m.start() for m in re.finditer(r"useSkill = function", game) if m.start() > a]
        self.assertEqual(len(after), 1)                                    # снимаем самой внешней обёрткой — после неё других нет
        self.assertIn('if(d.t === "sfx")', game)
        self.assertIn("ITP.delay", game[game.index('if(d.t === "sfx")'):][:200])            # с задержкой интерполяции робота

    def test_relay_wired_in_server(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent / "webserver.py").read_text(encoding="utf-8")
        self.assertIn('elif t == "sfx":', src)
        self.assertIn("skillfx.clean(d, info, time.time())", src)
        self.assertIn('"sfx"', src[src.index("WS_TYPES = {"):src.index("}", src.index("WS_TYPES = {"))])


if __name__ == "__main__":
    unittest.main()
