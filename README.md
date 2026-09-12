# mpstats-mcp

MCP-сервер поверх [MPSTATS API](https://mpstats.io/integrations) — аналитика
Wildberries, Ozon и Яндекс Маркета прямо в Claude. Подключается к claude.ai
как кастомный коннектор (Streamable HTTP): работает во всех чатах, включая
мобильное приложение.

## Инструменты

| Инструмент | Что делает |
|---|---|
| `mpstats_api_limit` | Остаток лимита запросов по тарифу |
| `mpstats_categories` | Дерево категорий с поиском по подстроке — отсюда берутся точные пути |
| `mpstats_category` | Товары, подкатегории, бренды, продавцы, тренды, динамика, ценовая сегментация, запросы |
| `mpstats_brand` | То же в разрезе бренда |
| `mpstats_seller` | То же в разрезе продавца |
| `mpstats_sku` | Карточка, продажи, остатки по складам и размерам, позиции, запросы, отзывы |
| `mpstats_subject` | Ниша (предмет) Wildberries |
| `mpstats_similar` | Похожие товары WB и аналитика по выборке |
| `mpstats_compare` | Сравнение двух периодов |
| `mpstats_forecast` | ИИ-прогнозы и сезонность WB |
| `mpstats_request` | Сырой вызов любого метода API |

Дерево категорий кэшируется на 6 часов, ответы обрезаются по `MAX_RESPONSE_CHARS`,
размер страницы в списочных отчётах ограничен 500 строками — это лимит API.

## Переменные окружения

| Переменная | Обязательна | Описание |
|---|---|---|
| `MPSTATS_TOKEN` | да | Токен из личного кабинета: https://mpstats.io/userpanel → блок «API token» |
| `MCP_PATH` | нет | Путь MCP-эндпоинта. Секретный путь работает как пароль: `/s/<случайная строка>/mcp` |
| `PORT` | нет | Порт внутри контейнера, по умолчанию 8000 |
| `MAX_RESPONSE_CHARS` | нет | Ограничение размера ответа, по умолчанию 60000 |

Значения заданы в Dokploy (вкладка Environment) и в репозитории не хранятся.

## Запуск локально

```bash
pip install "fastmcp>=2.12,<3" "httpx>=0.27" "uvicorn>=0.30"
export MPSTATS_TOKEN=...
export MCP_PATH=/s/локальный-секрет/mcp
python server.py
# проверка: curl http://localhost:8000/health
```

## Деплой

Dokploy, проект `ozon`, приложение `mpstats`:

- Provider: **Git** → `https://github.com/snsudak/mpstats-mcp.git`, ветка `main`
- Build Type: **Dockerfile**
- Domains: хост на порт контейнера `8000`, HTTPS + Let's Encrypt
- После пуша в `main` — нажать **Deploy** в Dokploy (или повесить вебхук из вкладки Deployments на репозиторий, тогда деплой пойдёт автоматически)

Проверка после деплоя: `GET /health` → `{"status":"ok","token_configured":true}`

## Подключение к Claude

Настройки → Connectors → Add custom connector → URL вида
`https://<хост>/s/<секрет>/mcp`.

Эндпоинт защищён только секретом в пути — этот URL не публиковать.

## Лицензия

MIT
