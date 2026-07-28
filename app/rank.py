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
    "notability": 0.42,
    "class": 0.18,
    "proximity": 0.15,
    "heritage": 0.15,
    "interest": 0.10,
}


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
            "interest": 1.0 if interests & set(poi.categories) else 0.0,
        }
        score = sum(WEIGHTS[k] * v for k, v in parts.items())
        out.append(Scored(poi, pos.s, pos.offset, pos.side, round(score, 4), parts, heritage_title))
    out.sort(key=lambda x: -x.score)
    return out


def thin(
    scored: list[Scored], route_length_m: float, *, limit: int, segment_m: float | None = None,
) -> list[Scored]:
    """Разрежение по километражу: не даём одному кварталу забрать весь топ."""
    if len(scored) <= limit:
        return sorted(scored, key=lambda x: x.s)
    if segment_m is None:
        # столько отрезков, чтобы в каждый попало 2-3 объекта
        segment_m = max(120.0, route_length_m / max(1, limit / 2.5))

    buckets: dict[int, list[Scored]] = {}
    for item in scored:  # уже отсортированы по убыванию score
        buckets.setdefault(int(item.s // segment_m), []).append(item)

    picked: list[Scored] = []
    depth = 0
    while len(picked) < limit:
        added = False
        for key in sorted(buckets):
            bucket = buckets[key]
            if depth < len(bucket) and len(picked) < limit:
                picked.append(bucket[depth])
                added = True
        if not added:
            break
        depth += 1
    return sorted(picked, key=lambda x: x.s)
