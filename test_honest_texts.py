"""Тексты игры совпадают с тем, как она работает на самом деле.

Раньше игра обещала то, чего нет: титановая сфера «сохраняет уровень» (на деле заточка −1), события
с расписанием, которых не существует, «скидка до конца сезона» с таймером, который каждый месяц
начинался заново, курс «1 GRAM = $1.55» независимо от настройки сервера и 4 класса в руководстве при 7 в игре.
Запуск: python -m unittest test_honest_texts
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GAME = (ROOT / "game.html").read_text(encoding="utf-8")
GUIDE = (ROOT / "guide.html").read_text(encoding="utf-8")


def block(src, start, end):
    a = src.index(start)
    return src[a:src.index(end, a)]


def i18n():
    """Словарь переводов: та же склейка, что в game.html (const I18N + все Object.assign) — через node."""
    lines = [l for l in GAME.split("\n") if l.startswith(("const I18N = {", "Object.assign(I18N, "))]
    js = "\n".join(lines) + "\nprocess.stdout.write(JSON.stringify(I18N));"
    try:
        out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=60, check=True).stdout   # через stdin: словарь больше лимита аргумента
    except (OSError, subprocess.CalledProcessError) as e:
        raise unittest.SkipTest(f"нужен node: {e}")
    return json.loads(out)


class TitaniumSphere(unittest.TestCase):
    def test_server_and_client_lower_enchant_by_one(self):
        items = (ROOT / "items.py").read_text(encoding="utf-8")
        self.assertIn('elif sid == "sph_ti":\n                vals, result = ({"e": e0 - 1} if e0 > 0 else {}), "fail"', items)
        self.assertIn("x.e = Math.max(0, e - 1)", GAME)

    def test_texts_say_so(self):
        desc = re.search(r'sph_ti:\{name:"[^"]+", type:"ench"[^}]*desc:"([^"]+)"', GAME).group(1)
        self.assertIn("снижается на 1", desc)
        card = block(GAME, '["sph_ti", "Безопасная заточка"', "]]")
        self.assertIn("снизится на 1", card)
        for wrong in ("сохраняет уровень", "пропадёт только попытка", "Было: остался"):
            self.assertNotIn(wrong, GAME)
        self.assertNotIn("сохраняет и предмет, и текущий уровень", GUIDE)
        self.assertIn("заточка снижается на 1", GUIDE)

    def test_enchant_gain_is_per_level_not_flat(self):
        self.assertNotIn("ENCH_STEP", GAME)                                   # «+8% за уровень» — неправда
        self.assertIn("ENCH_GAIN[e]*100", block(GAME, "function renderEnchant", "function doEnchant"))
        self.assertNotIn("добавляет 8%", GUIDE)


class Events(unittest.TestCase):
    def events(self):
        src = block(GAME, "const EVENTS = [", "\n];") + "\n]"
        try:
            out = subprocess.run(["node", "-"], input="process.stdout.write(JSON.stringify(" + src[len("const EVENTS = "):] + "))",
                                 capture_output=True, text=True, timeout=30, check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            raise unittest.SkipTest(f"нужен node: {e}")
        return {e["id"]: e for e in json.loads(out.stdout)}

    def test_every_event_is_real_or_marked_soon(self):
        ev = self.events()
        dispatch = block(GAME, 'else if(d.ev === "boss"', "else if(d.evback)")
        for eid, e in ev.items():
            if e.get("soon"):
                self.assertNotIn("when", e, eid)                              # без выдуманного расписания
            elif not e.get("play"):
                self.assertIn(f'd.ev === "{eid}"', dispatch, eid)            # у настоящего события — своё окно
        self.assertEqual({k for k, e in ev.items() if not e.get("soon")}, {"fear", "tower", "boss", "chipwar"})

    def test_schedules_match_server(self):
        ev = self.events()
        wb = (ROOT / "worldboss.py").read_text(encoding="utf-8")
        tw = (ROOT / "tower.py").read_text(encoding="utf-8")
        self.assertIn("DAYS = (0, 2, 4, 6)", wb)
        self.assertIn("HOUR = 20\n", wb)
        self.assertEqual(ev["boss"]["when"], "Пн, Ср, Пт, Вс в 20:00 МСК")
        self.assertIn("OPEN_H, OPEN_M, REG_SEC, RUN_SEC = 20, 30,", tw)
        self.assertEqual(ev["tower"]["when"], "Ежедневно в 20:30 МСК")
        self.assertNotIn("when", ev["chipwar"])                               # время Chip War присылает сервер
        self.assertIn("mskWhen(st.next)", block(GAME, "function cwWhen", "\n}\n"))

    def test_menu_opens_events_with_live_banners(self):
        # пункт меню берёт openEvents в момент вызова: плашки «босс в ангаре» и «запись в башню» не теряются
        self.assertIn("events:() => openEvents()", GAME)


class ShopAndRate(unittest.TestCase):
    def test_no_fake_sale_deadline(self):
        for wrong in ("до конца сезона:", "saleLeft", "saleEnd", "saleCd"):
            self.assertNotIn(wrong, GAME)

    def test_rate_comes_from_server(self):
        gram = (ROOT / "gram.py").read_text(encoding="utf-8")
        self.assertIn('"gram_usd": GRAM_USD', gram)
        self.assertIn("gramUsd:r.gram_usd || G.gramUsd", GAME)
        self.assertNotIn("Курс: 1 GRAM = $1.55", GAME)


class Guide(unittest.TestCase):
    def test_all_classes_listed(self):
        names = re.findall(r'^  (\w+):\{name:"([^"]+)", role:', block(GAME, "const CLASSES = {", "\n};"), re.M)
        self.assertEqual(len(names), 7)
        cards = re.findall(r'<article class="class"[^>]*>.*?<h3>([^<]+)</h3>', GUIDE)
        self.assertEqual(sorted(cards), sorted(n for _, n in names))
        self.assertIn("<b>7</b><span>боевых классов</span>", GUIDE)

    def test_gear_count(self):
        fams = re.findall(r'^  (\w+):\{slot:"', block(GAME, "const GEAR_FAM = {", "\n};"), re.M)
        extra = len(re.findall(r'ITEMS\["g_(?:phaseblades|glaive|emitter)_" \+ T\.n\]', GAME))
        tiers = len(re.findall(r"^  \{n:\d+, lvl:\d+", block(GAME, "const TIERS = [", "\n];"), re.M))
        self.assertIn(f"<b>{(len(fams) + extra) * tiers}</b><span>видов снаряжения</span>", GUIDE)

    def test_chip_war_rewards(self):
        cw = (ROOT / "chipwar.py").read_text(encoding="utf-8")
        self.assertIn('"cores": 5 if won else 0', cw)                        # ядра — только победившей фракции
        self.assertIn("а пилоты победившей фракции — ещё и ядра", GUIDE)
        self.assertIn("а пилоты победившей фракции — ещё и ядра", GAME)


class Translations(unittest.TestCase):
    def test_changed_strings_are_translated(self):
        tr = i18n()
        must = ["Безопасная заточка: при неудаче предмет не ломается, а заточка снижается на 1",
                "Редкая титановая сфера. Если заточка не удастся — предмет останется целым, но заточка снизится на 1.",
                "Титановая сфера сохранила предмет, но заточка снизилась:", "До +3 заточка безопасна для любых сфер.",
                "Следующий уровень даёт", "Курс: 1 GRAM =", "Ежедневно в 20:30 МСК", "Пн, Ср, Пт, Вс в 20:00 МСК",
                "Битва фракций за чип в Железном каньоне", "Скоро", "В разработке", "МСК",
                "Расписание объявим, когда событие откроется."]
        for k in must:
            self.assertIn(k, tr, k)
            self.assertEqual(len(tr[k]), 5, k)
            self.assertTrue(all(isinstance(v, str) and v for v in tr[k]), k)
        for gone in ("Безопасная заточка: при неудаче предмет не ломается и сохраняет уровень", "Курс: 1 GRAM = $1.55, 1 ⭐ =",
                     "Пн, Чт, Сб в 20:00", "Вт и Вс в 22:00", "Все пилоты сервера атакуют огромного босса. Награды по вкладу в урон."):
            self.assertNotIn(gone, tr, gone)                                  # старые неправдивые фразы не висят в словаре


if __name__ == "__main__":
    unittest.main()
