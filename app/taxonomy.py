"""Таксономия: приведение тегов OSM и типов Wikidata к смысловым категориям.

Пользователю неинтересно `historic=memorial` — ему интересно «памятник»,
«архитектура», «современная культура». 80% случаев решаются правилами:
дёшево, детерминированно, объяснимо. LLM нужен дальше, для текста.
"""

from __future__ import annotations

import re

CATEGORIES = {
    "history": "История",
    "architecture": "Архитектура",
    "memorial": "Памятники и мемориалы",
    "religion": "Религия",
    "museum": "Музеи",
    "art": "Искусство и стрит-арт",
    "modern_culture": "Современная культура",
    "social": "Социальная жизнь",
    "park_nature": "Парки и природа",
    "industrial": "Промышленное наследие",
    "viewpoint": "Виды и панорамы",
    "district": "Районы и местности",
    "transport": "Транспорт",
    "infrastructure": "Инженерия и инфраструктура",
    "community": "Сообщества и независимые места",
    "nightlife": "Ночная жизнь",
}

# (тег, значение-или-None) -> (категории, вес класса 0..1)
_TAG_RULES: list[tuple[str, tuple[str, ...] | None, tuple[str, ...], float]] = [
    ("historic", ("castle", "fort", "city_gate", "citywalls"), ("history", "architecture"), 0.85),
    ("historic", ("archaeological_site", "ruins"), ("history",), 0.7),
    ("historic", ("monument",), ("memorial", "history"), 0.7),
    ("historic", ("memorial",), ("memorial",), 0.45),
    ("historic", ("building", "house", "manor", "palace"), ("architecture", "history"), 0.65),
    ("historic", ("wayside_cross", "wayside_shrine", "boundary_stone"), ("history",), 0.3),
    ("historic", None, ("history",), 0.5),
    ("tourism", ("museum",), ("museum",), 0.8),
    ("tourism", ("gallery",), ("modern_culture", "art"), 0.6),
    ("tourism", ("artwork",), ("art",), 0.45),
    ("tourism", ("viewpoint",), ("viewpoint",), 0.5),
    ("tourism", ("attraction",), ("modern_culture",), 0.5),
    ("tourism", ("zoo", "aquarium", "theme_park"), ("modern_culture",), 0.6),
    ("amenity", ("place_of_worship",), ("religion", "architecture"), 0.6),
    ("amenity", ("theatre",), ("modern_culture",), 0.7),
    ("amenity", ("cinema",), ("modern_culture",), 0.45),
    ("amenity", ("arts_centre",), ("modern_culture", "art"), 0.6),
    ("amenity", ("library",), ("social", "modern_culture"), 0.5),
    ("amenity", ("community_centre", "social_facility"), ("social",), 0.4),
    ("amenity", ("marketplace",), ("social",), 0.45),
    ("amenity", ("fountain",), ("art",), 0.35),
    ("artwork_type", None, ("art",), 0.4),
    ("memorial", None, ("memorial",), 0.4),
    ("leisure", ("park", "garden"), ("park_nature",), 0.55),
    ("leisure", ("nature_reserve",), ("park_nature",), 0.6),
    ("boundary", ("protected_area",), ("park_nature",), 0.5),
    ("man_made", ("lighthouse", "windmill", "watermill"), ("industrial", "history"), 0.6),
    ("man_made", ("water_tower", "tower", "obelisk"), ("industrial",), 0.45),
    ("man_made", ("bridge",), ("architecture",), 0.4),
    ("building", ("church", "cathedral", "chapel", "monastery", "temple"), ("religion", "architecture"), 0.6),
    ("building", ("mosque", "synagogue"), ("religion", "architecture"), 0.6),
    ("building", ("castle", "palace"), ("architecture", "history"), 0.8),
    ("building", ("train_station",), ("architecture", "industrial"), 0.5),
    # инженерия: мосты, тоннели, депо, подстанции, водонапорные башни
    ("man_made", ("bridge", "tunnel", "pier", "crane", "gasometer", "chimney",
                  "cooling_tower", "storage_tank", "silo"), ("infrastructure",), 0.45),
    ("man_made", ("telescope", "antenna", "mast", "works", "pumping_station",
                  "water_works", "wastewater_plant", "kiln", "adit", "mineshaft"),
     ("infrastructure", "industrial"), 0.5),
    ("power", ("plant", "generator"), ("infrastructure", "industrial"), 0.55),
    ("power", ("substation",), ("infrastructure",), 0.4),
    ("railway", ("station", "halt"), ("infrastructure", "transport"), 0.45),
    ("railway", ("roundhouse", "turntable", "traverser", "water_tower", "signal_box"),
     ("infrastructure", "industrial", "history"), 0.6),
    ("waterway", ("dam", "weir", "lock_gate"), ("infrastructure",), 0.55),
    ("aeroway", ("terminal", "control_tower", "hangar"), ("infrastructure",), 0.45),
    ("bridge", None, ("infrastructure", "architecture"), 0.4),
    ("tunnel", None, ("infrastructure",), 0.4),
    ("building", ("industrial", "warehouse", "silo", "hangar", "water_tower"),
     ("industrial", "infrastructure"), 0.4),
    # современный слой
    ("leisure", ("hackerspace", "makerspace"), ("community", "modern_culture"), 0.75),
    ("leisure", ("dance", "escape_game"), ("modern_culture",), 0.45),
    ("amenity", ("social_centre",), ("community", "social"), 0.7),
    ("amenity", ("coworking_space",), ("community", "modern_culture"), 0.45),
    ("amenity", ("music_venue", "events_venue"), ("modern_culture",), 0.6),
    ("amenity", ("nightclub",), ("nightlife", "modern_culture"), 0.5),
    ("amenity", ("studio",), ("modern_culture", "art"), 0.5),
    ("amenity", ("public_bath",), ("social", "modern_culture"), 0.55),
    ("club", None, ("community", "modern_culture"), 0.55),
    ("theatre:genre", None, ("modern_culture",), 0.5),
    ("craft", ("artist", "sculptor", "printmaker", "photographer", "luthier",
               "organ_builder", "bookbinder", "pottery"), ("community", "art"), 0.5),
    ("craft", None, ("community",), 0.35),
    ("shop", ("books", "records", "music", "art", "antiques", "second_hand",
              "musical_instrument", "photo", "craft", "zero_waste", "charity"),
     ("community", "modern_culture"), 0.4),
    ("office", ("ngo", "association", "foundation"), ("community", "social"), 0.4),
    ("memorial", ("plaque", "plate"), ("memorial", "history"), 0.35),
]

