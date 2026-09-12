"""MPSTATS MCP server.

Обёртка над MPSTATS API (аналитика Wildberries, Ozon, Яндекс Маркет) в виде
MCP-сервера со Streamable HTTP транспортом. Подключается к claude.ai как
кастомный коннектор.

Переменные окружения:
  MPSTATS_TOKEN     — токен MPSTATS API (обязательно)
  MCP_PATH          — путь MCP-эндпоинта, по умолчанию /mcp.
                      Секретный путь = защита от чужих запросов, например
                      /s/8f3c.../mcp
  PORT              — порт, по умолчанию 8000
  MAX_RESPONSE_CHARS — максимальный размер ответа в символах (по умолчанию 60000)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Literal
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

TOKEN = os.environ.get("MPSTATS_TOKEN", "").strip()
MCP_PATH = os.environ.get("MCP_PATH", "/mcp").strip() or "/mcp"
PORT = int(os.environ.get("PORT", "8000"))
MAX_RESPONSE_CHARS = int(os.environ.get("MAX_RESPONSE_CHARS", "60000"))
TIMEOUT = float(os.environ.get("MPSTATS_TIMEOUT", "90"))

API_ROOT = "https://mpstats.io/api"
BASES = {
    "wb": f"{API_ROOT}/analytics/v1/wb",
    "oz": f"{API_ROOT}/analytics/v1/oz",
    "ym": API_ROOT,
}

MARKET_ALIASES = {
    "wb": "wb", "wildberries": "wb", "вб": "wb", "вайлдберриз": "wb",
    "oz": "oz", "ozon": "oz", "озон": "oz",
    "ym": "ym", "yandex": "ym", "yandex_market": "ym", "яндекс": "ym",
    "яндекс маркет": "ym", "ям": "ym",
}

Marketplace = Literal["wb", "oz", "ym"]

mcp = FastMCP(
    name="MPSTATS",
    instructions=(
        "Аналитика маркетплейсов MPSTATS: Wildberries (wb), Ozon (oz), "
        "Яндекс Маркет (ym).\n"
        "Порядок работы: сначала найти точный путь категории через "
        "mpstats_categories (поиск по подстроке), затем запрашивать отчёты.\n"
        "Даты всегда в формате YYYY-MM-DD, период d1..d2. Для сравнения "
        "периодов — mpstats_compare.\n"
        "Списочные отчёты постраничные: параметры start/limit, сортировка "
        "sort_by/sort_dir.\n"
        "Если готового инструмента не хватает — mpstats_request (сырой вызов "
        "любого метода API).\n"
        "Данные MPSTATS содержат выбросы (ошибки цен продавцов, несовпадение "
        "единиц). Перед выводом проверять порядок величин и явно помечать "
        "аномалии, а не выдавать их за факт."
    ),
)


# --------------------------------------------------------------------------- #
# HTTP-слой
# --------------------------------------------------------------------------- #
def _market(value: str) -> str:
    key = (value or "wb").strip().lower()
    if key not in MARKET_ALIASES:
        raise ValueError(
            f"Неизвестный маркетплейс: {value}. Допустимо: wb, oz, ym."
        )
    return MARKET_ALIASES[key]


def _body(start: int, limit: int, sort_by: str | None, sort_dir: str,
          filters: dict[str, Any] | None) -> dict[str, Any]:
    sort_model: list[dict[str, str]] = []
    if sort_by:
        sort_model = [{"colId": sort_by, "sort": sort_dir}]
    start = max(0, int(start))
    # API не отдаёт больше 500 строк за запрос
    limit = min(max(1, int(limit)), 500)
    return {
        "startRow": start,
        "endRow": start + limit,
        "filterModel": filters or {},
        "sortModel": sort_model,
    }


def _trim(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= MAX_RESPONSE_CHARS:
        return text
    cut = text[:MAX_RESPONSE_CHARS]
    return (
        cut
        + f'\n\n[ОБРЕЗАНО: ответ {len(text)} символов, показано '
        f'{MAX_RESPONSE_CHARS}. Уменьши limit или сузь период/фильтр.]'
    )


async def _call(method: str, base: str, path: str,
                params: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None) -> Any:
    if not TOKEN:
        raise RuntimeError(
            "MPSTATS_TOKEN не задан на сервере — добавь переменную окружения."
        )
    url = path if path.startswith("http") else f"{base.rstrip('/')}/{path.lstrip('/')}"
    clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    headers = {
        "X-Mpstats-TOKEN": TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.request(
            method.upper(), url, params=clean, headers=headers,
            json=body if method.upper() == "POST" else None,
        )
    if resp.status_code == 401:
        raise RuntimeError("401: неверный или просроченный токен MPSTATS.")
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After", "?")
        raise RuntimeError(
            f"429: превышен лимит запросов MPSTATS. Повторить через {retry} с."
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"{resp.status_code}: {resp.text[:500]}")
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:MAX_RESPONSE_CHARS]}


# --------------------------------------------------------------------------- #
# Роутинг отчётов
# --------------------------------------------------------------------------- #
# Отчёты, доступные по сущностям category / brand / seller для WB и Ozon.
WB_ENTITY_REPORTS = {
    "items", "categories", "brands", "sellers", "trends", "by_date",
    "price_segmentation", "subjects", "keywords", "warehouses", "geography",
    "niches",
}
OZ_ENTITY_REPORTS = {
    "items", "categories", "brands", "sellers", "trends", "by_date",
    "price_segmentation", "keywords", "niches", "geography",
}

# Яндекс Маркет живёт на старых путях ym/get/...
YM_ROUTES = {
    ("category", "items"): ("POST", "ym/get/category"),
    ("category", "categories"): ("GET", "ym/get/category/categories"),
    ("category", "sellers"): ("GET", "ym/get/category/sellers"),
    ("category", "brands"): ("GET", "ym/get/category/brands"),
    ("category", "by_date"): ("GET", "ym/get/category/by_date"),
    ("category", "price_segmentation"): ("GET", "ym/get/category/price_segmentation"),
    ("brand", "items"): ("POST", "ym/get/brand"),
    ("brand", "categories"): ("GET", "ym/get/brand/categories"),
    ("brand", "sellers"): ("GET", "ym/get/brand/sellers"),
    ("brand", "by_date"): ("GET", "ym/get/brand/by_date"),
    ("brand", "price_segmentation"): ("GET", "ym/get/brand/price_segmentation"),
    ("seller", "items"): ("POST", "ym/get/seller"),
    ("seller", "categories"): ("GET", "ym/get/seller/categories"),
    ("seller", "brands"): ("GET", "ym/get/seller/brands"),
    ("seller", "by_date"): ("GET", "ym/get/seller/by_date"),
    ("seller", "price_segmentation"): ("GET", "ym/get/seller/price_segmentation"),
}

SKU_REPORTS_WB = {
    "card": "", "full": "full", "by_period": "by_period",
    "balance": "balance/stores", "balance_sizes": "balance/sizes",
    "balance_stores_sizes": "balance/stores_and_sizes",
    "balance_colors": "balance/colors",
    "sales": "sales/stores", "sales_sizes": "sales/sizes",
    "sales_heatmap": "sales/heatmap", "stores": "stores",
    "search_stats": "search_stats", "keywords": "keywords",
    "keywords_hourly": "keywords/hourly", "comments": "comments",
    "faq": "faq", "photos_history": "photos_history",
}
SKU_REPORTS_OZ = {
    "card": "full", "full": "full", "sales": "sales", "by_day": "by_day",
    "balance": "balance", "categories": "categories", "keywords": "keywords",
    "by_period": "by_period", "search_stats": "search_stats",
    "stores": "stores", "comments": "comments",
    "comments_ai": "comments/ai_recommendations",
}


async def _entity_report(market: str, entity: str, report: str, path: str,
                         d1: str | None, d2: str | None, start: int, limit: int,
                         sort_by: str | None, sort_dir: str,
                         filters: dict[str, Any] | None, fbs: int | None) -> Any:
    params = {"path": path, "d1": d1, "d2": d2}
    if fbs is not None:
        params["fbs"] = fbs

    if market == "ym":
        route = YM_ROUTES.get((entity, report))
        if not route:
            raise ValueError(
                f"Для Яндекс Маркета нет отчёта {entity}/{report}. "
                f"Доступно: {sorted(r for (e, r) in YM_ROUTES if e == entity)}"
            )
        method, api_path = route
        body = _body(start, limit, sort_by, sort_dir, filters) if method == "POST" else None
        return await _call(method, BASES["ym"], api_path, params, body)

    allowed = WB_ENTITY_REPORTS if market == "wb" else OZ_ENTITY_REPORTS
    if report not in allowed:
        raise ValueError(
            f"Отчёт {report} недоступен для {market}/{entity}. "
            f"Доступно: {sorted(allowed)}"
        )
    body = _body(start, limit, sort_by, sort_dir, filters)
    return await _call("POST", BASES[market], f"{entity}/{report}", params, body)


# Дерево категорий кэшируется: API отдаёт максимум 500 строк за запрос,
# а полное дерево — это десятки страниц, то есть десятки единиц лимита.
_CAT_CACHE: dict[str, tuple[float, list[Any]]] = {}
_CAT_TTL = 21600  # 6 часов
_CAT_PAGE = 500
_CAT_BATCH = 6  # страниц за один заход, параллельно
_CAT_LOCKS: dict[str, asyncio.Lock] = {}


async def _category_page(market: str, date: str | None, start: int) -> list[Any]:
    body = {"startRow": start, "endRow": start + _CAT_PAGE,
            "filterModel": {}, "sortModel": []}
    data = await _call("POST", BASES[market], "category/list", {"date": date}, body)
    rows = data.get("data", data) if isinstance(data, dict) else data
    return rows if isinstance(rows, list) else []


async def _category_tree(market: str, date: str | None) -> list[Any]:
    key = f"{market}:{date or ''}"
    lock = _CAT_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _CAT_CACHE.get(key)
        now = time.time()
        if hit and now - hit[0] < _CAT_TTL:
            return hit[1]
        rows_all: list[Any] = []
        start = 0
        while start < 300000:
            pages = await asyncio.gather(*[
                _category_page(market, date, start + i * _CAT_PAGE)
                for i in range(_CAT_BATCH)
            ])
            for rows in pages:
                rows_all.extend(rows)
            if any(len(rows) < _CAT_PAGE for rows in pages):
                break
            start += _CAT_PAGE * _CAT_BATCH
        _CAT_CACHE[key] = (time.time(), rows_all)
        return rows_all


# --------------------------------------------------------------------------- #
# Инструменты
# --------------------------------------------------------------------------- #
@mcp.tool
async def mpstats_api_limit() -> str:
    """Остаток лимита запросов к API MPSTATS по текущему тарифу.

    Вызывать первым при подозрении на 429 или перед тяжёлой серией запросов.
    """
    return _trim(await _call("GET", API_ROOT, "user/report_api_limit"))


@mcp.tool
async def mpstats_categories(
    marketplace: str = "wb",
    query: str = "",
    limit: int = 100,
    date: str | None = None,
) -> str:
    """Дерево категорий маркетплейса с поиском по подстроке.

    Возвращает точные пути категорий (`path`), которые нужны всем остальным
    инструментам. Дерево большое — всегда сужай через query.

    Args:
        marketplace: wb | oz (для ym дерево не отдаётся, используй известный путь)
        query: подстрока для поиска, регистронезависимо (например «чехлы»)
        limit: сколько категорий вернуть (по умолчанию 100)
        date: дата среза YYYY-MM-DD (опционально)
    """
    market = _market(marketplace)
    if market == "ym":
        raise ValueError("Для Яндекс Маркета список категорий по API не отдаётся.")
    rows = await _category_tree(market, date)
    needle = query.strip().lower()
    if needle:
        rows = [r for r in rows
                if needle in str(r.get("path", "")).lower()
                or needle in str(r.get("name", "")).lower()]
    total = len(rows)
    rows = rows[: max(1, limit)]
    return _trim({"total_found": total, "shown": len(rows), "categories": rows})


@mcp.tool
async def mpstats_category(
    path: str,
    d1: str,
    d2: str,
    marketplace: str = "wb",
    report: str = "items",
    start: int = 0,
    limit: int = 50,
    sort_by: str | None = "revenue",
    sort_dir: str = "desc",
    fbs: int | None = None,
    filters: dict[str, Any] | None = None,
) -> str:
    """Аналитика категории: товары, разрезы по брендам/продавцам, тренды.

    Args:
        path: полный путь категории из mpstats_categories
        d1: начало периода YYYY-MM-DD
        d2: конец периода YYYY-MM-DD
        marketplace: wb | oz | ym
        report: items (товары) | categories (подкатегории) | brands | sellers |
            trends | by_date | price_segmentation | keywords | subjects (wb) |
            niches (oz) | warehouses (wb) | geography
        start: смещение для постраничной выдачи
        limit: сколько строк вернуть (максимум 5000, по умолчанию 50)
        sort_by: колонка сортировки (revenue, sales, balance, rating, comments)
        sort_dir: asc | desc
        fbs: 1 — учитывать FBS-товары
        filters: filterModel в формате MPSTATS (ag-grid)
    """
    return _trim(await _entity_report(_market(marketplace), "category", report,
                                      path, d1, d2, start, limit, sort_by,
                                      sort_dir, filters, fbs))


@mcp.tool
async def mpstats_brand(
    brand: str,
    d1: str,
    d2: str,
    marketplace: str = "wb",
    report: str = "items",
    start: int = 0,
    limit: int = 50,
    sort_by: str | None = "revenue",
    sort_dir: str = "desc",
    fbs: int | None = None,
    filters: dict[str, Any] | None = None,
) -> str:
    """Аналитика бренда: товары, категории, продавцы, динамика, тренды.

    Args:
        brand: название бренда как в карточках маркетплейса
        d1: начало периода YYYY-MM-DD
        d2: конец периода YYYY-MM-DD
        marketplace: wb | oz | ym
        report: items | categories | sellers | by_date | trends |
            price_segmentation | keywords | subjects (wb) | niches (oz) |
            warehouses (wb) | geography
        start: смещение постраничной выдачи
        limit: сколько строк вернуть
        sort_by: колонка сортировки
        sort_dir: asc | desc
        fbs: 1 — учитывать FBS
        filters: filterModel в формате MPSTATS
    """
    return _trim(await _entity_report(_market(marketplace), "brand", report,
                                      brand, d1, d2, start, limit, sort_by,
                                      sort_dir, filters, fbs))


@mcp.tool
async def mpstats_seller(
    seller: str,
    d1: str,
    d2: str,
    marketplace: str = "wb",
    report: str = "items",
    start: int = 0,
    limit: int = 50,
    sort_by: str | None = "revenue",
    sort_dir: str = "desc",
    fbs: int | None = None,
    filters: dict[str, Any] | None = None,
) -> str:
    """Аналитика продавца: ассортимент, категории, бренды, динамика.

    Args:
        seller: id продавца или название (для wb — supplier_id)
        d1: начало периода YYYY-MM-DD
        d2: конец периода YYYY-MM-DD
        marketplace: wb | oz | ym
        report: items | categories | brands | by_date | trends |
            price_segmentation | keywords | subjects (wb) | niches (oz) |
            warehouses (wb) | geography
        start: смещение постраничной выдачи
        limit: сколько строк вернуть
        sort_by: колонка сортировки
        sort_dir: asc | desc
        fbs: 1 — учитывать FBS
        filters: filterModel в формате MPSTATS
    """
    return _trim(await _entity_report(_market(marketplace), "seller", report,
                                      seller, d1, d2, start, limit, sort_by,
                                      sort_dir, filters, fbs))


@mcp.tool
async def mpstats_sku(
    sku: str,
    marketplace: str = "wb",
    report: str = "card",
    d1: str | None = None,
    d2: str | None = None,
    date: str | None = None,
) -> str:
    """Аналитика конкретного товара по его id на маркетплейсе.

    Args:
        sku: идентификатор товара (артикул WB / SKU Ozon / id ЯМ)
        marketplace: wb | oz | ym
        report: card (карточка) | full | sales (продажи и остатки) |
            by_period | balance | balance_sizes | balance_colors |
            sales_sizes | sales_heatmap | stores | search_stats | keywords |
            keywords_hourly | comments | comments_ai (oz) | by_day (oz) |
            categories (oz) | faq (wb) | photos_history (wb)
        d1: начало периода YYYY-MM-DD (для исторических отчётов)
        d2: конец периода YYYY-MM-DD
        date: конкретная дата среза (для balance / by_day)
    """
    market = _market(marketplace)
    params = {"d1": d1, "d2": d2, "date": date}
    if market == "ym":
        return _trim(await _call("GET", BASES["ym"],
                                 f"ym/get/item/{quote(str(sku))}/sales", params))
    table = SKU_REPORTS_WB if market == "wb" else SKU_REPORTS_OZ
    if report not in table:
        raise ValueError(
            f"Отчёт {report} недоступен для {market}. Доступно: {sorted(table)}"
        )
    suffix = table[report]
    api_path = f"items/{quote(str(sku))}" + (f"/{suffix}" if suffix else "")
    return _trim(await _call("GET", BASES[market], api_path, params))


@mcp.tool
async def mpstats_subject(
    subject: str,
    d1: str,
    d2: str,
    report: str = "items",
    start: int = 0,
    limit: int = 50,
    sort_by: str | None = "revenue",
    sort_dir: str = "desc",
    fbs: int | None = None,
) -> str:
    """Ниша (предмет) Wildberries — аналитика по предмету, а не по категории.

    Args:
        subject: id предмета WB (числовой) или его название
        d1: начало периода YYYY-MM-DD
        d2: конец периода YYYY-MM-DD
        report: items | categories | brands | sellers | trends | by_date |
            keywords | geography | similar | warehouses | price_segmentation
        start: смещение постраничной выдачи
        limit: сколько строк вернуть
        sort_by: колонка сортировки
        sort_dir: asc | desc
        fbs: 1 — учитывать FBS
    """
    params = {"path": subject, "d1": d1, "d2": d2}
    if fbs is not None:
        params["fbs"] = fbs
    body = _body(start, limit, sort_by, sort_dir, None)
    return _trim(await _call("POST", BASES["wb"], f"subject/{report}", params, body))


@mcp.tool
async def mpstats_similar(
    sku: str,
    report: str = "items",
    family: str = "similar",
    d1: str | None = None,
    d2: str | None = None,
    start: int = 0,
    limit: int = 50,
    sort_by: str | None = "revenue",
    sort_dir: str = "desc",
) -> str:
    """Похожие товары Wildberries и аналитика по этой выборке.

    Args:
        sku: артикул WB, вокруг которого строится выборка
        report: items | categories | brands | sellers | trends | by_date |
            keywords | warehouses | price_segmentation
        family: similar (похожие по WB) | identical (ИИ-аналоги MPSTATS) |
            identical_wb (аналоги WB) | in_similar (у кого товар в похожих)
        d1: начало периода YYYY-MM-DD
        d2: конец периода YYYY-MM-DD
        start: смещение постраничной выдачи
        limit: сколько строк вернуть
        sort_by: колонка сортировки
        sort_dir: asc | desc
    """
    if family not in {"similar", "identical", "identical_wb", "in_similar"}:
        raise ValueError("family: similar | identical | identical_wb | in_similar")
    params = {"path": sku, "d1": d1, "d2": d2}
    body = _body(start, limit, sort_by, sort_dir, None)
    return _trim(await _call("POST", BASES["wb"], f"{family}/{report}", params, body))


@mcp.tool
async def mpstats_compare(
    value: str,
    d11: str,
    d12: str,
    d21: str,
    d22: str,
    marketplace: str = "wb",
    entity: str = "category",
    limit: int = 50,
    fbs: int | None = None,
) -> str:
    """Сравнение двух периодов по категории, бренду, продавцу или нише.

    Args:
        value: путь категории / бренд / id продавца / id предмета
        d11: начало первого периода YYYY-MM-DD
        d12: конец первого периода YYYY-MM-DD
        d21: начало второго периода YYYY-MM-DD
        d22: конец второго периода YYYY-MM-DD
        marketplace: wb | oz | ym
        entity: category | brand | seller | subject (только wb)
        limit: сколько строк вернуть
        fbs: 1 — учитывать FBS
    """
    market = _market(marketplace)
    params = {"path": value, "d11": d11, "d12": d12, "d21": d21, "d22": d22}
    if fbs is not None:
        params["fbs"] = fbs
    body = _body(0, limit, None, "desc", None)
    if market == "ym":
        return _trim(await _call("POST", BASES["ym"],
                                 f"ym/get/{entity}/compare", params, body))
    return _trim(await _call("POST", BASES[market], f"{entity}/compare", params, body))


@mcp.tool
async def mpstats_forecast(
    path: str,
    report: str = "forecast/daily",
    entity: str = "category",
    period: str | None = None,
) -> str:
    """ИИ-прогнозы и сезонность Wildberries по категории или нише.

    Args:
        path: путь категории или id предмета
        report: forecast/daily | forecast/trend | season_effects/annual |
            season_effects/weekly
        entity: category | subject
        period: период для прогноза (опционально)
    """
    params = {"path": path, "period": period}
    return _trim(await _call("GET", BASES["wb"], f"{entity}/{report}", params))


@mcp.tool
async def mpstats_request(
    path: str,
    method: str = "GET",
    marketplace: str = "wb",
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> str:
    """Сырой вызов любого метода MPSTATS API, если готового инструмента нет.

    Args:
        path: путь относительно базы маркетплейса, например category/niches
            или items/123456/keywords. Путь, начинающийся с api/ или с http,
            используется как есть.
        method: GET | POST
        marketplace: wb | oz | ym — какую базу использовать
        params: query-параметры (d1, d2, path, fbs и т.д.)
        body: тело POST-запроса; по умолчанию стандартная пагинация
    """
    market = _market(marketplace)
    base = BASES[market]
    if path.startswith("api/"):
        base, path = API_ROOT, path[4:]
    payload = body
    if method.upper() == "POST" and payload is None:
        payload = {"startRow": 0, "endRow": 50, "filterModel": {},
                   "sortModel": [{"colId": "revenue", "sort": "desc"}]}
    return _trim(await _call(method, base, path, params, payload))


# --------------------------------------------------------------------------- #
# Служебные роуты
# --------------------------------------------------------------------------- #
@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "token_configured": bool(TOKEN)})


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=PORT, path=MCP_PATH)
