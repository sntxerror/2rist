"""Сборка пайплайна: маршрут -> коридор -> источники -> склейка -> ранг -> ответ."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from . import ai, geo, merge, narrate, rank, sources, taxonomy, vibe

log = logging.getLogger("2rist.pipeline")

# Базовый радиус коридора по профилю: пешеход реально свернёт на 200 м,
# на трассе с таким радиусом не найдётся ничего.
DEFAULT_RADIUS = {"walking": 250.0, "cycling": 400.0, "driving": 1200.0}
MAX_RADIUS = 5000.0


def resolve_route(spec: dict) -> dict:
    """Любой вход приводим к одному виду: линия + метаданные."""
    profile = spec.get("profile", "walking")
    labels: list[str] = []

    if spec.get("polyline"):
        pts = geo.decode_polyline(spec["polyline"])
    elif spec.get("geojson"):
        pts = geo.geojson_line(spec["geojson"])
    else:
        if spec.get("points"):
            waypoints = [(float(a), float(b)) for a, b in spec["points"]]
        else:
            places = [p for p in [spec.get("from"), *(spec.get("via") or []), spec.get("to")] if p]
            if len(places) < 2:
                raise ValueError("нужно указать from/to, points, polyline или geojson")
            waypoints = []
            for place in places:
                lat, lon, label = sources.geocode(place)
                waypoints.append((lat, lon))
                labels.append(label)
        r = sources.osrm_route(waypoints, profile)
        return {"polyline": r["polyline"], "points": geo.decode_polyline(r["polyline"]),
                "profile": profile, "distance_m": r["distance_m"],
                "duration_s": r["duration_s"], "waypoints": labels}

    return {"polyline": geo.encode_polyline(pts), "points": pts, "profile": profile,
            "distance_m": None, "duration_s": None, "waypoints": labels}


def highlights(
    spec: dict, *, radius_m: float | None = None, limit: int = 30,
    interests: list[str] | None = None, lang: str = "ru", with_text: bool = False,
    with_vibe: bool = True,
) -> dict:
    t0 = time.monotonic()
    stats: dict[str, int | float] = {}

    route_info = resolve_route(spec)
    route = geo.RouteIndex(route_info["points"])
    profile = route_info["profile"]
    radius = float(radius_m or DEFAULT_RADIUS.get(profile, 300.0))
    # на длинных маршрутах узкий коридор бессмысленен
    if radius_m is None and route.length > 50_000:
        radius = min(MAX_RADIUS, radius * (1 + route.length / 200_000))

    # --- 1. источники (разные хосты — забираем параллельно) -----------------
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_osm = pool.submit(sources.overpass_corridor, route, radius, with_vibe=with_vibe)
        f_wiki = pool.submit(sources.wikipedia_geosearch, route, radius, lang=lang)
        f_wd = pool.submit(sources.wikidata_in_corridor, route, radius)
        f_areas = pool.submit(_safe_areas, route)
        corridor, wiki_pages, wd_corridor, areas = (f.result() for f in (f_osm, f_wiki, f_wd, f_areas))

    # одна выборка Overpass делится локально: объекты и фон для «настроения»
    osm_raw = [el for el in corridor if sources.is_poi(el["tags"])]
    vibe_raw = corridor if with_vibe else []
    stats["osm"] = len(osm_raw)
    stats["wikipedia"] = len(wiki_pages)
    stats["wikidata"] = len(wd_corridor)

    pois = [p for p in (merge.from_osm(r, lang) for r in osm_raw) if p]
    pois += [merge.from_wikipedia(p) for p in wiki_pages]
    pois += [p for p in (merge.from_wikidata(r) for r in wd_corridor.values()) if p]
    stats["raw_total"] = len(pois)

    # --- 2. склейка --------------------------------------------------------
    pois = merge.dedupe(pois, route)
    stats["after_dedupe"] = len(pois)

    # --- 3. отсечение по коридору до всякого обогащения ---------------------
    pois = [p for p in pois if route.project(p.lat, p.lon).offset <= radius * 1.25]
    stats["in_corridor"] = len(pois)

    # --- 4. значимость: дёшево для всех, подробности — потом ----------------
    need = [p.qid for p in pois if p.qid and p.qid not in wd_corridor]
    facts = {**wd_corridor, **sources.wikidata_notability(need)}
    for p in pois:
        if p.qid and p.qid in facts:
            merge.apply_wikidata(p, facts[p.qid])
        p.categories, p.class_weight = taxonomy.classify(p.tags, p.wd_types, p.description)

    # --- 5. ранжирование и разрежение --------------------------------------
    scored = rank.score_all(pois, route, interests=interests, max_offset_m=radius)
    top = rank.thin(scored, route.length, limit=limit)

    # --- 6. районы и их «настроение» ---------------------------------------
    districts = narrate.districts_along(route, areas)
    vibes = vibe.segments(route, vibe_raw, vibe.spans_from(districts, route.length)) if vibe_raw else []

    # --- 7. тексты: по умолчанию лениво, через /api/poi при открытии карточки
    if with_text:
        _load_texts([x.poi for x in top], lang=lang)

    stats["returned"] = len(top)
    stats["elapsed_s"] = round(time.monotonic() - t0, 2)
    log.info("pipeline: %s", stats)

    return {
        "route": {
            "polyline": route_info["polyline"],
            "profile": profile,
            "length_m": round(route.length),
            "duration_s": route_info["duration_s"],
            "waypoints": route_info["waypoints"],
            "corridor_m": round(radius),
        },
        "districts": districts,
        "vibe": vibes,
        "highlights": [_card(item, districts) for item in top],
        "stats": stats,
    }


def _load_texts(pois: list[merge.POI], *, lang: str = "ru") -> None:
    """Вводные абзацы Википедии для набора объектов (один батч на 20 штук)."""
    titles = [p.wikipedia.split(":", 1)[1] for p in pois
              if p.wikipedia and p.wikipedia.startswith(f"{lang}:")]
    if not titles:
        return
    extracts = sources.wikipedia_extracts(titles, lang=lang)
    for p in pois:
        if p.wikipedia and (data := extracts.get(p.wikipedia.split(":", 1)[1])):
            p.extract = data["extract"]
            p.image = p.image or data["thumbnail"]


def details(
    *, qid: str | None = None, wikipedia: str | None = None, osm: str | None = None,
    name: str = "", lat: float = 0.0, lon: float = 0.0, lang: str = "ru",
    with_ai: bool = False,
) -> dict:
    """Полная карточка одного объекта — грузится при открытии/приближении.

    В списке маршрута этого нет намеренно: запрос всех фактов Wikidata для
    сотен объектов стоит десятки секунд, а нужен он ровно для того объекта,
    который человек открыл.
    """
    poi = merge.POI(name=name, lat=lat, lon=lon, qid=qid, wikipedia=wikipedia, osm=osm)
    if qid and (rec := sources.wikidata_by_qids([qid]).get(qid)):
        merge.apply_wikidata(poi, rec)
        poi.name = poi.name or rec.get("label") or qid
    _load_texts([poi], lang=lang)
    poi.categories, poi.class_weight = taxonomy.classify(poi.tags, poi.wd_types, poi.description)
    year = rank.year_of(poi)
    address = sources.reverse_geocode(lat, lon) if (lat or lon) else {}
    card = {
        "id": poi.key,
        "name": poi.name,
        "summary": narrate.describe(poi),
        "description": poi.description,
        "year": year,
        "period": narrate.period_of(year),
        "architects": poi.architects,
        "styles": poi.styles,
        "heritage": taxonomy.heritage_status(poi.heritage_labels, poi.tags)[1],
        "categories": poi.categories,
        "category_labels": [taxonomy.CATEGORIES.get(c, c) for c in poi.categories],
        "image": poi.image,
        "wd_types": poi.wd_types,
        "address": address,
        "sources": narrate.sources_of(poi),
    }

    if with_ai:
        # Люди, события, что было раньше, этимология улицы — отдельный слой,
        # его собирает поиск (см. app/ai.py). Недоступность не ломает карточку.
        try:
            card["ai"] = ai.research(ai.facts_for(
                {**card, "coords": [lat, lon], "qid": qid, "wikipedia": wikipedia,
                 "description": poi.description},
                address=address, district=address.get("район"),
            ))
        except ai.AIUnavailable as exc:
            card["ai"] = {"error": str(exc), "stories": []}
        except Exception as exc:
            log.warning("ai enrichment: %s", exc)
            card["ai"] = {"error": "обогащение недоступно", "stories": []}
    return card


def _safe_areas(route) -> list[dict]:
    """Районы — необязательный слой: их отсутствие не должно ронять ответ."""
    try:
        return sources.overpass_areas(route)
    except Exception as exc:
        log.warning("районы недоступны: %s", exc)
        return []





def _card(item: rank.Scored, districts: list[dict]) -> dict:
    poi = item.poi
    return {
        "id": poi.key,
        "name": poi.name,
        "coords": [round(poi.lat, 6), round(poi.lon, 6)],
        # ключи для догрузки подробностей через /api/poi
        "qid": poi.qid,
        "wikipedia": poi.wikipedia,
        "osm": poi.osm,
        "route_position": {
            "km": round(item.s / 1000, 2),
            "offset_m": round(item.offset),
            "side": item.side,
        },
        "categories": poi.categories,
        "category_labels": [taxonomy.CATEGORIES.get(c, c) for c in poi.categories],
        "year": rank.year_of(poi),
        "period": narrate.period_of(rank.year_of(poi)),
        "architects": poi.architects,
        "styles": poi.styles,
        "heritage": item.heritage_title,
        "summary": narrate.describe(poi),
        "why_here": narrate.why_here(item, districts),
        "image": poi.image,
        "score": item.score,
        "score_parts": {k: round(v, 3) for k, v in item.parts.items()},
        "sources": narrate.sources_of(poi),
        "source_kinds": poi.sources,
    }
