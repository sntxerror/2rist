"""AI-обогащение объекта: люди, события, что было здесь раньше, этимология улицы.

Зачем это отдельно от остального пайплайна:
OSM/Wikidata/Википедия отвечают на вопрос «что это за здание». Они почти
ничего не говорят про то, кто в нём жил, что тут случилось в 1993-м, что
снесли ради этой новостройки и почему улица так называется. Такие сведения
рассыпаны по краеведческим сайтам, архивам и новостям — их достаёт поиск.

Протокол из двух шагов, потому что смешивать инструмент поиска и строгую
JSON-схему в одном вызове Gemini нельзя:

  1. research  — вызов с google_search: модель ищет и отвечает текстом,
                 API возвращает grounding-метаданные со ссылками.
  2. structure — вызов без инструментов, но с responseSchema: тот же текст
                 раскладывается в строгий JSON.

Правила против выдумок:
  * на вход подаются только проверенные факты об объекте (координаты, адрес,
    год, тип, описание из Wikidata) — модель не угадывает, о чём речь;
  * сюжет без источника выбрасывается на этапе валидации;
  * всё, в чём модель не уверена, уходит в поле `unverified` и помечается
    в интерфейсе, а не подаётся как факт.
"""

from __future__ import annotations

import json
import logging
import os
import time

from .http import session

log = logging.getLogger("2rist.ai")

API = "https://generativelanguage.googleapis.com/v1beta/models"
MODEL = os.environ.get("TRIST_AI_MODEL", "gemini-2.5-flash")
SCHEMA_VERSION = 1
MAX_STORIES = 12

# Модель охотно выдаёт «отсюда красивый вид» с уверенностью 0.9. Такое верно,
# но бесполезно: ценность в людях, событиях и в том, что было здесь раньше.
TYPE_PRIORITY = {
    "person": 1.0, "before_here": 0.98, "event": 0.95, "etymology": 0.92,
    "engineering": 0.85, "culture": 0.7, "social": 0.4,
}
MAX_PER_TYPE = {"social": 2, "culture": 3}

# Дневной потолок вызовов: поиск с грounding тарифицируется, а объектов много.
DAILY_LIMIT = int(os.environ.get("TRIST_AI_DAILY_LIMIT", "300"))
_calls: dict[str, int] = {}

STORY_TYPES = ["person", "event", "before_here", "etymology", "culture", "social", "engineering"]

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": STORY_TYPES},
                    "title": {"type": "string"},
                    "text": {"type": "string"},
                    "year_from": {"type": "integer"},
                    "year_to": {"type": "integer"},
                    "people": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                    "source_urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["type", "title", "text", "confidence"],
            },
        },
        "street": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "etymology": {"type": "string"},
                "former_names": {"type": "array", "items": {"type": "string"}},
            },
        },
        "unverified": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["stories"],
}

RESEARCH_PROMPT = """Ты краевед-исследователь. Собери нетривиальные сведения об этом месте.

ОБЪЕКТ (проверенные данные, не оспаривай их):
{facts}

Найди в поиске и изложи то, чего нет в обычной справке:
1. Люди — кто здесь жил, работал, бывал; чем интересен; в какие годы.
2. События — что здесь происходило в любую эпоху, включая последние годы.
3. Что было на этом месте раньше — что снесли, перестроили, чем было здание
   до нынешней функции.
4. Улица — происхождение названия, прежние названия и почему их меняли.
5. Современный контекст — чем место живёт сейчас, местные истории, репутация.
6. Инженерная сторона, если она есть — конструкция, технология, авторы проекта.

Требования:
* Только то, что подтверждается найденными источниками. Если по пункту ничего
  нет — пропусти его, не додумывай.
* Для каждого утверждения указывай год или период, если он известен.
* Отдельно перечисли то, что встречается как городская легенда или требует
  проверки, — так и назови.
* Не пересказывай общеизвестное описание объекта, оно уже есть.
* Отвечай на русском."""

STRUCTURE_PROMPT = """Разложи этот текст в JSON по схеме. Ничего не добавляй от себя.

Типы сюжетов:
  person       — человек, связанный с местом
  event        — событие
  before_here  — что было на этом месте раньше
  etymology    — происхождение названия улицы/места
  culture      — культурный контекст, упоминания в книгах, кино, музыке
  social       — как место живёт сейчас, кто им пользуется
  engineering  — конструкция, технология, авторы инженерного решения

confidence: 0.9 — подтверждено несколькими источниками, 0.6 — одним,
0.3 — упоминается как легенда. В source_urls переноси ссылки из текста.
В unverified вынеси всё непроверенное.

ТЕКСТ:
{text}"""


