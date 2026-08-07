"""Нормализация и склейка объектов из трёх источников.

Самая грязная часть пайплайна: один памятник приходит из OSM, из Wikidata и
из Википедии под разными именами и с разными координатами.

  1. Совпал QID — это один объект, вопросов нет.
  2. Иначе — пространственный поиск (STRtree) + похожесть имени (rapidfuzz).
  3. Слияние полей по приоритету источников; геометрия — из OSM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from shapely.strtree import STRtree

from . import taxonomy

# Родовые слова мешают сравнению: «Памятник Пушкину» ≈ «Пушкину, памятник»
_GENERIC = re.compile(
    r"\b(памятник|мемориал|музей|храм|церковь|собор|дом|здание|усадьба|особняк|монумент|"
    r"бюст|станция|имени|им|the|of|museum|monument|memorial|church|house)\b", re.I)
_PUNCT = re.compile(r"[^\w\s]", re.U)

# Безымянные, но заметные объекты: подписываем по классу, а не «historic: ruins»
FALLBACK_NAMES = {
    "city_gate": "Городские ворота", "citywalls": "Городская стена", "ruins": "Руины",
    "monument": "Монумент", "memorial": "Мемориал", "statue": "Скульптура",
    "archaeological_site": "Археологический памятник", "wayside_cross": "Придорожный крест",
    "wayside_shrine": "Придорожная часовня", "boundary_stone": "Межевой камень",
    "castle": "Крепость", "fort": "Форт", "manor": "Усадьба", "tomb": "Гробница",
    "mural": "Мурал", "graffiti": "Граффити", "sculpture": "Скульптура",
    "installation": "Инсталляция", "mosaic": "Мозаика", "bust": "Бюст",
    "bridge": "Мост", "tunnel": "Тоннель", "tower": "Башня", "obelisk": "Обелиск",
    "water_tower": "Водонапорная башня", "windmill": "Ветряная мельница",
    "watermill": "Водяная мельница", "lighthouse": "Маяк", "chimney": "Заводская труба",
    "crane": "Портальный кран", "gasometer": "Газгольдер", "silo": "Элеватор",
    "cooling_tower": "Градирня", "pier": "Пирс",
}


@dataclass
class POI:
    name: str
    lat: float
    lon: float
    categories: list[str] = field(default_factory=list)
    class_weight: float = 0.0
    tags: dict[str, str] = field(default_factory=dict)
    qid: str | None = None
    wikipedia: str | None = None      # "ru:Заголовок"
    osm: str | None = None            # "way/123"
    sitelinks: int = 0
    inception: str | None = None
    architects: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    heritage_labels: list[str] = field(default_factory=list)
    description: str | None = None
    wd_types: list[str] = field(default_factory=list)
    image: str | None = None
    extract: str | None = None
    sources: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.qid or self.osm or f"{self.name}@{self.lat:.5f},{self.lon:.5f}"


def _norm(name: str) -> str:
    return " ".join(_GENERIC.sub(" ", _PUNCT.sub(" ", (name or "").lower().replace("ё", "е"))).split())


def _similar(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    return fuzz.token_set_ratio(na, nb) / 100.0 if na and nb else 0.0


# --- нормализация источников ----------------------------------------------
def from_osm(rec: dict, lang: str = "ru") -> POI | None:
    tags = rec["tags"]
    # в не-русскоязычных странах у большинства объектов только местное name,
    # поэтому английский вариант читабельнее оригинала
    name = (tags.get(f"name:{lang}") or tags.get("name:en") or tags.get("name")
            or tags.get("official_name"))
    if not name:
        # безымянное берём только если это заведомо заметный класс
        if not (tags.get("historic") or tags.get("tourism") == "artwork"):
            return None
        name = tags.get("inscription") or next(
            (FALLBACK_NAMES.get(tags[k], tags[k].replace("_", " ").capitalize())
             for k in ("historic", "memorial", "artwork_type", "man_made") if k in tags),
            "Объект без названия")
    qid = tags.get("wikidata")
    return POI(
        name=name, lat=rec["lat"], lon=rec["lon"], tags=tags,
        qid=qid if (qid or "").startswith("Q") else None,
        wikipedia=tags.get("wikipedia"), osm=f"{rec['osm_type']}/{rec['osm_id']}",
        inception=tags.get("start_date"),
        architects=[a for a in [tags.get("architect")] if a],
        sources=["osm"],
    )


def from_wikidata(rec: dict) -> POI | None:
    if rec.get("lat") is None:
        return None
    return POI(
        name=rec.get("label") or rec["qid"], lat=rec["lat"], lon=rec["lon"], qid=rec["qid"],
        sitelinks=rec.get("sitelinks", 0), inception=rec.get("inception"),
        architects=list(rec["architects"]), styles=list(rec["styles"]),
        heritage_labels=list(rec["heritage"]), description=rec.get("description"),
        wd_types=list(rec["types"]), image=rec.get("image"), sources=["wikidata"],
    )


def from_wikipedia(rec: dict) -> POI:
    return POI(
        name=rec["title"], lat=rec["lat"], lon=rec["lon"], qid=rec.get("qid"),
        wikipedia=f"{rec['lang']}:{rec['title']}", image=rec.get("thumbnail"),
        sources=["wikipedia"],
    )


def apply_wikidata(poi: POI, rec: dict) -> None:
    poi.sitelinks = max(poi.sitelinks, rec.get("sitelinks", 0))
    for attr, key in (("inception", "inception"), ("description", "description"), ("image", "image")):
        setattr(poi, attr, getattr(poi, attr) or rec.get(key))
    for attr, key in (("architects", "architects"), ("styles", "styles"),
                      ("heritage_labels", "heritage"), ("wd_types", "types")):
        setattr(poi, attr, getattr(poi, attr) or list(rec.get(key) or []))
    if rec.get("label") and (poi.name in FALLBACK_NAMES.values() or poi.name == "Объект без названия"):
        poi.name = rec["label"]
    if "wikidata" not in poi.sources:
        poi.sources.append("wikidata")


def _absorb(base: POI, other: POI) -> None:
    for attr in ("qid", "osm", "wikipedia", "image", "description", "inception"):
        setattr(base, attr, getattr(base, attr) or getattr(other, attr))
    for attr in ("architects", "styles", "heritage_labels", "wd_types"):
        setattr(base, attr, getattr(base, attr) or getattr(other, attr))
    base.sitelinks = max(base.sitelinks, other.sitelinks)
    base.tags = {**other.tags, **base.tags}
    base.sources += [s for s in other.sources if s not in base.sources]
    if len(other.name) > len(base.name) and not base.qid:
        base.name = other.name


# --- дедупликация ----------------------------------------------------------
def dedupe(pois: list[POI], route, *, max_dist_m: float = 70.0, min_sim: float = 0.62) -> list[POI]:
    by_qid: dict[str, POI] = {}
    rest: list[POI] = []
    for p in pois:
        if p.qid:
            if p.qid in by_qid:
                _absorb(by_qid[p.qid], p)
            else:
                by_qid[p.qid] = p
        else:
            rest.append(p)

    merged = list(by_qid.values())
    points = [route.point_m(p.lat, p.lon) for p in merged]
    tree = STRtree(points) if points else None
    # безымянные накапливаем в сетке ~100 м, чтобы склеивать и между собой
    cell = max_dist_m * 1.5
    grid: dict[tuple[int, int], list[tuple[POI, object]]] = {}

    for p in rest:
        pt = route.point_m(p.lat, p.lon)
        candidates = [(merged[i], points[i]) for i in (tree.query(pt.buffer(max_dist_m)) if tree is not None else [])]
        gi, gj = int(pt.x // cell), int(pt.y // cell)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                candidates += grid.get((gi + di, gj + dj), [])

        best: tuple[float, POI] | None = None
        for cand, cand_pt in candidates:
            dist = pt.distance(cand_pt)
            sim = _similar(p.name, cand.name)
            if dist > max_dist_m or sim < min_sim:
                continue
            score = sim - dist / (max_dist_m * 10)
            if best is None or score > best[0]:
                best = (score, cand)

        if best:
            _absorb(best[1], p)
        else:
            merged.append(p)
            grid.setdefault((gi, gj), []).append((p, pt))

    for p in merged:
        p.categories, p.class_weight = taxonomy.classify(p.tags, p.wd_types, p.description)
    return merged
