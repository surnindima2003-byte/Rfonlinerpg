"""Переводы нового контента: классы и умения, Кровавая башня, мировой босс, Chip War, магазин и предметы.

Игра берёт язык из Telegram, поэтому всем, у кого он не русский, интерфейс показывается на английском
(или украинском, испанском, турецком, португальском). Раньше в этих разделах оставался русский
или смесь вроде «Damage в радиусе 130, stun на 3 s». Проверка идёт тем же механизмом перевода, что в игре
(словарь I18N и функция T из game.html), поэтому нужен node.
Запуск: python -m unittest test_i18n_content
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GAME = (ROOT / "game.html").read_text(encoding="utf-8")
LANGS = ("en", "uk", "es", "tr", "pt")


def block(start, end="\n};"):
    a = GAME.index(start)
    return GAME[a:GAME.index(end, a)]


def data_strings():
    """Русские названия и описания из таблиц игры, которые видит игрок."""
    out = []
    out += re.findall(r'(?:name|role|desc|lore):"([^"]+)"', block("const CLASSES = {"))
    out += re.findall(r'(?:name|desc):"([^"]+)"', block("const PASSIVES = [", "\n];"))
    for t in ("RUNES", "DRONES", "ARTS", "WINGS", "CLOAKS"):
        out += re.findall(r'name:"([^"]+)"', block(f"const {t} = {{"))
    out += re.findall(r'(?:n|m):"([^"]+)"', block("const RARITY = [", "\n];"))
    out += re.findall(r'n:"([^"]+)"', block("const STAT_META = {"))
    out += re.findall(r'\["\w+", "([^"]+)", \d+\]', block("const WB_DROP = [", "];"))
    packs = block("const PACKS = [", "\n];")
    out += re.findall(r'SP\("\w+", "([^"]+)"', packs) + re.findall(r'(?:name|desc):"([^"]+)"', packs)
    tabs = GAME[GAME.index('const tabs = [["packs", "Паки"]'):]
    out += re.findall(r'\["\w+", "([^"]+)"\]', tabs[:tabs.index("];")])
    out += re.findall(r':"([^"]+)"', block("const FAC_INFO = {", "};"))
    return [s for s in dict.fromkeys(out) if re.search("[А-Яа-яЁё]", s)]


# тексты окон и уведомлений в том виде, в каком их собирает игра (с числами)
SCREENS = [
    # навыки Ремонтника
    "Лечение · Ур. 1/10", "Лечение цели: 16% её прочности · дальность 380", "Облако (радиус 150): 3,5% прочности в секунду · 6 сек",
    "Щит: −30% входящего урона · 5 сек", "Разряд: 150% атаки · 50% урона уходит на лечение", "Шанс успеха 30% · при неудаче книги сгорают",
    "Умения класса «Призрак». Каждый уровень усиливает эффект и сокращает перезарядку на 2%.", "Книга: Фазовый разрез", "Классовые: Глифоносец",
    # Кровавая башня
    "2 ч 59 мин", "До открытия · 20:30 МСК", "Записалось: 4 · Попыток сегодня: 1/1", "Правила:", "• Только с 10 уровня", "Награды:",
    "⭐ Опыт с монстров в Башне", "👑 Ядра — победителю", "Вы в очереди", "Ждём игроков", "Босс: 300 000", "Идёт забег",
    "Отменить запись", "Записаться", "Войти в башню", "Ты выбыл из забега", "барьер: 12", "проход открыт", "⚠ БОСС · Багровый Страж",
    "🩸 Кровавая башня: старт! Прорвись через монстров к боссу", "👑 Ты победил в Кровавой башне! +40 ядер",
    "Босс башни повержен. Победитель: Nova · +10 ядер, если бил босса", "Забег окончен: босс устоял", "Не удалось записаться",
    # мировой босс
    "1 д 2 ч 5 мин", "До следующего босса", "⚔ Босс в ангаре!", "В ангар", "Расписание:",
    "понедельник, среда, пятница, воскресенье — в 20:00 по Москве.", "Полный дроп с одного убийства:", "КРИТ 1234", "⚠ БОСС · Владыка Ржавчины",
    # Chip War и выбор фракции
    "Chip War: счёт", "Chip War через 4:59", "Chip War через 5 минут — Железный каньон", "⚔ Chip War началась: удержи чип в Железном каньоне",
    "Chip War началась! Чип в центре Железного каньона. Минимальный уровень: 10", "Chip War: победа Vex Industries — бонус твоей фракции на сутки!",
    "Chip War окончена вничью", "Чип держит Aegis Dynamics", "· до конца 9:59", "Чип оспаривается", "Чип свободен", "Следующая битва:",
    "Бонус победителя у CoreForge: +15% к шансу ценного лута и +10% опыта до 04:00", "Очков для победы:", "Выбрать фракцию",
    "Выбери фракцию", "Выбор постоянный: сменить фракцию потом нельзя", "Фракция: Aegis Dynamics", "Chip War: получено 600 лома",
    # магазин
    "Магазин GRAM", "Покупки повышают VIP", "Скидка на «Паки» и «Допы»", "Наборы усиления — по обычной цене", "Дрон «Нова»",
    "+6% атаки · +3% крита · +5% опыта · сбор лута 340", "Скорость бега +8% · Прочность +10% · Атака +5% · Ремонт +3/с",
    "Резонанс: Призрак, Снайпер", "Резонанс: все классы", "Руна войны ×2", "Набор II",
    # экран гибели робота
    "Робот уничтожен", "возврат в ангар…",
]


def run_node(strings):
    lines = [l for l in GAME.split("\n") if l.startswith(("const I18N = {", "Object.assign(I18N, "))]
    a, b = GAME.index("const LANG_IDX = "), GAME.index("\n\n", GAME.index("function T(s){"))
    js = ("\n".join(lines) + "\n" + GAME[a:b] + "\nbuildTrRe();\n"
          "const LIST = " + json.dumps(strings, ensure_ascii=False) + ";\nconst out = {};\n"
          "for (const l of " + json.dumps(LANGS) + ") { LANG = l; trMemo.clear(); out[l] = LIST.map(s => T(s)); }\n"
          "out.bad = Object.entries(I18N).filter(([k, v]) => !Array.isArray(v) || v.length !== 5 || v.some(x => typeof x !== 'string' || !x)).map(([k]) => k);\n"
          "process.stdout.write(JSON.stringify(out));")
    try:
        res = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=120, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        raise unittest.SkipTest(f"нужен node: {e}")
    return json.loads(res.stdout)


class NewContentIsTranslated(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = data_strings()
        cls.strings = cls.data + SCREENS
        cls.res = run_node(cls.strings)

    def untranslated(self, lang):
        russian = re.compile("[ыэъёЫЭЪЁ]" if lang == "uk" else "[А-Яа-яЁё]")      # в украинском этих букв нет
        return [f"{s!r} → {t!r}" for s, t in zip(self.strings, self.res[lang]) if russian.search(t)]

    def test_enough_strings_checked(self):
        self.assertGreater(len(self.data), 200)                    # классы, умения, руны, дроны, крылья, плащи, паки…

    def test_every_language(self):
        for lang in LANGS:
            with self.subTest(lang=lang):
                self.assertEqual(self.untranslated(lang), [])

    def test_no_half_translated_units(self):
        en = dict(zip(self.strings, self.res["en"]))
        self.assertEqual(en["Скорость бега +8% · Прочность +10% · Атака +5% · Ремонт +3/с"],
                         "Move speed +8% · Hull +10% · Attack +5% · Repair +3/s")       # раньше «Repair +3/from»
        self.assertEqual(en["2 ч 59 мин"], "2 h 59 min")
        self.assertEqual(en["Проход сквозь цель с ×2,2 урона и выход за её спиной; по целям ниже 50% прочности — гарантированный крит"],
                         "Dash through the target for ×2.2 damage and come out behind it; guaranteed crit on targets below 50% hull")

    def test_dictionary_is_well_formed(self):
        self.assertEqual(self.res["bad"], [])                      # у каждой фразы ровно 5 переводов


class ScreensBuildTranslatableText(unittest.TestCase):
    def has(self, fragment):
        self.assertTrue(fragment in GAME, f"в game.html нет: {fragment}")     # без вывода всего файла при ошибке

    def test_times_have_spaces_for_units(self):
        self.has('${hh} ч ${mm} мин')                                # мировой босс
        self.has('${hh} ч ${Math.floor(left%36e5/6e4)} мин')         # башня
        self.assertIsNone(re.search(r"\$\{hh\}ч|\}м</b>", GAME))

    def test_canvas_labels_go_through_T(self):
        self.has('ctx.fillText(T(open ? "проход открыт" : "барьер: "')

    def test_chip_war_rules_have_no_numbers_inside_sentences(self):
        w = block("function openChipWar(){", "\n}\n")
        self.assertIn("Очков для победы: <b>${st.target}</b> · Минимальный уровень: <b>${st.min}</b>", w)
        self.assertNotIn("Побеждает первая фракция с ${st.target}", w)


if __name__ == "__main__":
    unittest.main()
