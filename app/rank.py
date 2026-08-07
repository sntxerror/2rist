"""Ранжирование и разрежение.

Без этого на маршруте Москва — Петербург вернётся 40 000 объектов, а весь
топ-100 окажется в историческом центре. Скор — линейная комбинация, плюс
обязательное разрежение по километражу: топ-N из каждого отрезка маршрута.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from . import taxonomy
from .merge import POI

WEIGHTS = {
    "notability": 0.34,
    "class": 0.18,
    "proximity": 0.14,
    "heritage": 0.12,
    "liveliness": 0.12,
    "interest": 0.10,
}

# Категории «живого города». У них почти никогда нет статьи в Википедии,
# поэтому по общей формуле они не всплывают никогда — им нужна квота.
MODERN = {"community", "modern_culture", "nightlife", "art", "social", "museum"}
MODERN_SHARE = 0.4  # доля выдачи, зарезервированная под современный слой
CATEGORY_CAP = 0.3  # не больше этой доли выдачи на одну категорию

# Признаки того, что место живое и за ним кто-то следит
_ALIVE_TAGS = ("website", "contact:website", "opening_hours", "description",
               "contact:instagram", "contact:telegram", "phone", "contact:phone",
               "email", "image", "wheelchair", "fee")


@dataclass(slots=True)
class Scored:
    poi: POI
    s: float          # метры от старта
    offset: float
    side: str
    score: float
    parts: dict[str, float]
    heritage_title: str | None


def notability(poi: POI) -> float:
    """0..1 из числа языковых версий Википедии — лучший бесплатный прокси известности."""
    n = poi.sitelinks
    if n <= 0:
        return 0.12 if poi.wikipedia else 0.0
    return min(1.0, math.log1p(n) / math.log1p(60))


def liveliness(poi: POI) -> float:
    """0..1 по полноте тегов: заполненная карточка — признак действующего места.

    Заброшенную подстанцию никто не описывает и не указывает часы работы,
    а у хакспейса будут сайт, телеграм и расписание событий.
    """
    tags = poi.tags
    score = sum(0.16 for key in _ALIVE_TAGS if tags.get(key))
    if tags.get("check_date", "") >= "2025":  # кто-то недавно проверял на месте
        score += 0.2
    if tags.get("opening_hours"):
        score += 0.1
    return min(1.0, score)


def year_of(poi: POI) -> int | None:
    for value in (poi.inception, poi.tags.get("start_date")):
        if value and (m := re.search(r"(-?\d{3,4})", value)):
            return int(m.group(1))
    return None


def score_all(
    pois: list[POI], route, *, interests: list[str] | None = None, max_offset_m: float = 400.0,
) -> list[Scored]:
    interests = set(interests or [])
    out: list[Scored] = []
    for poi in pois:
        pos = route.project(poi.lat, poi.lon)
        heritage_boost, heritage_title = taxonomy.heritage_status(poi.heritage_labels, poi.tags)
        parts = {
            "notability": notability(poi),
            "class": poi.class_weight,
            "proximity": 1.0 / (1.0 + pos.offset / max(60.0, max_offset_m * 0.4)),
            "heritage": min(1.0, heritage_boost / 0.45),
            "liveliness": liveliness(poi),
            "interest": 1.0 if interests & set(poi.categories) else 0.0,
        }
        score = sum(WEIGHTS[k] * v for k, v in parts.items())
        out.append(Scored(poi, pos.s, pos.offset, pos.side, round(score, 4), parts, heritage_title))
    out.sort(key=lambda x: -x.score)
    return out


def thin(
    scored: list[Scored], route_length_m: float, *, limit: int,
    segment_m: float | None = None, modern_share: float | None = MODERN_SHARE,
) -> list[Scored]:
    """Разрежение по километражу плюс квота современному слою.

    Без квоты выдача состоит из памятников: у них есть Википедия, а у
    независимой галереи или хакспейса — нет.
    """
    if len(scored) <= limit:
        return sorted(scored, key=lambda x: x.s)

    if modern_share:
        modern = [x for x in scored if MODERN & set(x.poi.categories)]
        classic = [x for x in scored if not (MODERN & set(x.poi.categories))]
        want_modern = min(len(modern), round(limit * modern_share))
        picked = (_thin_track(modern, route_length_m, want_modern, segment_m)
                  + _thin_track(classic, route_length_m, limit - want_modern, segment_m))
        return sorted(picked, key=lambda x: x.s)
    return _thin_track(scored, route_length_m, limit, segment_m)


def _thin_track(
    scored: list[Scored], route_length_m: float, limit: int, segment_m: float | None = None,
) -> list[Scored]:
    if limit <= 0:
        return []
    if len(scored) <= limit:
        return list(scored)
    if segment_m is None:
        # столько отрезков, чтобы в каждый попало 2-3 объекта
        segment_m = max(120.0, route_length_m / max(1, limit / 2.5))

    buckets: dict[int, list[Scored]] = {}
    for item in scored:  # уже отсортированы по убыванию score
        buckets.setdefault(int(item.s // segment_m), []).append(item)

    # мемориальных досок на улице бывает десяток подряд; без потолка они
    # вытесняют всё остальное
    cap = max(2, round(limit * CATEGORY_CAP))
    used: dict[str, int] = {}

    def over_cap(item: Scored) -> bool:
        primary = item.poi.categories[0] if item.poi.categories else "?"
        return used.get(primary, 0) >= cap

    def take(item: Scored) -> None:
        primary = item.poi.categories[0] if item.poi.categories else "?"
        used[primary] = used.get(primary, 0) + 1

    picked: list[Scored] = []
    postponed: list[Scored] = []
    depth = 0
    while len(picked) < limit:
        added = False
        for key in sorted(buckets):
            bucket = buckets[key]
            if depth < len(bucket) and len(picked) < limit:
                item = bucket[depth]
                added = True
                if over_cap(item):
                    postponed.append(item)
                else:
                    picked.append(item)
                    take(item)
        if not added:
            break
        depth += 1
    # если после потолка мест не хватило — добираем отложенными
    for item in postponed:
        if len(picked) >= limit:
            break
        picked.append(item)
    return picked