class AIUnavailable(RuntimeError):
    """Ключ не настроен или квота исчерпана."""


def api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise AIUnavailable(
            "нет GEMINI_API_KEY: создайте ключ в проекте с включённым Gemini API "
            "и положите в переменную окружения"
        )
    return key


def _budget_check() -> None:
    today = time.strftime("%Y-%m-%d")
    used = _calls.get(today, 0)
    if used >= DAILY_LIMIT:
        raise AIUnavailable(f"дневной лимит вызовов исчерпан ({DAILY_LIMIT})")
    _calls[today] = used + 1


def _generate(body: dict, *, ttl_days: int = 90) -> dict:
    _budget_check()
    r = session.post(
        f"{API}/{MODEL}:generateContent",
        params={"key": api_key()},
        json=body,
        timeout=120,
        expire_after=ttl_days * 24 * 3600,
    )
    if r.status_code >= 400:
        raise AIUnavailable(f"Gemini {r.status_code}: {r.text[:200]}")
    return r.json()


def _text_of(resp: dict) -> str:
    parts = resp.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "\n".join(p.get("text", "") for p in parts).strip()


def _grounding_sources(resp: dict) -> list[dict]:
    """Ссылки, которыми модель реально пользовалась, — из метаданных API."""
    meta = resp.get("candidates", [{}])[0].get("groundingMetadata", {})
    out = []
    for chunk in meta.get("groundingChunks", []) or []:
        web = chunk.get("web") or {}
        if web.get("uri"):
            out.append({"title": web.get("title") or web["uri"], "url": web["uri"]})
    seen, uniq = set(), []
    for s in out:
        if s["url"] not in seen:
            seen.add(s["url"])
            uniq.append(s)
    return uniq


def research(facts: dict) -> dict:
    """Полный цикл обогащения одного объекта."""
    facts_text = json.dumps(facts, ensure_ascii=False, indent=2)

    grounded = _generate({
        "contents": [{"parts": [{"text": RESEARCH_PROMPT.format(facts=facts_text)}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2},
    })
    raw_text = _text_of(grounded)
    sources = _grounding_sources(grounded)
    if not raw_text:
        return {"stories": [], "sources": [], "unverified": [], "model": MODEL}

    structured = _generate({
        "contents": [{"parts": [{"text": STRUCTURE_PROMPT.format(text=raw_text)}]}],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    })
    try:
        data = json.loads(_text_of(structured) or "{}")
    except json.JSONDecodeError as exc:
        log.warning("ai: невалидный JSON: %s", exc)
        return {"stories": [], "sources": sources, "unverified": [], "model": MODEL}

    return _validate(data, sources)


def _validate(data: dict, sources: list[dict]) -> dict:
    """Отбраковка: сюжет без источника или без содержания не публикуется."""
    known = {s["url"] for s in sources}
    stories = []
    for item in data.get("stories", []):
        if not item.get("text") or len(item["text"]) < 40:
            continue
        urls = [u for u in item.get("source_urls", []) if u in known] or [s["url"] for s in sources[:2]]
        if not urls:
            continue
        stories.append({
            "type": item.get("type", "event"),
            "title": item.get("title", "").strip(),
            "text": item["text"].strip(),
            "years": [item.get("year_from"), item.get("year_to")],
            "people": item.get("people", []),
            "confidence": round(float(item.get("confidence", 0.5)), 2),
            "sources": [s for s in sources if s["url"] in urls][:3],
        })
    stories.sort(key=lambda s: -TYPE_PRIORITY.get(s["type"], 0.6) * s["confidence"])
    picked, per_type = [], {}
    for story in stories:
        kind = story["type"]
        per_type[kind] = per_type.get(kind, 0) + 1
        if per_type[kind] > MAX_PER_TYPE.get(kind, MAX_STORIES):
            continue
        picked.append(story)
        if len(picked) >= MAX_STORIES:
            break
    stories = picked
    return {
        "stories": stories,
        "street": data.get("street") or None,
        "unverified": data.get("unverified", []),
        "sources": sources,
        "model": MODEL,
        "schema_version": SCHEMA_VERSION,
    }


def facts_for(poi: dict, *, address: dict | None = None, district: str | None = None) -> dict:
    """Что подаём модели: только проверенное, чтобы ей нечего было выдумывать."""
    return {
        "название": poi.get("name"),
        "координаты": poi.get("coords"),
        "адрес": address or {},
        "район": district,
        "тип": poi.get("category_labels") or poi.get("wd_types"),
        "год": poi.get("year"),
        "архитекторы": poi.get("architects"),
        "описание_из_wikidata": poi.get("description"),
        "wikidata": poi.get("qid"),
        "wikipedia": poi.get("wikipedia"),
    }
