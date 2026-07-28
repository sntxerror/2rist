"""Геометрия маршрута поверх shapely + pyproj.

Маршрут — не набор точек, а линия с километражом. Любой объект проецируется
на неё и получает (км от старта, отступ в метрах, сторона по ходу движения).
Именно это превращает список POI в последовательный рассказ.
"""

from __future__ import annotations

from dataclasses import dataclass

import polyline as polyline_lib
from pyproj import CRS, Transformer
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, shape
from shapely.ops import polygonize, transform, unary_union

decode_polyline = polyline_lib.decode  # [(lat, lon), ...]
encode_polyline = polyline_lib.encode


@dataclass(slots=True)
class Position:
    """Положение объекта относительно маршрута."""

    s: float  # метры от старта вдоль маршрута
    offset: float  # метры от оси маршрута
    side: str  # left | right | on


class RouteIndex:
    """Линия маршрута в метрической проекции + линейная привязка."""

    def __init__(self, latlons: list[tuple[float, float]]):
        if len(latlons) < 2:
            raise ValueError("маршрут должен содержать минимум 2 точки")
        self.latlons = latlons
        lat0 = sum(p[0] for p in latlons) / len(latlons)
        lon0 = sum(p[1] for p in latlons) / len(latlons)
        # Азимутальная равнопромежуточная с центром на маршруте: расстояния
        # и буферы считаются честно в метрах без выбора UTM-зоны.
        crs = CRS.from_proj4(f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +units=m +datum=WGS84")
        self._fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
        self._inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
        self.line_ll = LineString([(lon, lat) for lat, lon in latlons])
        self.line = transform(self._fwd, self.line_ll)
        self.length = self.line.length

    # --- преобразования -------------------------------------------------
    def to_m(self, geom):
        return transform(self._fwd, geom)

    def to_ll(self, geom):
        return transform(self._inv, geom)

    def point_m(self, lat: float, lon: float) -> Point:
        return Point(self._fwd(lon, lat))

    # --- запросы --------------------------------------------------------
    def project(self, lat: float, lon: float) -> Position:
        p = self.point_m(lat, lon)
        s = self.line.project(p)
        c = self.line.interpolate(s)
        offset = p.distance(c)
        if offset < 8.0:
            return Position(s, offset, "on")
        # направление движения в этой точке -> с какой стороны объект
        ahead = self.line.interpolate(min(s + 5.0, self.length))
        vx, vy = ahead.x - c.x, ahead.y - c.y
        cross = vx * (p.y - c.y) - vy * (p.x - c.x)
        return Position(s, offset, "left" if cross > 0 else "right")

    def point_at(self, s: float) -> tuple[float, float]:
        p = self.line.interpolate(max(0.0, min(s, self.length)))
        lon, lat = self._inv(p.x, p.y)
        return lat, lon

    def samples(self, step_m: float) -> list[tuple[float, tuple[float, float]]]:
        """Равномерные пробы вдоль маршрута: [(s, (lat, lon)), ...]."""
        n = max(1, int(self.length // step_m))
        out = [(i * step_m, self.point_at(i * step_m)) for i in range(n + 1)]
        if out[-1][0] < self.length:
            out.append((self.length, self.latlons[-1]))
        return out

    def corridor(self, radius_m: float):
        """Полигон коридора в WGS84 — для отрисовки и bbox-запросов."""
        return self.to_ll(self.line.buffer(radius_m, quad_segs=4))

    def bbox(self, pad_m: float = 0.0) -> tuple[float, float, float, float]:
        """(south, west, north, east)."""
        minx, miny, maxx, maxy = self.corridor(max(pad_m, 1.0)).bounds
        return miny, minx, maxy, maxx


def build_polygon(rings: list[list[tuple[float, float]]]) -> Polygon | MultiPolygon | None:
    """Собирает полигон района из кусков границы (`out geom` у Overpass).

    shapely.polygonize сам склеивает разорванные линии в кольца — вручную это
    полсотни строк.
    """
    lines = [LineString([(lon, lat) for lat, lon in r]) for r in rings if len(r) >= 2]
    if not lines:
        return None
    polys = list(polygonize(unary_union(lines)))
    if not polys:
        return None
    return unary_union(polys)


def geojson_line(gj: dict) -> list[tuple[float, float]]:
    if gj.get("type") == "Feature":
        gj = gj["geometry"]
    geom = shape(gj)
    if geom.geom_type != "LineString":
        raise ValueError("geojson: нужен LineString")
    return [(y, x) for x, y in geom.coords]
