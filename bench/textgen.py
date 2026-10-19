"""Детерминированный «стог сена»: связный, но бессодержательный текст заданной длины.

Генерируется из словарей по seed — без внешних корпусов, одинаково при каждом запуске,
поэтому результаты разных моделей сравнимы. Длина задаётся в токенах через
калибровку `chars_per_token` (см. calibrate в runner.py).
"""

import random

RU = {
    "subj": ["Совет директоров", "Отдел логистики", "Городская библиотека", "Метеостанция", "Небольшая пекарня",
             "Команда инженеров", "Школьный оркестр", "Местный музей", "Фермерское хозяйство", "Служба поддержки",
             "Исследовательская группа", "Строительная бригада", "Редакция журнала", "Портовая администрация"],
    "verb": ["обсудил", "подготовила", "пересмотрела", "зафиксировала", "проверил", "согласовала", "описала",
             "запланировала", "сравнила", "отложила", "обновила", "уточнила"],
    "obj": ["график поставок", "отчёт о погоде", "план ремонта", "список закупок", "расписание экскурсий",
            "бюджет на квартал", "схему маршрутов", "правила хранения", "каталог экспонатов", "инструкцию по технике",
            "порядок дежурств", "смету работ"],
    "tail": ["на следующую неделю", "после долгого обсуждения", "с учётом пожеланий жителей", "без особых изменений",
             "по просьбе руководства", "в рамках ежегодной проверки", "несмотря на плохую погоду",
             "к началу сезона", "по итогам прошлого месяца", "для внутреннего пользования"],
    "extra": ["Вопросов ни у кого не возникло.", "Решение приняли единогласно.", "Подробности опубликуют позже.",
              "Это заняло почти весь день.", "Все остались довольны результатом.", "Работа продолжится завтра."],
}
EN = {
    "subj": ["The board", "The logistics team", "The city library", "A weather station", "A small bakery",
             "The engineering crew", "The school orchestra", "The local museum", "A family farm", "Customer support",
             "The research group", "The harbour office"],
    "verb": ["discussed", "prepared", "reviewed", "recorded", "checked", "approved", "described", "scheduled",
             "compared", "postponed", "updated", "clarified"],
    "obj": ["the delivery schedule", "the weather report", "the renovation plan", "the shopping list",
            "the tour timetable", "the quarterly budget", "the route map", "the storage rules",
            "the exhibit catalogue", "the safety manual"],
    "tail": ["for next week", "after a long debate", "taking residents' wishes into account", "without major changes",
             "at management's request", "as part of the annual audit", "despite the bad weather",
             "before the season starts"],
    "extra": ["Nobody had any questions.", "The decision was unanimous.", "Details will be published later.",
              "It took almost the whole day.", "Everyone was happy with the result.", "Work continues tomorrow."],
}
WORDS = {"ru": RU, "en": EN}


def sentence(rng: random.Random, lang: str = "ru") -> str:
    w = WORDS[lang]
    s = f"{rng.choice(w['subj'])} {rng.choice(w['verb'])} {rng.choice(w['obj'])} {rng.choice(w['tail'])}."
    if rng.random() < 0.3:
        s += " " + rng.choice(w["extra"])
    return s


def haystack(chars: int, seed: int = 0, lang: str = "ru") -> list[str]:
    """Список предложений общей длиной ≈ chars (с пробелами между ними)."""
    rng = random.Random(seed)
    out, size = [], 0
    while size < chars:
        s = sentence(rng, lang)
        out.append(s)
        size += len(s) + 1
    return out


def insert(sentences: list[str], items: list[tuple[float, str]]) -> str:
    """Вставить предложения-иголки на относительные позиции 0..1 (по границам предложений)."""
    result = list(sentences)
    for depth, text in sorted(items, key=lambda x: -x[0]):  # с конца — индексы не съезжают
        idx = round(max(0.0, min(1.0, depth)) * len(result))
        result.insert(idx, text)
    return " ".join(result)


def filler(tokens: int, chars_per_token: float, seed: int = 0, lang: str = "ru") -> str:
    return " ".join(haystack(int(tokens * chars_per_token), seed, lang))