# Ключевые слова в типах Wikidata (P31) и описаниях — второй проход.
# Порядок важен: «памятник архитектуры» — это охранный статус здания, а не
# скульптура на постаменте, поэтому такие формы разбираются отдельно и раньше.
_KEYWORD_RULES: list[tuple[str, tuple[str, ...], float]] = [
    (r"памятник\w*\s+(архитектур|градостроительств|истори|культур|наследи)", ("architecture", "history"), 0.55),
    (r"\b(музей|museum)", ("museum",), 0.8),
    (r"\b(театр|theatre|theater)", ("modern_culture",), 0.7),
    (r"\b(галере|gallery)", ("modern_culture", "art"), 0.6),
    (r"\b(храм|церк|собор|часовн|church|cathedral|mosque|мечет|синагог|монастыр)",
     ("religion", "architecture"), 0.6),
    (r"\b(памятник(?!\w*\s+(архитектур|градостроительств|истори|культур|наследи))|монумент|monument|"
     r"memorial|мемориал|скульптур|statue|бюст|обелиск)", ("memorial", "art"), 0.55),
    (r"\b(усадьб|особняк|дворец|palace|mansion|доходный дом|villa|палаты)", ("architecture", "history"), 0.7),
    (r"\b(парк|сад\b|park|garden|сквер|бульвар|пруд)", ("park_nature",), 0.5),
    (r"\b(завод|фабрик|factory|мануфактур|депо|электростанц)", ("industrial", "history"), 0.55),
    (r"\b(вокзал|мост|bridge|railway station)", ("architecture",), 0.5),
    (r"\b(библиотек|library|университет|институт|консерватор|академи|university)",
     ("social", "modern_culture"), 0.5),
    (r"\b(битв|сражен|восстани|battle|uprising)", ("history",), 0.7),
    (r"\b(граффити|мурал|street art|стрит-арт)", ("art", "modern_culture"), 0.5),
    (r"\b(станция метро|метрополитен|metro station)", ("transport",), 0.25),
    (r"\b(мост|виадук|эстакад|тоннел|туннел|плотин|шлюз|акведук|водонапорн|градирн|"
     r"тэц|гэс|электростанц|подстанц|депо|телебашн|радиобашн|обсерватор|элеватор|"
     r"газгольдер|дымов\w* труб)", ("infrastructure",), 0.5),
]

# Охранный статус -> прибавка к значимости
HERITAGE_BOOST = [
    (("всемирного наследия", "world heritage", "unesco"), 0.45, "объект Всемирного наследия ЮНЕСКО"),
    (("федерального значения", "national"), 0.3, "объект культурного наследия федерального значения"),
    (("регионального значения", "regional"), 0.18, "объект культурного наследия регионального значения"),
    (("местного значения", "local", "выявленн"), 0.1, "объект культурного наследия местного значения"),
]


_COMPILED = [(re.compile(p, re.I), c, w) for p, c, w in _KEYWORD_RULES]


def classify(tags: dict[str, str], wd_types: list[str], description: str | None) -> tuple[list[str], float]:
    """-> (категории, вес класса 0..1)."""
    cats: dict[str, None] = {}
    weight = 0.0

    for key, values, categories, w in _TAG_RULES:
        val = tags.get(key)
        if val is None:
            continue
        if values is not None and val not in values:
            continue
        for c in categories:
            cats[c] = None
        weight = max(weight, w)

    haystack = " ".join(filter(None, [*wd_types, description or ""])).lower()
    for pattern, categories, w in _COMPILED:
        if pattern.search(haystack):
            for c in categories:
                cats[c] = None
            weight = max(weight, w)

    # Уточнения по дополнительным тегам
    if tags.get("heritage") or tags.get("ref:ru:kulturnoe-nasledie"):
        cats.setdefault("history", None)
        weight = max(weight, 0.6)
    if tags.get("start_date") or tags.get("architect"):
        cats.setdefault("architecture", None)
        weight = max(weight, 0.5)

    if not cats:
        cats["history"] = None
        weight = max(weight, 0.2)
    return list(cats), weight


def heritage_status(labels: list[str], tags: dict[str, str]) -> tuple[float, str | None]:
    haystack = " ".join(labels + [tags.get("heritage", ""), tags.get("heritage:operator", "")]).lower()
    for needles, boost, title in HERITAGE_BOOST:
        if any(n in haystack for n in needles):
            return boost, title
    if tags.get("heritage") or tags.get("ref:ru:kulturnoe-nasledie"):
        return 0.1, "объект культурного наследия"
    return 0.0, None
