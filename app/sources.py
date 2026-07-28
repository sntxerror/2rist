"""Внешние источники данных. Все — публичные, бесплатные, без ключей.

  OSRM        — построение маршрута
  Nominatim   — геокодирование адресов
  Overpass    — объекты и границы районов из OpenStreetMap (ODbL)
  Wikidata    — значимость, датировки, авторы, охранный статус
  Wikipedia   — тексты и иллюстрации

Все запросы идут через общую сессию с кэшем и лимитом частоты (app/http.py).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import geo
from .http import get_json, post_json, session

log = logging.getLogger("2rist.sources")

OSRM = "https://router.project-osrm.org"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
WDQS = "https://query.wikidata.org/sparql"
WIKI = "https://{lang}.wikipedia.org/w/api.php"

OSRM_PROFILE = {"walking": "foot", "cycling": "bike", "driving": "driving"}


# --------------------------------------------------------------------------
# маршрут
# --------------------------------------------------------------------------
def geocode(query: str) -> tuple[float, float, str]:
    data = get_json(
        NOMINATIM,
        {"q": query, "format": "jsonv2", "limit": 1, "accept-language": "ru"},
        slow=True,  # у Nominatim жёсткий потолок 1 запрос/с
    )
    if not data:
        raise ValueError(f"не удалось найти место: {query!r}")
    return float(data[0]["lat"]), float(data[0]["lon"]), data[0].get("display_name", query)


def suggest(query: str, *, near: tuple[float, float] | None = None,
            country: str | None = None, limit: int = 6) -> list[dict]:
    """Подсказки адресов. Nominatim, поэтому ввод на фронте дебаунсится."""
    params = {"q": query, "format": "jsonv2", "limit": limit,
              "accept-language": "ru", "addressdetails": 1}
    if country:
        params["countrycodes"] = country
    if near:  # приоритет ближайшим результатам
        lat, lon = near
        d = 0.75
        params["viewbox"] = f"{lon - d},{lat + d},{lon + d},{lat - d}"
    try:
        data = get_json(NOMINATIM, params, slow=True)
    except Exception as exc:
        log.warning("suggest: %s", exc)
        return []
    out, seen = [], set()
    for item in data:
        name = item.get("display_name", "")
        if name in seen:
            continue
        seen.add(name)
        out.append({
            "label": name.split(",")[0],
            "detail": ", ".join(name.split(",")[1:4]).strip(),
            "full": name,
            "lat": float(item["lat"]), "lon": float(item["lon"]),
            "kind": item.get("type"),
        })
    return out


def whereami() -> dict:
    """Грубая привязка по IP — чтобы подставить страну и центр карты."""
    try:
        data = get_json("https://ipapi.co/json/", expire_after=3600)
    except Exception as exc:
        log.warning("ip lookup: %s", exc)
        return {}
    if not data.get("latitude"):
        return {}
    return {
        "lat": data["latitude"], "lon": data["longitude"],
        "city": data.get("city"), "country": data.get("country_name"),
        "country_code": (data.get("country_code") or "").lower(),
    }


def reverse_geocode(lat: float, lon: float) -> dict:
    """Адрес и улица — нужны и для карточки, и как контекст для AI-обогащения."""
    try:
        data = get_json(
            "https://nominatim.openstreetmap.org/reverse",
            {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "format": "jsonv2",
             "accept-language": "ru", "zoom": 18},
            slow=True,
        )
    except Exception as exc:
        log.warning("reverse geocode: %s", exc)
        return {}
    a = data.get("address") or {}
    return {k: v for k, v in {
        "улица": a.get("road") or a.get("pedestrian") or a.get("footway"),
        "дом": a.get("house_number"),
        "район": a.get("suburb") or a.get("city_district") or a.get("quarter"),
        "город": a.get("city") or a.get("town") or a.get("village"),
        "страна": a.get("country"),
    }.items() if v}


def osrm_route(waypoints: list[tuple[float, float]], profile: str) -> dict:
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in waypoints)
    data = get_json(
        f"{OSRM}/route/v1/{OSRM_PROFILE.get(profile, 'foot')}/{coords}",
        {"overview": "full", "geometries": "polyline", "steps": "false"},
    )
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"OSRM: {data.get('code')} {data.get('message', '')}")
    r = data["routes"][0]
    return {"polyline": r["geometry"], "distance_m": r["distance"], "duration_s": r["duration"]}


# --------------------------------------------------------------------------
# OpenStreetMap / Overpass
# --------------------------------------------------------------------------
TAG_FILTERS = [
    '["historic"]',
    '["tourism"~"^(attraction|museum|artwork|viewpoint|gallery|aquarium|zoo)$"]',
    '["amenity"~"^(place_of_worship|theatre|arts_centre|library|community_centre|'
    'cinema|fountain|marketplace|social_facility)$"]',
    '["heritage"]',
    '["memorial"]',
    '["artwork_type"]',
    '["man_made"~"^(lighthouse|water_tower|windmill|watermill|obelisk|tower|bridge)$"]',
    '["leisure"~"^(park|garden|nature_reserve)$"]',
    '["boundary"="protected_area"]',
    '["building"~"^(church|cathedral|chapel|mosque|synagogue|temple|castle|palace|'
    'monastery|train_station)$"]',
    '["building"]["wikidata"]',
    '["building"]["start_date"]',
    '["building"]["architect"]',
    # инженерный слой: не «достопримечательности», но самое интересное
    # для тех, кто смотрит на город как на конструкцию
    '["man_made"~"^(bridge|tunnel|silo|gasometer|chimney|crane|pier|storage_tank|'
    'cooling_tower|telescope|antenna|mast|works|adit|mineshaft|pumping_station|'
    'water_works|wastewater_plant|kiln)$"]',
    '["power"~"^(plant|substation|generator)$"]',
    '["railway"~"^(station|halt|roundhouse|turntable|traverser|water_tower|signal_box)$"]',
    '["waterway"~"^(dam|weir|lock_gate)$"]',
    '["aeroway"~"^(terminal|control_tower|hangar)$"]',
    '["bridge"]["name"]',
    '["tunnel"]["name"]',
    '["building"~"^(industrial|warehouse|water_tower|silo|hangar)$"]["name"]',
]

# Слой «настроения»: по составу заведений видно, чем район живёт.
VIBE_FILTERS = [
    '["amenity"~"^(cafe|bar|pub|nightclub|biergarten|restaurant|fast_food|'
    'coworking_space|university|college|school|kindergarten|library|theatre|'
    'marketplace|place_of_worship)$"]',
    '["shop"]',
    '["office"]',
    '["tourism"~"^(hotel|hostel|guest_house|information|attraction|museum)$"]',
    '["landuse"~"^(industrial|residential|commercial|retail|construction)$"]',
    '["leisure"~"^(park|garden|fitness_centre|sports_centre|playground)$"]',
    '["public_transport"="station"]',
]


def _parallel(fn, items: list, workers: int = 4) -> list:
    """Запросы к разным кускам коридора не зависят друг от друга."""
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


# У публичных Overpass жёсткий лимит одновременных слотов на клиента:
# две параллельные выборки — и в ответ прилетает 429/504, а ретраи
# растягивают запрос на минуты. Поэтому к Overpass ходим строго по одному.
_OVERPASS_LOCK = threading.Semaphore(1)


def _wait_for_slot(endpoint: str, max_wait: float = 40.0) -> None:
    """Overpass сам сообщает, когда освободится слот, — спрашиваем и ждём.

    Без этого клиент упирается в 429 и уходит в ретраи, растягивая запрос
    на минуты вместо секунд.
    """
    status_url = endpoint.replace("/interpreter", "/status")
    try:
        text = session.get(status_url, timeout=15, expire_after=0).text
    except Exception:
        return
    if "slots available now" in text:
        return
    waits = [int(m) for m in re.findall(r"in (\d+) seconds", text)]
    if waits:
        delay = min(max(min(waits), 1), max_wait)
        log.info("overpass: жду слот %d c", delay)
        time.sleep(delay)


def _overpass(query: str, attempts: int = 3) -> dict:
    """Ретраи и зеркала: успешный ответ уходит в кэш и больше не запрашивается."""
    body = query.encode("utf-8")
    last: Exception | None = None
    for attempt in range(attempts):
        endpoint = OVERPASS_ENDPOINTS[attempt % len(OVERPASS_ENDPOINTS)]
        try:
            with _OVERPASS_LOCK:
                _wait_for_slot(endpoint)
                return post_json(endpoint, data=body)
        except Exception as exc:
            last = exc
            log.warning("overpass %s: %s (попытка %d)", endpoint.split("//")[1][:24], exc, attempt + 1)
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"Overpass недоступен: {last}")


def route_bboxes(route: geo.RouteIndex, radius_m: float, chunk_m: float = 4000.0) -> list[str]:
    """Маршрут -> набор bbox'ов, покрывающих коридор.

    `around` вдоль длинной ломаной Overpass не переживает (504), а bbox идёт
    по индексу. Лишнее из углов отсекается локальным фильтром по коридору.
    """
    from shapely.ops import substring

    boxes = []
    start = 0.0
    while start < route.length:
        end = min(start + chunk_m, route.length)
        part = substring(route.line, start, end)
        minx, miny, maxx, maxy = route.to_ll(part.buffer(radius_m, quad_segs=2)).bounds
        boxes.append(f"{miny:.5f},{minx:.5f},{maxy:.5f},{maxx:.5f}")
        start = end
    return boxes


def is_poi(tags: dict[str, str]) -> bool:
    """Локальный аналог TAG_FILTERS: делит общую выборку на объекты и фон.

    Держать рядом с TAG_FILTERS обязательно — расходятся, и часть объектов
    молча провалится в слой «настроения».
    """
    if tags.keys() & {"historic", "heritage", "memorial", "artwork_type"}:
        return True
    if tags.get("tourism") in {"attraction", "museum", "artwork", "viewpoint", "gallery", "aquarium", "zoo"}:
        return True
    if tags.get("amenity") in {"place_of_worship", "theatre", "arts_centre", "library",
                               "community_centre", "cinema", "fountain", "marketplace", "social_facility"}:
        return True
    if tags.get("man_made") in {"lighthouse", "water_tower", "windmill", "watermill", "obelisk",
                                "tower", "bridge", "tunnel", "silo", "gasometer", "chimney", "crane",
                                "pier", "storage_tank", "cooling_tower", "telescope", "antenna", "mast",
                                "works", "adit", "mineshaft", "pumping_station", "water_works",
                                "wastewater_plant", "kiln"}:
        return True
    if tags.get("leisure") in {"park", "garden", "nature_reserve"} or tags.get("boundary") == "protected_area":
        return True
    if tags.get("power") in {"plant", "substation", "generator"}:
        return True
    if tags.get("railway") in {"station", "halt", "roundhouse", "turntable", "traverser",
                               "water_tower", "signal_box"}:
        return True
    if tags.get("waterway") in {"dam", "weir", "lock_gate"}:
        return True
    if tags.get("aeroway") in {"terminal", "control_tower", "hangar"}:
        return True
    if tags.get("building") in {"church", "cathedral", "chapel", "mosque", "synagogue", "temple",
                                "castle", "palace", "monastery", "train_station"}:
        return True
    if "building" in tags and tags.keys() & {"wikidata", "start_date", "architect"}:
        return True
    if tags.get("name") and (tags.keys() & {"bridge", "tunnel"} or
                             tags.get("building") in {"industrial", "warehouse", "silo", "hangar", "water_tower"}):
        return True
    return False


def overpass_corridor(route: geo.RouteIndex, radius_m: float, *, with_vibe: bool = True) -> list[dict]:
    """Одна выборка на коридор: и объекты, и фон для «настроения».

    Раньше это были два запроса, но публичный Overpass не любит, когда его
    дёргают параллельно, а последовательно — вдвое дольше.
    """
    filters = TAG_FILTERS + (VIBE_FILTERS if with_vibe else [])

    def one(bbox: str) -> dict:
        body = "\n".join(f"  nwr({bbox}){f};" for f in filters)
        return _overpass(f"[out:json][timeout:180];\n(\n{body}\n);\nout center tags;")

    seen: dict[tuple[str, int], dict] = {}
    for data in _parallel(one, route_bboxes(route, radius_m), workers=1):
        for el in data.get("elements", []):
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            if lat is None:
                continue
            seen[(el["type"], el["id"])] = {
                "osm_type": el["type"], "osm_id": el["id"],
                "lat": lat, "lon": lon, "tags": el.get("tags") or {},
            }
    log.info("overpass: %d элементов в коридоре", len(seen))
    return list(seen.values())


def overpass_areas(route: geo.RouteIndex, pad_m: float = 500.0, lang: str = "ru") -> list[dict]:
    """Полигоны районов и местностей вокруг маршрута."""
    s, w, n, e = route.bbox(pad_m)
    bbox = f"{s:.5f},{w:.5f},{n:.5f},{e:.5f}"
    query = (
        "[out:json][timeout:180];\n(\n"
        f'  rel({bbox})["boundary"="administrative"]["admin_level"~"^(8|9|10)$"];\n'
        f'  rel({bbox})["place"~"^(suburb|quarter|neighbourhood|borough|city_district)$"];\n'
        f'  way({bbox})["place"~"^(suburb|quarter|neighbourhood)$"];\n'
        ");\nout geom;"
    )
    data = _overpass(query)
    areas = []
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        # в не-русскоязычных странах name — на местном языке, берём перевод
        name = tags.get(f"name:{lang}") or tags.get("name:en") or tags.get("name")
        if not name:
            continue
        if el["type"] == "way":
            rings = [[(p["lat"], p["lon"]) for p in el.get("geometry") or []]]
        else:
            rings = [
                [(p["lat"], p["lon"]) for p in m.get("geometry") or []]
                for m in el.get("members", [])
                if m.get("role") in ("outer", "")
            ]
        poly = geo.build_polygon([r for r in rings if len(r) >= 2])
        if poly is None or poly.is_empty:
            continue
        areas.append({"osm": f"{el['type']}/{el['id']}", "name": name, "tags": tags,
                      "poly": poly, "size": poly.area})
    log.info("overpass areas: %d полигонов", len(areas))
    return areas


# --------------------------------------------------------------------------
# Wikidata
# --------------------------------------------------------------------------
_FIELDS = """
  OPTIONAL { ?item wdt:P571 ?inception }
  OPTIONAL { ?item wdt:P18 ?image }
  OPTIONAL { ?item wdt:P31 ?type }
  OPTIONAL { ?item wdt:P1435 ?heritage }
  OPTIONAL { ?item wdt:P84 ?architect }
  OPTIONAL { ?item wdt:P149 ?style }
