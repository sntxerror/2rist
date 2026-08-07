"""Единая HTTP-сессия: дисковый кэш + троттлинг на хост.

requests-cache отвечает за кэш (SQLite), requests-ratelimiter — за вежливость
к бесплатным публичным эндпоинтам. Больше про транспорт думать не нужно.
"""

from __future__ import annotations

from datetime import timedelta
from http.cookiejar import DefaultCookiePolicy
from pathlib import Path

import requests
from requests_cache import CacheMixin, SQLiteCache
from requests_ratelimiter import LimiterMixin

ROOT = Path(__file__).resolve().parent.parent
CACHE_DB = ROOT / "data" / "cache" / "http"
CACHE_DB.parent.mkdir(parents=True, exist_ok=True)

USER_AGENT = "2rist/0.1 (route-highlights prototype; +https://github.com/local/2rist)"


class Session(CacheMixin, LimiterMixin, requests.Session):
    """Кэш поверх ограничителя частоты — рекомендованная связка обеих библиотек."""


def _make(per_second: float) -> Session:
    """Отдельная сессия под свой лимит частоты."""
    s = Session(
        backend=SQLiteCache(str(CACHE_DB)),
        expire_after=timedelta(days=14),
        allowable_methods=("GET", "POST"),  # Overpass и SPARQL ходят POST'ом
        allowable_codes=(200,),
        stale_if_error=True,
        per_second=per_second,
        per_host=True,
    )
    s.headers["User-Agent"] = USER_AGENT
    # Wikipedia отдаёт Vary: Cookie и ставит куки на первом же ответе — из-за
    # этого второй запрос уходит с другими заголовками и мимо кэша.
    s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    return s


# Overpass/WDQS/Wikipedia спокойно держат несколько запросов в секунду.
session = _make(4)
# У Nominatim в правилах жёсткий потолок: 1 запрос в секунду.
slow_session = _make(1)


def get_json(url: str, params: dict | None = None, *, slow: bool = False, **kw) -> dict:
    s = slow_session if slow else session
    r = s.get(url, params=params, timeout=kw.pop("timeout", 90), **kw)
    r.raise_for_status()
    return r.json()


def post_json(url: str, data, **kw) -> dict:
    r = session.post(url, data=data, timeout=kw.pop("timeout", 180), **kw)
    r.raise_for_status()
    return r.json()


def cached_only(session_kw: dict | None = None) -> dict:
    """Флаг для requests-cache: отдать только из кэша, в сеть не ходить."""
    return {"only_if_cached": True, **(session_kw or {})}
