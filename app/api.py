"""HTTP-API на FastAPI + статика фронтенда."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import pipeline, sources, taxonomy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

WEB = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="2rist", version="0.1", description="Знаковые места вдоль маршрута")


class RouteSpec(BaseModel):
    from_: str | None = Field(None, alias="from", description="адрес или место старта")
    to: str | None = None
    via: list[str] | None = None
    points: list[tuple[float, float]] | None = Field(None, description="[[lat, lon], ...]")
    polyline: str | None = Field(None, description="encoded polyline (OSRM/Google)")
    geojson: dict | None = None
    profile: str = Field("walking", pattern="^(walking|driving)$")

    model_config = {"populate_by_name": True}


class HighlightsRequest(BaseModel):
    route: RouteSpec
    radius_m: float | None = Field(None, ge=20, le=5000, description="ширина коридора; по умолчанию — от профиля")
    limit: int = Field(30, ge=1, le=200)
    interests: list[str] | None = Field(None, description="ключи из /api/categories")
    lang: str = "ru"
    with_text: bool = Field(False, description="сразу тянуть тексты; обычно их грузят лениво через /api/poi")
    with_vibe: bool = Field(True, description="профиль «настроения» участков маршрута")
    stage: str = Field("full", pattern="^(fast|full)$",
                       description="fast — черновик за 2-4 с без SPARQL и районов")


@app.get("/api/categories")
def categories() -> dict[str, str]:
    return taxonomy.CATEGORIES


@app.get("/api/suggest")
def suggest(q: str, near: str | None = None, country: str | None = None) -> list[dict]:
    """Подсказки адресов для полей маршрута."""
    if len(q.strip()) < 3:
        return []
    point = None
    if near:
        try:
            lat, lon = (float(x) for x in near.split(","))
            point = (lat, lon)
        except ValueError:
            point = None
    return sources.suggest(q.strip(), near=point, country=country)


@app.get("/api/poi-address")
def poi_address(lat: float, lon: float) -> dict:
    """Обратное геокодирование — подпись для точки, поставленной на карте."""
    return sources.reverse_geocode(lat, lon)


@app.get("/api/whereami")
def whereami() -> dict:
    """Страна и примерный центр по IP — стартовая точка для карты."""
    return sources.whereami()


@app.post("/api/route-highlights")
def route_highlights(req: HighlightsRequest) -> dict:
    spec = req.route.model_dump(by_alias=True, exclude_none=True)
    try:
        return pipeline.highlights(
            spec, radius_m=req.radius_m, limit=req.limit,
            interests=req.interests, lang=req.lang,
            with_text=req.with_text, with_vibe=req.with_vibe, stage=req.stage,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # внешний источник лёг
        logging.exception("pipeline failed")
        raise HTTPException(502, f"источник данных недоступен: {exc}") from exc


@app.get("/api/poi")
def poi_details(
    qid: str | None = None, wikipedia: str | None = None, osm: str | None = None,
    name: str = "", lang: str = "ru", lat: float = 0.0, lon: float = 0.0,
    ai: bool = False,
) -> dict:
    """Подробности по одному объекту — вызывается при открытии карточки."""
    if not (qid or wikipedia):
        raise HTTPException(400, "нужен qid или wikipedia")
    try:
        return pipeline.details(qid=qid, wikipedia=wikipedia, osm=osm, name=name,
                                lang=lang, lat=lat, lon=lon, with_ai=ai)
    except Exception as exc:
        logging.exception("details failed")
        raise HTTPException(502, f"источник данных недоступен: {exc}") from exc


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


app.mount("/web", StaticFiles(directory=WEB), name="web")