"""
_SELECT = (
    "?item ?itemLabel ?itemDescription ?sitelinks ?inception ?image ?coord "
    "?typeLabel ?heritageLabel ?architectLabel ?styleLabel"
)


def _sparql(query: str) -> list[dict]:
    data = post_json(
        WDQS,
        data={"query": query},
        headers={"Accept": "application/sparql-results+json"},
    )
    return data["results"]["bindings"]


def _collect(rows: list[dict]) -> dict[str, dict]:
    """SPARQL с OPTIONAL даёт декартово произведение — схлопываем по item."""
    out: dict[str, dict] = {}
    for row in rows:
        get = lambda k: row[k]["value"] if k in row else None  # noqa: E731
        qid = get("item").rsplit("/", 1)[-1]
        rec = out.setdefault(
            qid,
            {"qid": qid, "label": None, "description": None, "sitelinks": 0, "inception": None,
             "image": None, "lat": None, "lon": None,
             "types": set(), "heritage": set(), "architects": set(), "styles": set()},
        )
        rec["label"] = rec["label"] or get("itemLabel")
        rec["description"] = rec["description"] or get("itemDescription")
        rec["sitelinks"] = max(rec["sitelinks"], int(get("sitelinks") or 0))
        rec["inception"] = rec["inception"] or get("inception")
        rec["image"] = rec["image"] or get("image")
        for src, dst in (("typeLabel", "types"), ("heritageLabel", "heritage"),
                         ("architectLabel", "architects"), ("styleLabel", "styles")):
            if v := get(src):
                rec[dst].add(v)
        if rec["lat"] is None and (coord := get("coord")):
            try:  # "Point(37.6 55.75)"
                lon, lat = coord[coord.index("(") + 1 : coord.index(")")].split()
                rec["lat"], rec["lon"] = float(lat), float(lon)
            except ValueError:
                pass
    for rec in out.values():
        for f in ("types", "heritage", "architects", "styles"):
            rec[f] = sorted(rec[f])
    return out


def wikidata_notability(qids: list[str], lang: str = "ru,en", batch: int = 150) -> dict[str, dict]:
    """Только значимость и подпись — для ранжирования этого достаточно.

    Полный набор фактов (архитектор, стиль, охранный статус) стоит на порядок
    дороже, поэтому его берём отдельно и только для короткого списка.
    """
    qids = sorted({q for q in qids if q.startswith("Q")})

    def one(chunk: list[str]) -> dict[str, dict]:
        values = " ".join(f"wd:{q}" for q in chunk)
        query = f"""SELECT ?item ?itemLabel ?itemDescription ?sitelinks WHERE {{
  VALUES ?item {{ {values} }}
  ?item wikibase:sitelinks ?sitelinks .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{lang}". }}
}}"""
        try:
            return _collect(_sparql(query))
        except Exception as exc:
            log.warning("wikidata notability: %s", exc)
            return {}

    result: dict[str, dict] = {}
    chunks = [qids[i : i + batch] for i in range(0, len(qids), batch)]
    for part in _parallel(one, chunks, workers=3):
        result.update(part)
    log.info("wikidata notability: %d из %d", len(result), len(qids))
    return result


def wikidata_by_qids(qids: list[str], lang: str = "ru,en", batch: int = 60) -> dict[str, dict]:
    """Полные факты по объектам — дорогой запрос, только для шорт-листа."""
    qids = sorted({q for q in qids if q.startswith("Q")})

    def one(chunk: list[str]) -> dict[str, dict]:
        values = " ".join(f"wd:{q}" for q in chunk)
        query = f"""SELECT {_SELECT} WHERE {{
  VALUES ?item {{ {values} }}
  ?item wikibase:sitelinks ?sitelinks .
  OPTIONAL {{ ?item wdt:P625 ?coord }}
{_FIELDS}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{lang}". }}
}}"""
        try:
            return _collect(_sparql(query))
        except Exception as exc:  # источник не должен ронять весь запрос
            log.warning("wikidata batch: %s", exc)
            return {}

    result: dict[str, dict] = {}
    chunks = [qids[i : i + batch] for i in range(0, len(qids), batch)]
    for part in _parallel(one, chunks, workers=3):
        result.update(part)
    log.info("wikidata: обогащено %d из %d", len(result), len(qids))
    return result


def wikidata_in_corridor(
    route: geo.RouteIndex, radius_m: float, *, min_sitelinks: int = 4, lang: str = "ru,en",
) -> dict[str, dict]:
    """Объекты, которых нет в OSM: события, снесённые здания, литературные адреса.

    wikibase:box по прямоугольнику дешевле десятка wikibase:around по кругам,
    а лишнее из углов всё равно отсекает фильтр коридора.
    """
    def one(bbox: str) -> dict[str, dict]:
        s, w, n, e = (float(x) for x in bbox.split(","))
        query = f"""SELECT {_SELECT} WHERE {{
  SERVICE wikibase:box {{
    ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:cornerSouthWest "Point({w:.5f} {s:.5f})"^^geo:wktLiteral .
    bd:serviceParam wikibase:cornerNorthEast "Point({e:.5f} {n:.5f})"^^geo:wktLiteral .
  }}
  ?item wikibase:sitelinks ?sitelinks . FILTER(?sitelinks >= {min_sitelinks})
{_FIELDS}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{lang}". }}
}}"""
        try:
            return _collect(_sparql(query))
        except Exception as exc:  # источник не должен ронять весь запрос
            log.warning("wikidata box: %s", exc)
            return {}

    result: dict[str, dict] = {}
    for chunk in _parallel(one, route_bboxes(route, radius_m, chunk_m=8000.0), workers=3):
        for qid, rec in chunk.items():
            result.setdefault(qid, rec)
    log.info("wikidata corridor: %d объектов", len(result))
    return result


# --------------------------------------------------------------------------
# Wikipedia
# --------------------------------------------------------------------------
def _wiki(lang: str, params: dict) -> dict:
    return get_json(WIKI.format(lang=lang), {"format": "json", "formatversion": 2, **params})


def wikipedia_geosearch(
    route: geo.RouteIndex, radius_m: float, *, lang: str = "ru",
    per_circle: int = 40, max_circles: int = 16,
) -> list[dict]:
    radius = int(min(10000, max(10, radius_m)))
    samples = route.samples(radius * 1.5)
    if len(samples) > max_circles:
        k = len(samples) / max_circles
        samples = [s for i, s in enumerate(samples) if int(i % k) == 0]

    def one(sample) -> dict:
        lat, lon = sample[1]
        try:
            return _wiki(lang, {
                "action": "query", "generator": "geosearch",
                "ggscoord": f"{lat:.5f}|{lon:.5f}", "ggsradius": radius, "ggslimit": per_circle,
                "prop": "coordinates|pageprops|pageimages",
                "ppprop": "wikibase_item", "piprop": "thumbnail", "pithumbsize": 480,
            })
        except Exception as exc:
            log.warning("wikipedia geosearch: %s", exc)
            return {}

    pages: dict[int, dict] = {}
    for data in _parallel(one, samples, workers=4):
        for page in data.get("query", {}).get("pages", []):
            coords = (page.get("coordinates") or [{}])[0]
            if "lat" not in coords:
                continue
            pages[page["pageid"]] = {
                "title": page["title"], "lat": coords["lat"], "lon": coords["lon"],
                "qid": (page.get("pageprops") or {}).get("wikibase_item"),
                "thumbnail": (page.get("thumbnail") or {}).get("source"),
                "lang": lang,
            }
    log.info("wikipedia: %d статей", len(pages))
    return list(pages.values())


def wikipedia_extracts(titles: list[str], *, lang: str = "ru", batch: int = 20) -> dict[str, dict]:
    """Вводные абзацы — только для короткого списка: extracts дорогие."""
    out: dict[str, dict] = {}
    for i in range(0, len(titles), batch):
        chunk = [t for t in titles[i : i + batch] if t]
        if not chunk:
            continue
        try:
            data = _wiki(lang, {
                "action": "query", "titles": "|".join(chunk),
                "prop": "extracts|pageimages", "exintro": 1, "explaintext": 1, "exlimit": "max",
                "piprop": "thumbnail", "pithumbsize": 640, "redirects": 1,
            })
        except Exception as exc:
            log.warning("wikipedia extracts: %s", exc)
            continue
        query = data.get("query", {})
        alias: dict[str, str] = {}
        for kind in ("normalized", "redirects"):
            for item in query.get(kind, []):
                alias[item["to"]] = alias.get(item["from"], item["from"])
        for page in query.get("pages", []):
            if page.get("missing"):
                continue
            out[alias.get(page["title"], page["title"])] = {
                "title": page["title"],
                "extract": (page.get("extract") or "").strip(),
                "thumbnail": (page.get("thumbnail") or {}).get("source"),
            }
    return out
