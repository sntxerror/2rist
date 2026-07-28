"""Текстовый слой: районы вдоль маршрута и карточки объектов.

Здесь всё детерминированно: факты берутся из Wikidata/Wikipedia, шаблон только
связывает их с положением на маршруте. Место для подключения LLM отмечено
в `describe()` — важно, что модель получает готовые факты и не сочиняет даты.
"""

from __future__ import annotations

from . import rank
from .merge import POI

SIDE_RU = {"left": "слева по ходу", "right": "справа по ходу", "on": "на самом маршруте"}

_PERIODS = [
    (-10000, 1300, "средневековье"),
    (1300, 1700, "допетровское время"),
    (1700, 1800, "XVIII век"),
    (1800, 1861, "первая половина XIX века"),
    (1861, 1917, "эпоха до революции"),
    (1917, 1932, "первые советские годы, авангард"),
    (1932, 1955, "сталинская эпоха"),
    (1955, 1991, "поздний СССР"),
    (1991, 2100, "современность"),
]


def period_of(year: int | None) -> str | None:
    if year is None:
        return None
    return next((name for lo, hi, name in _PERIODS if lo <= year < hi), None)


def districts_along(route, areas: list[dict], step_m: float = 150.0) -> list[dict]:
    """Какие районы маршрут пересекает и на каких километрах.

    Районы — не точки, а области: это скелет повествования
    («первые 2 км — Хамовники, дальше Арбат»).
    """
    from shapely.geometry import Point

    # от самых мелких к крупным: на точке оставляем самый конкретный район,
    # иначе один и тот же участок трижды подписан округом, районом и кварталом
    prepared = sorted(((a, a["poly"]) for a in areas), key=lambda x: x[0].get("size", 0))
    track: list[tuple[float, list[str]]] = []
    index = {a["name"]: a for a in areas}

    for s, (lat, lon) in route.samples(step_m):
        pt = Point(lon, lat)
        hit = next(([a["name"]] for a, poly in prepared if poly.covers(pt)), [])
        track.append((s, hit))

    # для каждого имени — непрерывные интервалы километража
    spans: dict[str, list[list[float]]] = {}
    for s, names in track:
        for name in names:
            runs = spans.setdefault(name, [])
            if runs and s - runs[-1][1] <= step_m * 1.5:
                runs[-1][1] = s
            else:
                runs.append([s, s])

    out = []
    for name, runs in spans.items():
        area = index[name]
        tags = area["tags"]
        for start, end in runs:
            if end - start < step_m:  # касание краем не считаем
                continue
            out.append({
                "name": name,
                "kind": tags.get("place") or f"admin_level={tags.get('admin_level')}",
                "from_m": round(start), "to_m": round(end),
                "length_m": round(end - start),
                "osm": area["osm"],
                "wikidata": tags.get("wikidata"),
                "wikipedia": tags.get("wikipedia"),
            })
    # самые «узкие» районы информативнее общих округов — их наверх
    out.sort(key=lambda d: (d["from_m"], d["length_m"]))
    return out


def why_here(item: rank.Scored, districts: list[dict]) -> str:
    """Связь объекта с маршрутом: где именно и в каком контексте."""
    poi = item.poi
    bits = [f"{item.s / 1000:.1f} км от старта, {SIDE_RU[item.side]}"]
    if item.side != "on":
        bits[0] += f", {round(item.offset)} м от маршрута"
    district = next((d["name"] for d in districts if d["from_m"] <= item.s <= d["to_m"]), None)
    if district:
        bits.append(district)
    if period := period_of(rank.year_of(poi)):
        bits.append(period)
    if item.heritage_title:
        bits.append(item.heritage_title)
    return "; ".join(bits)


def describe(poi: POI) -> str:
    """Краткое описание объекта.

    Сейчас — первый абзац Википедии либо описание Wikidata: всё проверяемо и
    со ссылкой на источник. Сюда же вставляется вызов LLM, если нужен связный
    рассказ по маршруту: на вход подаются только факты ниже.
    """
    if poi.extract:
        text = poi.extract.split("\n")[0]
        return text[:600] + ("…" if len(text) > 600 else "")
    if poi.description:
        return poi.description[0].upper() + poi.description[1:]
    facts = []
    if year := rank.year_of(poi):
        facts.append(str(year))
    if poi.architects:
        facts.append("арх. " + ", ".join(poi.architects[:2]))
    if poi.styles:
        facts.append(", ".join(poi.styles[:2]))
    return "; ".join(facts) or "Описание пока не найдено в открытых источниках."


def sources_of(poi: POI) -> list[dict]:
    out = []
    if poi.osm:
        kind, oid = poi.osm.split("/")
        out.append({"type": "osm", "id": poi.osm, "url": f"https://www.openstreetmap.org/{kind}/{oid}"})
    if poi.qid:
        out.append({"type": "wikidata", "id": poi.qid, "url": f"https://www.wikidata.org/wiki/{poi.qid}"})
    if poi.wikipedia and ":" in poi.wikipedia:
        lang, title = poi.wikipedia.split(":", 1)
        out.append({
            "type": "wikipedia", "id": poi.wikipedia,
            "url": f"https://{lang}.wikipedia.org/wiki/" + title.replace(" ", "_"),
        })
    return out
