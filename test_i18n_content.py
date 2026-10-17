"""Переводы нового контента: классы и умения, Кровавая башня, мировой босс, Chip War, магазин и предметы,
а также квесты, кодекс, сезон, вкладки персонажа, Мастерская, торговец и склад.

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
    shop = block("const SHOP_GROUPS = [", "\n];")              # вкладки магазина и разделы внутри них
    out += re.findall(r'name:"([^"]+)"', shop) + re.findall(r'\["\w+", "([^"]+)"\]', shop)
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
    "1 д 2 ч 5 мин", "До следующего босса", "⚔ Босс в логове!", "К боссу", "Расписание:",
    "понедельник, среда, пятница, воскресенье — в 20:00 по Москве.", "Полный дроп с одного убийства:", "КРИТ 1234", "⚠ БОСС · Владыка Ржавчины",
    # Chip War и выбор фракции
    "Chip War: счёт", "Chip War через 4:59", "Chip War через 5 минут — Железный каньон", "⚔ Chip War началась: удержи чип в Железном каньоне",
    "Chip War началась! Чип в центре Железного каньона. Минимальный уровень: 10", "Chip War: победа Vex Industries — бонус твоей фракции на сутки!",
    "Chip War окончена вничью", "Чип держит Aegis Dynamics", "· до конца 9:59", "Чип оспаривается", "Чип свободен", "Следующая битва:",
    "Бонус победителя у CoreForge: +15% к шансу ценного лута и +10% опыта до 04:00", "Очков для победы:", "Выбрать фракцию",
    "Выбери фракцию", "Выбор постоянный: сменить фракцию потом нельзя", "Фракция: Aegis Dynamics", "Chip War: получено 600 лома",
    # магазин
    "Магазин GRAM", "Покупки повышают VIP", "Дрон «Нова»",
    "+6% атаки · +3% крита · +5% опыта · сбор лута 340", "Скорость бега +8% · Прочность +10% · Атака +5% · Ремонт +3/с",
    "Резонанс: Призрак, Снайпер", "Резонанс: все классы", "Руна войны ×2", "Набор II",
    # экран гибели робота
    "Робот уничтожен", "возврат в ангар…", "Ремонт: −3\u00a0703 лома (3%)",
    # портал сезонной зоны
    "Сезонная зона", "Портал сезона", "Войти в сезонную зону", "Зона для владельцев билета сезона: больше опыта и дропа.",
    "🔒 Вход только с билетом сезона. Билет даёт ×2 опыт, +60% к дропу и премиум-награды пропуска.", "🎫 Купить билет · 15 GRAM",
    # «Страх»: окно события, отсчёт, волны, выход и монстры
    "Попыток на сегодня", "Войдите, чтобы начать", "Новые попытки — в 00:00 МСК", "Можно начать", "Попытки закончились", "Нет попыток",
    "Когда", "Игроки", "Соло", "Попытки", "Как это работает", "2 попытки в день, тратится при входе",
    "Монстры приходят волнами по 20, уровень волны растёт с каждой", "Дойдите до волны 59, чтобы пройти испытание целиком",
    "Смерть выбрасывает из подземелья в центральный ангар", "Монстры появятся через", "Волна 7 из 59", "Испытание пройдено!",
    "👑 Испытание «Страх» пройдено: все 59 волн!", "Пройдено волн: 12 из 59", "В «Страх» входят через События", "«Страх» открывается с 10 уровня",
    "«Страх»: попытки на сегодня закончились", "Выход из подземелья", "Забег закончится, а попытка не вернётся.", "Покинуть подземелье",
    "Слабый Краулер страж", "Закалённый Краулер воин", "Сумеречный Дрон повелитель", "Непобедимый Дрон титан",
    # мировой босс в своём логове
    "Логово Владыки", "Мировой босс · PvP-зона", "⚔ К боссу", "Вернуться к боссу — кнопка «⚔ К боссу» в ангаре", "Мировой босс ушёл — возврат в ангар",
    "Мировой босс сейчас не в логове. Расписание — в Событиях", "⚔ Добыча мирового босса", "Логово Владыки · прочность босса 63% · PvP-зона",
    "Босс повержен — добыча ещё лежит в логове", "Ты уже в логове", "🏆 Босс повержен", "В логово", "👑 Мировой босс сейчас в логове",
    "Босс появляется в своём логове. Вход — через События или портал в ангаре, пока босс жив.",
    "В логове PvP: другие пилоты могут атаковать. Ремонт после гибели бесплатный.", "Погиб — возвращайся сразу: кнопка «⚔ К боссу» в ангаре.",
    "⚠ Мировой босс «Владыка Ржавчины» появился в своём логове! Вход — портал в ангаре или События", "🏆 Владыка Ржавчины повержен! Добыча лежит в логове",
    "Мировой босс «Владыка Ржавчины» ушёл непобеждённым", "Ты в локации: Логово Владыки",
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


# ===================== шаг 6: задания, кодекс, сезон, персонаж, Мастерская, торговец, склад =====================
NB = " "           # так toLocaleString("ru-RU") разделяет тысячи


def step6_data():
    """Строки этих экранов в том виде, в каком их собирает игра: из таблиц game.html и заданий сезона на сервере."""
    out = []
    # сюжетные квесты: название и условие (как в qText)
    q_pl = dict(re.findall(r'(\w+):"([^"]+)"', block("const Q_PL = {", "};")))
    q_where = dict(re.findall(r'(\w+):"([^"]*)"', block("const Q_WHERE = {", "};")))
    locs = dict(re.findall(r'^\s*(\w+):\{name:"([^"]+)"', block("const LOCS = {"), re.M))
    story = block("const STORY = [", "\n];")
    quests = 0
    for name, mob, n, where in re.findall(r'qk\("([^"]+)", "(\w+)", (\d+), "(\w+)"', story):
        out += [name, f"Убей {n} {q_pl[mob]}{q_where[where]}"]; quests += 1
    for f, text in (("ql", "Достигни {} уровня"), ("qe", "Заточи предмет до +{}"), ("qb", "Купи {} ремкомплектов у торговца")):
        for name, n in re.findall(f + r'\("([^"]+)", (\d+)', story):
            out += [name, text.format(n)]; quests += 1
    for name, loc in re.findall(r'\{name:"([^"]+)", t:"loc", loc:"(\w+)"', story):
        out += [name, f"Войди в локацию «{locs[loc]}»"]; quests += 1
    for name, where, n in re.findall(r'\{name:"([^"]+)", t:"boss", where:"(\w+)", n:(\d+)', story):
        out += [name, f"Победи {n} главарей{q_where[where]}" if int(n) > 1 else f"Победи главаря{q_where[where]}"]; quests += 1
    for name, n in re.findall(r'\{name:"([^"]+)", t:"pvp", n:(\d+)', story):
        out += [name, f"Победи {n} пилотов в PvP"]; quests += 1
    expected = sum(story.count(f) for f in ("qk(", "ql(", "qe(", "qb(")) + story.count("{name:")
    assert quests == expected, f"разобрано {quests} сюжетных квестов из {expected}: поменялся формат STORY"
    # наборы кодекса: «Тема: поколение грейд №N», «Боевой комплект класса: поколение комплект, слот» и т. д.
    tiers = re.findall(r'"([^"]+)"', block("const CX_TIER_RU = [", "];"))
    grades = re.findall(r'name:"([^"]+)"', block("const GRADES = [", "\n];"))
    slots = re.findall(r':"([^"]+)"', block("const CX_SLOT_RU = {", "};"))
    gen = re.findall(r':"([^"]+)"', block("const CX_CLS_GEN = {", "};"))
    themes = re.findall(r'\["([^"]+)", "\w+", "\w+"\]', block("const CX_PAIR_THEME = [", "];"))
    trio = re.findall(r'\["([^"]+)", \["', block("const TRIO = [", "];"))
    for t in tiers:
        for g in grades:
            out += [f"{th}: {t} {g.lower()} №{n}" for th in themes for n in (1, 10)]
            out += [f"Полный доспех: {t} {g.lower()}", f"Ядро и модуль: {t} {g.lower()}"] + [f"{x}: {t} {g.lower()}" for x in trio]
        out += [f"Боевой комплект {c}: {t.lower()} комплект, {s}" for c in gen for s in slots]
    out += [f"{th}: {g} №{n}" for th in re.findall(r'\["([^"]+)", "\w+"\]', block("const CODEX_T = [", "];")) for g in grades for n in (1, 3)]
    # снаряжение всех поколений, в том числе оружие Призрака, Глифоносца и Ремонтника
    adj = re.findall(r'adj:\["([^"]+)", "([^"]+)", "([^"]+)"\]', block("const TIERS = [", "\n];"))
    fam = re.findall(r'noun:"([^"]+)", g:(\d)', block("const GEAR_FAM = {", "\n};"))
    extra = re.findall(r'name:T\.adj\[(\d)\] \+ " ([^"]+)"', GAME)
    for a in adj:
        out += [a[int(g)] + " " + noun for noun, g in fam] + [a[int(g)] + " " + noun for g, noun in extra]
    # таблицы: улучшения, осколки, предметы торговца, спецквесты, сокращения характеристик
    out += re.findall(r'(?:name|desc):"([^"]+)"', block("const UPGRADES = [", "\n];"))
    out += re.findall(r'(?:name|lore):"([^"]+)"', block("const SHARD = {", "\n};"))
    out += re.findall(r'(?:name|desc):"([^"]+)"', block("const ITEMS = {", "\n};"))
    out += re.findall(r'(?:name|sub):"([^"]+)"', block("const SPECIAL_Q = [", "\n];"))
    out += re.findall(r':"([^"]+)"', block("const STAT_SHORT = {", "};"))
    # задания сезона приходят с сервера по-русски (seasonpts.TASKS)
    src = (ROOT / "seasonpts.py").read_text(encoding="utf-8")
    tasks = src[src.index("TASKS = ["):src.index("\n]", src.index("TASKS = ["))]
    out += re.findall(r'\("\w+", "\w+", "([^"]+)"', tasks)
    return [s for s in dict.fromkeys(out) if re.search("[А-Яа-яЁё]", s)]


SCREENS6 = [
    # квесты
    "Сюжетный", "Специальный", "Ежедневные", "Этаж 2 · ", "откроется после этажа 1", "0/15 выполнено", "✓ Выполнено", "Проверяем…",
    "Все сюжетные квесты выполнены. Новые этажи — в следующих обновлениях", "✓ Проверено! +10 ядер", "📜 Квест «Охотник II» выполнен: ★2 400 XP · ⚙1 200",
    "Вступи или подпишись по кнопке «Перейти», вернись в игру и нажми «Проверить» — бот проверит и выдаст награду",
    # кодекс
    "Все классы", "Все редкости", "Без класса", f"Показано 40 из 2{NB}048", "Показать ещё 40", "+0,2 АТК", "+1,5 ПРОЧ", "+0 АТК · +0 ЗАЩ · +0 ПРОЧ · +0% КРИТ",
    # сезон
    "Задания, рейтинг и пропуск сезона", "Очки сезона", "Пропуск", "Уровень пропуска", "Опыт пропуска",
    "Опыт пропуска — за победы над врагами. Он открывает награды уровней и на рейтинг не влияет.",
    "Очки сезона — за задания. По ним место в рейтинге и призы в USDT.", "Место — по очкам сезона. Призы в USDT получают первые 20 пилотов.",
    "Уровень пропуска: 5", "+5 опыта пропуска", "Настройки", "До конца сезона · 1-е число, 18:00 МСК", "📈 Постоянно", "☀️ Ежедневные", "📅 Еженедельные",
    "сброс в 00:00 МСК", "сброс в понедельник", "Очки начисляются сами, как только задание выполнено — кнопка не нужна.", f"9{NB}800 очк.",
    f"· 1{NB}234 очков", "🏆 Приз сезона 2026-09: 2 место", "Пока в рейтинге 3 пилот(ов) — выполняй задания сезона", "🏆 +5 очков сезона",
    f"0 / 300 опыта до 1 уровня · 1 враг = 1 опыт, главарь = 20",
    # персонаж
    "Стенд пилота", "Отсек поддержки", "Не выпущен", "нажми, чтобы выбрать", "Не надеты", "(нажми для экипировки)", "Дрон, пусто", "Сенсор, пусто",
    "Авто: ВКЛ", "Авто: ВЫКЛ", "✓ Активно · осталось ~7 мин", "Характеристики", "Все бонусы", "VIP 1: +5% · гильдия +6%", "🎁 Доп. шанс дропа",
    "Сбросить улучшения · 3 ядра", "Вернётся очков навыка: 0. Потраченный лом не возвращается.", "Получено за уровни: 31",
    "Дрон не выбран", "Дронов в ангаре: 1 из 8", f"Собрать в Мастерской · с 10 ур. · 15{NB}000 лома, 50 ядер, 10 провода, 10 бронепластина",
    "Только в магазине GRAM · 15 GRAM", "Все артефакты (1 из 8)", "Своих плащей: 1 · виден всем игрокам", "Мастерская · с 15 ур.", "Есть ×2",
    "Уже стоит в 2 вещах из 3", "Больше 3 таких рун не действуют", "Новый дрон: «Искра» — уже летает рядом", "Дрон «Искра» в деле",
    "Умение улучшено до ур. 3!", "Нужен 15 уровень", "Нужен ур. 25",
    # Мастерская и Кузнец
    "сет «Цитадель»", "Сет «Архонт»", "Осколки выпадают с врагов от 15 уровня и продаются на маркете. Магматический осколок, Солнечный осколок, Криоосколок.",
    "Обычная руна · Атака +2% · вставляется в гнездо вещи", "Легендарный артефакт · Опыт +12% · надевается в слот артефакта",
    "Скорость бега +4% · надеваются в слот крыльев", "Броня +4% · надевается в слот плаща", "⚡ Резонанс с твоим классом: ×1,2",
    "Собрано: Руна ярости. Вставь её в вещь: Персонаж → вещь → гнездо", "Руна «Руна ярости» вынута", "Нет места: руна «Руна ярости» потеряна",
    # торговец, телепорт, склад
    "5 ⚙ за штуку", "Цена:", "Куплено: ×10 Зелье силы", "Телепорт в центральный ангар (каст 7 сек)", "Телепорт в ангар через 7 с. Не получай урон!",
    "В бою с игроком телепорт недоступен ещё 12 с", "Телепорт 3.4с", "5 с", "Хранение предметов (200 ячеек)", "В хранилище сложено: 25 шт. Не поместилось: 3",
    "Хранилище пустое. Положи сюда то, что не хочешь потерять: хранилище не теряется при гибели", "⇄ за GRAM можно продать 3 шт.",
    # распродажа в магазине GRAM
    "Распродажа · конец: Sun 23:59 МСК", "1 д 2 ч", "3 ч 12 мин", "4 мин 30 сек", "🔥 Распродажа в магазине GRAM: −30% · «Паки», «Допы»",
    "Цена изменилась: теперь 45,5 GRAM", "Магазин обновился: закрой игру и открой её снова",
]


class RemainingScreensAreTranslated(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = step6_data()
        cls.strings = cls.data + SCREENS6
        cls.res = run_node(cls.strings)
        cls.tr = {l: dict(zip(cls.strings, cls.res[l])) for l in LANGS}

    def test_enough_strings_checked(self):
        self.assertGreater(len(self.data), 1000)                   # квесты, наборы кодекса, снаряжение, задания сезона…

    def test_every_language(self):
        for lang in LANGS:
            russian = re.compile("[ыэъёЫЭЪЁ]" if lang == "uk" else "[А-Яа-яЁё]")
            with self.subTest(lang=lang):
                self.assertEqual([f"{s!r} → {t!r}" for s, t in self.tr[lang].items() if russian.search(t)], [])

    def test_numbers_inside_phrases(self):
        en, tr = self.tr["en"], self.tr["tr"]
        self.assertEqual(en["Убей 100 стражей в подземелье"], "Kill 100 sentries in a dungeon")
        self.assertEqual(en[f"Показано 40 из 2{NB}048"], f"Showing 40 of 2{NB}048")
        self.assertEqual(tr[f"Показано 40 из 2{NB}048"], f"2{NB}048 setten 40 tanesi gösteriliyor")    # числа переставлены
        self.assertEqual(en["Мастерская · с 15 ур."], "Workshop · lv. 15+")                          # раньше «from 15 lv.»
        self.assertEqual(en[f"Собрать в Мастерской · с 10 ур. · 15{NB}000 лома, 50 ядер, 10 провода, 10 бронепластина"],
                         f"Craft in the Workshop · lv. 10+ · 15{NB}000 scrap, 50 cores, 10 wires, 10 armor plates")
        self.assertEqual(en["Все классы"], "All classes")                                           # раньше «All классы»
        self.assertEqual(en["⚡ Резонанс с твоим классом: ×1,2"], "⚡ Resonance with your class: ×1,2")   # раньше «Резонанс from твоим»

    def test_codex_names_exist_for_every_class(self):
        classes = re.findall(r'^  (\w+):\{name:"', block("const CLASSES = {"), re.M)
        gen = re.findall(r'(\w+):"', block("const CX_CLS_GEN = {", "};"))
        self.assertEqual(sorted(gen), sorted(classes))               # раньше у новых классов было «Боевой комплект : …»

    def test_story_quest_names_match_targets(self):
        for mob in re.findall(r'qk\("Страж падёт(?: I+)?", "(\w+)"', block("const STORY = [", "\n];")):
            self.assertEqual(mob, "sentry_bot")                       # «Страж падёт» на первом этаже просил дронов


class PlayerContentIsNotTranslated(unittest.TestCase):
    """Ники, названия гильдий и их описания — текст игроков: «Призрак» не должен превращаться в «Ghost»."""
    def has(self, fragment):
        self.assertTrue(fragment in GAME, f"в game.html нет: {fragment}")

    def test_guilds(self):
        self.has('<b data-notr><span class="tag">[${esc(g.tag)}]</span>${esc(g.name)}</b>')         # список гильдий
        self.has('${g.desc ? `<p data-notr>${esc(g.desc)}</p>` : ""}</div>${btn}</div>')
        self.has('<h2 data-notr>[${esc(g.tag)}] ${esc(g.name)}</h2>')                                 # шапка своей гильдии
        self.has('<b data-notr style="color:${(FACTIONS[m.fac] || FACTIONS.aegis).color}">${esc(m.nick)}</b>')
        self.has('<div class="mem"><div class="tx"><b data-notr>${esc(r.nick)}</b>')                   # заявки
        self.has('$("shTitle").setAttribute("data-notr", "")')                                        # окно участника: ник в заголовке
        self.has('$("shTitle").removeAttribute("data-notr")')                                         # остальные окна переводятся

    def test_season_rating_and_market(self):
        self.has('<div class="pod-n" data-notr>')
        self.has('<span>${w.place}</span><b data-notr>${esc(w.nick)}</b>')
        self.has('`<span data-notr>${["🥇", "🥈", "🥉"][w.place - 1]} ${esc(w.nick)}')
        self.has('<small class="mk-seller" data-notr>@${esc(lot.seller || S.name)}</small>')             # «@ник» продавца на маркете


class ScreensBuildTranslatableText(unittest.TestCase):
    def has(self, fragment):
        self.assertTrue(fragment in GAME, f"в game.html нет: {fragment}")     # без вывода всего файла при ошибке

    def test_times_have_spaces_for_units(self):
        self.has('${hh} ч ${mm} мин')                                # мировой босс
        self.has('${hh} ч ${Math.floor(left%36e5/6e4)} мин')         # башня
        self.assertIsNone(re.search(r"\$\{hh\}ч|\}м</b>", GAME))

    def test_canvas_labels_go_through_T(self):
        self.has('ctx.fillText(T(open ? "проход открыт" : "барьер: "')
        self.has('const txt = T("Телепорт " + Math.max(0, TP_CAST - (nowS - TP.t0)).toFixed(1) + "с");')   # каст камня телепортации
        self.has('ctx.fillText(T(Math.ceil(left) + " с"), x, y - 14);')                                    # таймер руны возврата (drawRuneMark)

    def test_chip_war_rules_have_no_numbers_inside_sentences(self):
        w = block("function openChipWar(){", "\n}\n")
        self.assertIn("Очков для победы: <b>${st.target}</b> · Минимальный уровень: <b>${st.min}</b>", w)
        self.assertNotIn("Побеждает первая фракция с ${st.target}", w)


if __name__ == "__main__":
    unittest.main()
