# Аудит и проверка нового функционала деривативов

Дата: 2026-11-10 (UTC). Работа выполнена в текущем checkout `Ghostofnotwar/pumpdump_monitor` на сессионной ветке `arena/3e962891-pumpdump-monitor`.

## Выбор проекта и исходный аудит

Выбран уже находящийся в рабочем checkout репозиторий [Ghostofnotwar/pumpdump_monitor](https://github.com/Ghostofnotwar/pumpdump_monitor), а не отдельный архив из списка кандидатов: это существующий Python/Telegram-монитор со своим rolling detector, SQLite, Bybit REST-клиентом, управляемой очередью и отправкой PNG через `send_photo`. Значит, новый скриншот можно подключить к имеющемуся alert delivery без второго бота или смены основного источника цен. Git remote указывает на этот репозиторий. Отдельный clone/форк стороннего проекта не делался.

В проекте уже есть `AUDIT.md` по исправлениям исходного прототипа: в нём разобраны проверка Telegram WebApp-подписи, настройки и SQL allowlist, управление задачами, ошибки Bybit, SQLite, фильтры, управление PNG и отсутствие реальных OI/ликвидаций в исходных графиках. Эта итерация дополняет его данными деривативов и Coinglass. В checkout нет файла LICENSE; существующие сведения о лицензии не менялись.

## Реализация

- `market_indicators.py` нормализует источники Binance USD-M public REST без Binance API-key:
  - `aggTrades` — signed base-quantity CVD (`m=true` трактуется как taker sell, `m=false` как taker buy); выборка ограничена 1000 агрегатами, CVD явно помечен как partial window;
  - `openInterestHist` — ряды OI;
  - `allForceOrders` — реальные принудительно закрываемые ордера; SELL группируется как ликвидации long, BUY — short; notional считается как quantity × price.
- Приведение временных точек к свечам выполняется в `chart_builder.py`. Доступные панели имеют отдельные подписи и оси; данные, которых нет, не синтезируются. Если CVD недоступен, сохраняется уже существовавшая OHLCV-оценка с явной пометкой `proxy`.
- Опциональный Coinglass v4 источник запрашивает `/api/futures/liquidation/aggregated-heatmap/model1`, передаёт ключ в `CG-API-KEY`, кэширует ответ 5 минут и дедуплицирует одновременные запросы. Парсер валидирует разреженные `[candle_index, price_index, intensity]`, индексы и конечность значений; оси сортируются. Коэффициент цвета подписан `Model intensity (not USD)`, денежный знак к модельной интенсивности не добавляется.
- `build_liquidation_heatmap()` формирует самостоятельный тёмный PNG с ценой по Y, временем по X, ценовой линией и тепловой картой inferno.
- В настройках Telegram появились включатели CVD, OI, ликвидаций и отдельного heatmap PNG. Heatmap по умолчанию выключен. Новый DB-флаг мигрирует аддитивно.
- При отсутствии ключа, API entitlement, пустом ответе или сбое heatmap пропускается: текстовый/основной скриншотный алерт остаётся независимым. Coinglass-ошибка не валит worker.

## Проверки

В изолированном Python 3.11 virtualenv установлены зависимости из `requirements-dev.txt`. Результат локального прогона:

```text
python -m pytest -q --timeout=30 --timeout-method=thread
104 passed
ruff check .
All checks passed!
```

В проверках присутствуют:

- парсинг CVD, OI, long/short forced orders, sparse Coinglass data, неверных индексов и переставленных осей;
- рендер PNG с тремя панелями, отдельной тестовой heatmap и закрытием Matplotlib figures;
- отсутствие Coinglass-ключа, отклонённый/недоступный API, пустые данные и graceful degradation;
- кэш и объединение параллельных запросов;
- Telegram-доставка отдельного heatmap фото и сохранение основного алерта при сбое heatmap;
- paper-симуляция спокойного рынка (движение 0.1% не создаёт сигнал), существующие проверки детектора PUMP/DUMP, очереди, cooldown и остановки.

**Ограничение независимого теста:** sandbox имеет ограниченный egress, поэтому live-запросы к Binance/Coinglass и production-тариф Coinglass не проверялись, а непрерывный 1–2-дневный тест на реальном рынке не заявляется. Проверен детерминированный paper fixture и обработка отключённых/ошибочных API. Для production нужно проверить Binance-эндпоинты из региона деплоя и доступность Coinglass у аккаунта Professional/Enterprise. Без ключа Coinglass в тестовом окружении heatmap намеренно не генерируется.

## Запуск

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
cp .env.example .env
# Указать BOT_TOKEN; по желанию добавить COINGLASS_API_KEY
python main.py
```

CVD/OI/liquidations и отдельная heatmap настраиваются в Telegram → **Панели графика**. Для Coinglass нужен платный доступ и `COINGLASS_API_KEY`. Подробности источников и оговорки по частоте/покрытию описаны в `README.md`.
