"""«Настроение» участков маршрута — в духе Hoodmaps, но выводится из данных.

Hoodmaps размечают люди; здесь то же самое считается по составу заведений и
землепользования вокруг маршрута. Кофейни и винтажные магазины дают один
профиль, склады и подстанции — другой, общаги и копицентры — третий.

Это не «достопримечательности», а ответ на вопрос «а что здесь вообще за место».
"""

from __future__ import annotations

from collections import Counter

# сигнал -> (человекочитаемая метка, вес одного объекта)
LABELS = {
    "nightlife": ("ночная жизнь", "🍸"),
    "coffee": ("кофейни и независимые лавки", "☕"),
    "tourists": ("туристы", "📸"),
    "students": ("студенты", "🎓"),
    "office": ("офисы и деловая жизнь", "💼"),
    "industrial": ("промзона", "🏭"),
    "residential": ("жилой район", "🏠"),
    "retail": ("торговля", "🛍"),
    "green": ("зелень и спорт", "🌳"),
    "transit": ("транспортный узел", "🚉"),
    "culture": ("культурные площадки", "🎭"),
    "faith": ("храмы", "⛪"),
}


def signal_of(tags: dict[str, str]) -> tuple[str, float] | None:
    """Один объект OSM -> сигнал и его вес."""
    amenity = tags.get("amenity")
    shop = tags.get("shop")
    landuse = tags.get("landuse")
    tourism = tags.get("tourism")
    leisure = tags.get("leisure")

    if amenity in ("bar", "pub", "nightclub", "biergarten"):
        return "nightlife", 1.4
    if amenity in ("cafe", "coworking_space") or shop in ("coffee", "second_hand", "books", "art", "music", "bicycle"):
        return "coffee", 1.0
    if amenity in ("restaurant", "fast_food"):
        return "retail", 0.5
    if tourism in ("hotel", "hostel", "guest_house", "information"):
        return "tourists", 1.2
    if tourism in ("attraction", "museum"):
        return "tourists", 0.8
    if amenity in ("university", "college"):
        return "students", 1.6
    if amenity in ("school", "kindergarten"):
        return "residential", 1.0
    if amenity in ("library", "theatre"):
        return "culture", 1.0
    if amenity == "place_of_worship":
        return "faith", 0.8
    if amenity == "marketplace" or shop:
        return "retail", 0.6
    if tags.get("office"):
        return "office", 1.0
    if leisure in ("park", "garden", "fitness_centre", "sports_centre", "playground"):
        return "green", 0.8
    if tags.get("public_transport") == "station" or tags.get("railway") in ("station", "halt"):
        return "transit", 1.5
    if landuse == "industrial":
        return "industrial", 3.0  # полигон промзоны весит больше точки
    if landuse == "residential":
        return "residential", 2.0
    if landuse in ("commercial",):
        return "office", 2.0
    if landuse in ("retail",):
        return "retail", 2.0
    if landuse == "construction":
        return "industrial", 1.0
    return None


def segments(
    route, elements: list[dict], spans: list[tuple[float, float, str]], *, min_objects: int = 12,
) -> list[dict]:
    """Считает профиль для каждого отрезка маршрута.

    spans — [(от_м, до_м, подпись)], обычно границы районов или ровные бины.
    """
    scores: dict[int, Counter] = {i: Counter() for i in range(len(spans))}
    totals: dict[int, int] = dict.fromkeys(range(len(spans)), 0)

    for el in elements:
        sig = signal_of(el["tags"])
        if not sig:
            continue
        pos = route.project(el["lat"], el["lon"])
        for i, (start, end, _) in enumerate(spans):
            if start <= pos.s <= end:
                # ближе к маршруту — весомее
                decay = 1.0 / (1.0 + pos.offset / 300.0)
                scores[i][sig[0]] += sig[1] * decay
                totals[i] += 1
                break

    out = []
    for i, (start, end, title) in enumerate(spans):
        counter = scores[i]
        total = sum(counter.values())
        if totals[i] < min_objects or total <= 0:
            out.append({"from_m": round(start), "to_m": round(end), "title": title,
                        "vibe": [], "objects": totals[i]})
            continue
        top = counter.most_common(3)
        out.append({
            "from_m": round(start), "to_m": round(end), "title": title,
            "objects": totals[i],
            "vibe": [
                {"key": key, "label": LABELS[key][0], "emoji": LABELS[key][1],
                 "share": round(value / total, 3)}
                for key, value in top if value / total >= 0.12
            ],
        })
    return out


def spans_from(districts: list[dict], route_length: float, *, bin_m: float = 1000.0) -> list[tuple[float, float, str]]:
    """Отрезки для профилирования: границы районов, иначе ровные бины."""
    if districts:
        spans = [(d["from_m"], d["to_m"], d["name"]) for d in districts if d["to_m"] > d["from_m"]]
        if spans:
            return spans
    n = max(1, int(route_length // bin_m) + 1)
    step = route_length / n
    return [(i * step, (i + 1) * step, f"{i * step / 1000:.1f}–{(i + 1) * step / 1000:.1f} км")
            for i in range(n)]
