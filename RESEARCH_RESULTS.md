# Исследование вариантов ускорения 2rist

Дата проверки: 2026-07-28. Цель: убрать публичные WDQS (12–30 с) и
Overpass (6–10 с) из горячего пути и получить 1–2 с без HTTP-кэша.
Размеры ниже — GB (10^9 байт), если явно не указано GiB.

## 1. Готовый геосрез Wikidata

**Вывод: актуального готового Parquet/CSV со всеми нужными полями не найдено; практичный путь — один раз стримингово разобрать официальный JSON dump, а не RDF truthy, потому что sitelinks и ru/en labels в JSON собраны внутри item.**

Факты:

- Официальный источник — еженедельный
  [`latest-all.json.bz2`](https://dumps.wikimedia.org/wikidatawiki/entities/latest-all.json.bz2);
  [документация](https://www.wikidata.org/wiki/Wikidata:Database_download).
  Структурированные данные Wikidata — CC0. На странице документации прямо
  рекомендован JSON, одна entity находится на одной строке, поэтому возможен
  стриминг без распаковки на диск.
- RDF truthy:
  [`latest-truthy.nt.gz`](https://dumps.wikimedia.org/wikidatawiki/entities/latest-truthy.nt.gz)
  и более компактный
  [`latest-truthy.nt.bz2`](https://dumps.wikimedia.org/wikidatawiki/entities/latest-truthy.nt.bz2).
  На проверенном индексе 2026-03-06 это 69.92 GB gzip и 42.41 GB bzip2,
  а не 40 GB для gzip. Truthy содержит только best-rank direct claims без
  qualifiers/references. Для требуемого результата его неудобно группировать:
  факты, labels и wiki pages идут отдельными RDF-тройками.
- [Kensho Derived Wikimedia Dataset](https://www.kaggle.com/datasets/kenshoresearch/kensho-derived-wikimedia-data):
  snapshot 2019-12, 51 млн nodes/120 млн edges, CSV; `item.csv` содержит только
  английские label/description, `statements.csv` — truthy Q-P-Q, `page.csv` —
  только English Wikipedia mapping. Координаты-литералы и ru labels/полные
  sitelinks нужной формы отсутствуют. Лицензия производного набора CC BY-SA 3.0.
  Не годится из-за возраста и схемы.
- [Sophox](https://wiki.openstreetmap.org/wiki/Sophox) активен для OSM, но
  **больше не содержит копию Wikidata** из-за места/производительности и
  федеративно вызывает тот же WDQS. Не является дампом.
- `wikidata-geo`: поддерживаемого официального или воспроизводимого актуального
  Parquet/CSV с заявленной схемой не найдено. Проекты с похожим названием —
  специализированные выгрузки/код, не полный P625-срез.
- [DBpedia Latest Core](https://www.dbpedia.org/resources/latest-core/) —
  ежемесячные RDF-артефакты, около 900 млн triples уже в 2021 году, преобразуют
  Wikidata к DBpedia ontology. Есть coordinates/types/labels и связи, но нет
  гарантированной сохранности именно P571/P84/P149/P1435 и полного числа
  sitelinks; CSV существует лишь для части core. Лицензия DBpedia обычно
  CC BY-SA/GFDL, тогда как исходная Wikidata — CC0. Это не эквивалентный срез.
- Hugging Face: найдены неофициальные Parquet-перекладки (например,
  [`piebro/wikidata-extraction`](https://huggingface.co/datasets/piebro/wikidata-extraction)
  и разрозненные shards), но не найден набор с одновременно P625, всеми семью
  свойствами, полными sitelinks, ru/en labels, документированной датой,
  полнотой и устойчивым pipeline. Использовать как production dependency нельзя.
- Порядок величины: в 2022 году было 9.06 млн items с P625; сейчас следует
  ожидать примерно 10–12 млн строк. Parquet с `qid`, двумя float64, sitelink
  count/ru+en titles, списками QID и двумя labels оценивается в 2–6 GB (ZSTD);
  с полным map всех sitelinks — 5–12 GB.
- Оценка для данной машины (8 ядер, 31 GB RAM, SSD): скачать 100–155 GB JSON
  gzip или около 100 GB bzip2; параллельная декомпрессия + line-by-line JSON
  filter — 4–10 часов, Python-only — 10–24 часа. Пиковая RAM 2–8 GB при
  батчах 50–200 тыс. строк; временный диск 10–25 GB без полной распаковки.
  Это инженерная оценка, а не измерение на этой машине. Для сравнения,
  построение полного truthy HDT (не фильтра) документировано как 12 часов на
  Ryzen 5 5600/4 worker/6 GB RAM и даёт около 50 GB
  ([benchmark](https://github-wiki-see.page/m/ate47/hdt-java/wiki/Part-4---Create-an-HDT-from-a-Wikidata-dump)).

**Рекомендация: делать.** Парсить официальный JSON bzip2 через `lbzip2` и
bounded worker pool, сразу писать partitioned Parquet. Это одноразовые 4–10 ч и
примерно 5–12 GB результата против 12–30 с на каждый запрос. Truthy RDF брать
только если важен минимальный download и готов отдельный join sitelinks/labels.

## 2. Overture Maps вместо Overpass

**Вывод: Overture пригоден как локальный базовый слой и дополнение OSM, но запрос к мировому S3 в горячем пути сам по себе не решает задержку и `places` не покрывает инженерные сооружения так же полно, как OSM.**

Факты:

- Текущий проверенный release:
  [`2026-06-17.0`](https://docs.overturemaps.org/release/latest/), GeoParquet в
  `s3://overturemaps-us-west-2/release/2026-06-17.0/`.
  [DuckDB-инструкция](https://docs.overturemaps.org/getting-data/duckdb/).
  `places` — CDLA Permissive 2.0; отдельные upstream records могут требовать
  attribution. `buildings`, полученные из OSM, несут ODbL; лицензию надо брать
  из `sources[].license`, а не считать весь release одной лицензией
  ([attribution guide](https://docs.overturemaps.org/attribution/)).
- Реальный замер 2026-07-28: DuckDB 1.5.4, `spatial`+`httpfs`, публичный S3,
  bbox Тбилиси `(44.65,41.60,45.00,41.85)` дал 26,593 places; Москва
  `(37.30,55.55,37.90,55.95)` — 142,134. Один cold SQL с двумя bbox занял
  **19.76 с** (user CPU 3.52 с). Отдельная агрегация категорий Тбилиси —
  24.98 с; более тяжёлая агрегация двух городов не закончилась за 90 с и была
  остановлена. Следовательно, S3 нельзя ставить в hot path; после региональной
  выгрузки локальный bbox обычно занимает десятки миллисекунд.
- В Тбилиси среди places есть 253 `landmark_and_historical_building` /
  `historic_site`; это показывает сильную бизнес/туристическую, а не
  инженерную таксономию. `places` хорошо ловит музеи, храмы и известные
  landmarks, но мосты, депо, подстанции и водонапорные башни чаще являются
  OSM features/buildings и не являются Places POI.
- Сопоставимых опубликованных city-level benchmark для заданных восьми классов
  не найдено. Поэтому утверждать процент полноты для Москвы/Тбилиси нельзя без
  отдельного object-level join. По происхождению данных Overture buildings
  включает OSM и другие источники, поэтому footprint coverage может быть не
  хуже OSM, но семантические OSM tags при нормализации теряются/сужаются.
  Для профиля 2rist OSM остаётся эталоном для `man_made=*`, `power=*`,
  `railway=*`, `bridge=*`, `historic=*`.
- `id` — GERS ID, не исходный OSM ID. `sources[]` содержит `dataset`,
  `record_id`, `license`, `update_time`: для записи из OSM `record_id` позволяет
  восстановить upstream object, но это provenance, а не стабильный top-level
  `osm_id`. В `places` есть `brand.wikidata`, но это QID **бренда**, не
  универсальный QID места. В base-темах часть OSM `wikidata=*` вынесена в
  top-level properties, однако в places/buildings нет гарантированного общего
  поля `wikidata` для каждой сущности
  ([common schema/source docs](https://docs.overturemaps.org/schema/concepts/by-theme/)).

Рабочий SQL (пересечение bbox; predicate по `bbox` обеспечивает Parquet pruning):

```sql
INSTALL spatial;
INSTALL httpfs;
LOAD spatial;
LOAD httpfs;
SET s3_region = 'us-west-2';

SELECT id, names.primary AS name, categories.primary AS category,
       sources, geometry
FROM read_parquet(
  's3://overturemaps-us-west-2/release/2026-06-17.0/theme=places/type=place/*',
  hive_partitioning = 1
)
WHERE bbox.xmin <= 45.00 AND bbox.xmax >= 44.65
  AND bbox.ymin <= 41.85 AND bbox.ymax >= 41.60;
```

Для buildings заменить путь на
`theme=buildings/type=building/*`; для строго попавшей геометрии добавить
`ST_Intersects(geometry, ST_MakeEnvelope(44.65,41.60,45.00,41.85))`.

**Рекомендация: делать пилот, но не прямую замену.** Скачивать country/city
subset раз в месяц в local Parquet и объединять с OSM: Overture places для
современных POI, OSM для исторических/инженерных tags. На тесте посчитать
precision/recall по OSM IDs и координатам; без этого заявлять полноту нельзя.

## 3. PastVu

**Вывод: API жив и геопоиск удобен, но это слой ссылок/метаданных, не лицензированный фотобанк для коммерческого кэширования изображений.**

Факты:

- Официальная [API-документация](https://docs.pastvu.com/dev/api), endpoint
  `https://api.pastvu.com/api2` (старый `pastvu.com/api2` deprecated).
  JSON-RPC-подобный GET: `method=...&params={JSON}`; ответ JSON.
- `photo.giveNearestPhotos`: `[lat,lon]`, radius по умолчанию 10 км, максимум
  1000 км, `limit <= 30`, есть `skip`, `year/year2`, `photo|painting`.
  `photo.getByBounds`: GeoJSON Polygon/MultiPolygon `[lon,lat]`, zoom; при
  `z<=16` могут вернуться clusters, при `z>=17` photos. Изображение строится из
  `file`: `https://img.pastvu.com/h/`, `/d/`, `/a/`.
- Документированного requests/sec, суточного лимита, SLA или API key не
  найдено. Администрация оставляет право вводить ограничения. Нужны
  последовательные запросы, User-Agent, локальный cache metadata и backoff.
- Единой открытой лицензии на фотографии нет. По
  [правилам](https://docs.pastvu.com/en/rules) пользователь разрешает
  бесплатное **личное некоммерческое** использование; коммерческое возможно
  только с разрешения правообладателя конкретного изображения. Метаданные также
  не опубликованы как CC0/ODbL. Без письменного разрешения хранить лучше только
  PastVu `cid`, координату, годы, подпись и URL, а показывать thumbnail с
  attribution/link.
- Проект глобальный, radius допускает 1000 км; API-пример даже использует
  Сан-Франциско. Покрытие есть далеко за Москвой/СПб, включая города бывшего
  СССР, но опубликованной таблицы полноты по городам не найдено. Оно резко
  неравномерно и пользовательское; проверять bbox каждого региона до запуска.

**Рекомендация: делать как необязательный lazy-layer.** Один bbox/nearest
запрос при открытии карточки, кэшировать метаданные, изображения не
перепубликовывать коммерчески без индивидуального разрешения.

## 4. Wikipedia Pageviews

**Вывод: для 10–50 тыс. заранее известных статей дешевле месячный Parquet dump; REST удобен только для малых инкрементов и проверки отдельных карточек.**

Факты:

- [Analytics API](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html):
  `metrics/pageviews/per-article/{project}/{access}/{agent}/{article}/{granularity}/{start}/{end}`,
  данные с 2015-07-01, JSON, без ключа. Один URL — одна article; 10–50 тыс.
  статей означают 10–50 тыс. HTTP calls. Указывать идентифицирующий User-Agent.
- Старое часто цитируемое «100 req/s» нельзя считать гарантированной квотой:
  актуальная [rate-limit policy](https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits)
  с 2026 года экспериментальная и лимиты зависят от API/authentication.
  Без договорённой квоты безопасно 5–10 req/s: 10 тыс. статей 17–33 мин,
  50 тыс. — 1.4–2.8 ч плюс retries.
- Месячные
  [`pageview_complete`](https://dumps.wikimedia.org/other/pageview_complete/monthly/)
  — Parquet, уже агрегированы по месяцу и разделяют user/spider/automated.
  Размер конкретного месяца меняется; на доступном индексе точный актуальный
  размер получить не удалось. Планировать примерно 10–30 GB download на месяц
  для всех Wikimedia projects и 1–5 минут DuckDB scan на SSD после загрузки;
  это оценка, её надо подтвердить `Content-Length` выбранного месяца.
- Сопоставление QID: из локального JSON Wikidata сохранить `sitelinks.ruwiki.title`
  и `sitelinks.enwiki.title`; нормализовать spaces/underscores и join по
  `(project,title)`. Альтернатива для разовой миграции — MediaWiki
  `action=query&prop=pageprops&ppprop=wikibase_item` батчами по 50 titles
  ([sitelinks](https://www.wikidata.org/wiki/Help:Sitelinks)).
- Лицензия агрегированных метрик: Wikimedia открывает их для анализа; сами
  структурированные QID — CC0. Не следует пытаться деанонимизировать данные.

**Рекомендация: делать через monthly dump.** Раз в месяц вычислять `qid →
ru_views_30d, en_views_30d, total`, сохранять 10–50 тыс. строк (<10 MB).
REST оставить для свежих 1–100 статей.

## 5. Платные Places API

**Вывод: для постоянной локальной базы подходит только Geoapify; Google и HERE запрещают repository/cache pattern, а Foursquare допускает отображение в сервисе, но не bulk-базу и требует проверки текущих caching guidelines.**

Факты (USD, состояние на дату отчёта):

| API | Бесплатно | Цена порядка 1,000 calls | Хранение |
|---|---:|---:|---|
| Foursquare Places | первые 500 Pro calls/месяц | $15 до 100k; $12 100–500k; $9 500k–1m | Не bulk; не отдавать третьим лицам; attribution. EULA отсылает к account-specific caching rules. Постоянную собственную POI-базу без enterprise grant не строить. |
| Geoapify Places | 3,000 credits/day, 5 rps | API10: $59/mo за 10k/day ≈ $0.20/1k при полном использовании; API25 $109 ≈ $0.15/1k | Поставщик прямо разрешает cache/store/redistribute по своим terms, с attribution. |
| Google Places New | free monthly threshold зависит от SKU; для Nearby Search Pro обычно 5,000 events/month | Nearby/Text Search Pro list price порядка $32/1k; Details Pro порядка $17/1k, volume tiers ниже | Нельзя prefetch/cache/store content; бессрочно можно хранить только `place_id`. Результаты на карте — Google Map. |
| HERE Geocoding & Search | публичный актуальный free quota/price без входа не найден | Публичная проверяемая цена за 1,000 не найдена; требуется calculator/contract | Нельзя строить repository или масштабировать один ответ на многих пользователей; cache только по headers или до 30 дней для end-user flow. |

Ссылки: [Foursquare pricing](https://foursquare.com/pricing/) и
[EULA](https://foursquare.com/legal/terms/apilicenseagreement/);
[Geoapify pricing](https://www.geoapify.com/pricing/), где Places выдаёт до
500 объектов и каждые 20 результатов стоят 1 credit
([docs](https://apidocs.geoapify.com/docs/places/));
[Google pricing](https://developers.google.com/maps/billing-and-pricing/pricing)
и [cache policy](https://developers.google.com/maps/documentation/places/web-service/policies);
[HERE terms](https://developers.here.com/terms-and-conditions), пункты 286–288.
Цены Google зависят от requested field mask: запрос тарифицируется по самому
дорогому запрошенному SKU.

**Рекомендация: Geoapify — первый платный пилот** для современной культурной
жизни, предварительно сравнить 1–2 bbox с Overture. Foursquare — только live
enrichment после юридической проверки cache TTL. Google/HERE не использовать
для предрасчёта или общей локальной таблицы.

## 6. Реестры культурного наследия

**Вывод: старый opendata.mkrf.ru фактически ненадёжен/недоступен; в Грузии жив публичный национальный GIS с >20 тыс. объектов и скачиваемыми spatial data, но стабильный публичный API и явную лицензию найти не удалось.**

Факты:

- `https://opendata.mkrf.ru/` и известная страница ЕГРОКН при проверке дали
  timeout; работоспособный API/дамп получить не удалось. Исторически портал
  публиковал XML и JSON и имел REST API, но считать его живым production
  source нельзя. Не путать с живым API
  [PRO.Культура.РФ](https://pro.culture.ru/documentation/) (`csv|json`):
  это учреждения/события/места партнёров, не полный государственный ЕГРОКН.
- Прямая проверяемая свежая bulk-ссылка ЕГРОКН, размер файла, дата обновления
  и лицензия на старом домене **не найдены**. Региональные порталы публикуют
  CSV/XML/JSON, но это фрагменты и разные схемы.
- Грузия: официальный
  [memkvidreoba.gov.ge](https://memkvidreoba.gov.ge/) жив, это Unified Cultural
  Heritage Database Национального агентства. Есть interactive map, карточки,
  координаты, protective zones и страница скачивания spatial data; портал
  публично запущен с 2017 года
  ([Council of Europe](https://www.coe.int/en/web/herein-system/georgia)).
  Он показывает записи 2026 года, то есть обновляется.
- Независимый интерфейс [Sakdrisi](https://sakdrisi.ge/) сообщает о более чем
  20,000 памятников из кадастра, включая археологические, религиозные и
  инженерные объекты по всей Грузии. Это полезное подтверждение масштаба, но
  не официальный dump.
- В открытой документации не найдены: гарантированный REST/WFS endpoint,
  rate limit, полный bulk-размер и однозначная лицензия для коммерческого
  повторного распространения. «Open/public access» не равно CC0; перед импортом
  запросить у Агентства письменные terms и рекомендуемый export URL.

**Рекомендация: Грузию делать через официальный spatial export после уточнения
лицензии; Россию пока не делать зависимостью.** Для РФ использовать OSM/Wikidata
и искать актуальную выгрузку ЕГРОКН через официальный запрос или региональные
порталы, фиксируя provenance.

## Предлагаемый порядок работ

| Шаг | Работа | Трудозатраты |
|---:|---|---:|
| 1 | Парсер Wikidata JSON → Parquet, схема/валидация, первый прогон | 16–24 ч разработки + 4–10 ч машинного времени |
| 2 | Подключить локальную таблицу вместо WDQS, spatial index/H3 и benchmark | 8–12 ч |
| 3 | Скачать Overture subsets Тбилиси/Москвы, local DuckDB; object-level сравнение с текущим Overpass по восьми классам | 12–18 ч |
| 4 | Оставить OSM для инженерных tags, объединить с Overture places и убрать Overpass из hot path | 12–20 ч |
| 5 | `pageview_complete` → месячная таблица QID popularity | 6–10 ч |
| 6 | Benchmark end-to-end, H3/precomputed corridor candidates, обновления/rollback | 12–18 ч |
| 7 | Пилот PastVu metadata и проверка прав | 4–8 ч + юридическое согласование |
| 8 | Импорт грузинского кадастра после письменной лицензии | 8–16 ч |
| 9 | Geoapify A/B-пилот современной культуры | 4–8 ч |

Итого основной performance path (шаги 1–6): **66–102 инженерных часа** плюс
первичные загрузки/парсинг. Наиболее короткий путь к цели: сначала local
Wikidata (убирает 12–30 с), затем **локальный** Overture/OSM subset (убирает
6–10 с). Прямой S3 Overture, PastVu и платные API в горячий путь не ставить.
