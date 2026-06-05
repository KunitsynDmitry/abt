# Real-Life Case: Конкурентная разведка

## Контекст

Обсуждаем, чем ABT отличается от dbt в реальном кейсе.
dbt — конвейер данных (трансформирует таблицы).
ABT — конвейер решений (читает, рассуждает, классифицирует).

Выбрали проект: агент конкурентной разведки.
Каждое утро обходит новости про конкурентов, LLM читает и классифицирует,
собирает сводку и кидает в Slack.

dbt здесь бесполезен: нет структурированных данных, есть неструктурированная
новостная лента. Всё держится на суждениях LLM, а не на SQL.

---

## Шаг 1: Создание проекта

Создали директорию `market_intel/` со структурой:
```
market_intel/
├── abt_project.yml        # Конфиг проекта
├── sources/news.yml       # Источник: Python-функция для RSS
├── rss_fetcher.py         # Модуль: скачивает RSS, фильтрует по конкурентам
├── prompts/               # (пусто)
├── schemas/               # (пусто)
└── sources/               # (уже есть news.yml)
```

### abt_project.yml
- Название: market_intel
- Модель: deepseek-chat, temperature 0.3
- Переменные: список конкурентов (Яндекс, Ozon, Wildberries, Сбер)
- Выход: Telegram (пока только chat_id в vars)

### sources/news.yml
- Тип: python_function
- Модуль: rss_fetcher, функция: fetch_competitor_news
- Принимает competitors (list[str]) и max_articles (int)
- Возвращает {total_found, fetched_at, articles[]}

### rss_fetcher.py
- Использует feedparser для скачивания RSS
- Три ленты: Интерфакс, Коммерсантъ, РБК
- Фильтрует статьи по ключевым словам конкурентов
- Возвращает массив новостей с полями: title, summary, link, published, source, matched_competitor

### Почему это не dbt:
dbt не может скачать RSS и отфильтровать по смыслу. Это не данные в колонках — это неструктурированный текст из интернета. Первый же шаг требует Python-кода, а не SQL. ABT позволяет обернуть это в источник и идти дальше.

---

## Шаг 2: Схема выходных данных

Создали `schemas/briefing.yml` с двумя моделями:

### news_item — одна новость
- competitor: название компании
- title: заголовок
- category: тип события (enum из 7 значений: Запуск продукта, Наём/увольнения, Цены/тарифы, Инвестиции/сделки, Партнёрство, Регуляторика, Прочее)
- importance: Критично / Важно / Фон
- summary: суть в одном предложении
- why_matters: почему это важно для нас

### daily_briefing — вся сводка
- date, total_articles, relevant_count
- items: list[news_item] — массив классифицированных новостей
- top_story: главная новость дня
- telegram_message: готовая сводка в Markdown для отправки

### Почему это не dbt:
В dbt схема описывает колонки таблицы — это контракт базы данных. Здесь схема описывает **суждение LLM**. LLM должен сам решить, какая category у новости, какой importance, и почему это matters. Поле `telegram_message` — это вообще генеративный текст, а не агрегация данных. dbt не умеет проверять, что `importance ∈ {Критично, Важно, Фон}`. ABT — умеет, и если LLM ошибётся, Pydantic-валидация вернёт ошибку и LLM переделает.

---

## Шаг 3: Промпт (ядро агента)

Создали `prompts/daily_briefing.prompt`.

### Структура CTE (три шага внутри одного файла):

1. **fetch_news AS TOOL** — вызывает Python-функцию `news_fetcher.fetch_competitor_news`.
   TOOL кешируется (одинаковый запрос = кеш), детерминированный вызов.

2. **classify AS LLM** — LLM читает статьи и классифицирует каждую:
   - Какой конкурент, категория (enum), важность, саммари, почему важно.

3. **compose AS LLM** — LLM собирает финальную сводку:
   - Главная новость дня
   - Готовое Markdown-сообщение для Telegram (Критично / Важно / Фон)

### Ключевое:
- `{{ config(output_schema="daily_briefing") }}` — выход проходит Pydantic-валидацию
- `{{ var('competitors') }}` — список конкурентов из abt_project.yml
- `{{ source('news_fetcher', 'fetch_competitor_news') }}` — вызов Python-функции через ABT-источник
- LLM не просто читает, а **принимает решения**: что критично, что фон, что главная новость

### Почему это не dbt:
В dbt трансформация — это SQL: SELECT, JOIN, GROUP BY. Здесь трансформация — это **рассуждение**. LLM решает, что важно, а что нет. Никакой SQL не напишет «эта новость критична, потому что конкурент запускает продукт в нашем сегменте». ABT связывает TOOL (детерминированный сбор данных) → LLM (недетерминированное рассуждение) → Pydantic (жёсткая валидация структуры).

---

## Шаг 4: Отправка в Telegram и роутинг

### telegram_sender.py
Python-модуль для отправки сообщений через Telegram Bot API.
- Читает `TELEGRAM_BOT_TOKEN` из env
- Функция `send_message(chat_id, text, parse_mode)` → dict с ответом API

### sources/news.yml (обновлён)
Добавлен источник `telegram.send_message` (python_function).

### prompts/sequential/ — папка с порядком выполнения
```
sequential/
├── daily_briefing.prompt   # Шаг 1: RSS → анализ → сводка
└── send_briefing.prompt    # Шаг 2: взять telegram_message → отправить
```
Папка `sequential/` говорит ABT: выполняй файлы по алфавиту.
Это аналог dbt-графа: модель A, потом модель B.

### send_briefing.prompt
- `{{ ref('sequential/daily_briefing') }}` — читает вывод первого узла
- `compose AS LLM` — извлекает `telegram_message`
- `_send AS TOOL` — вызывает `telegram.send_message`, передавая текст через `$compose.telegram_message`

### Микро-фикс в node_runner.py
Добавлена поддержка `$cte_name.field` в WHERE-параметрах TOOL-вызовов.
Раньше TOOL принимал только литералы. Теперь может ссылаться на результат
предыдущего CTE-блока внутри того же промпта.

### Почему это не dbt:
В dbt модель отдаёт таблицу в базу данных. Здесь модель отдаёт сообщение в Telegram.

---

## Багфикс: в cli.py не хватало `@cli.command()` у `run`

У функции `run` отсутствовал декоратор `@cli.command()` и опция `--select`.
Добавлены.

## Шаг 6: Компиляция и запуск

### Компиляция — успешно
```
abt compile
→ Compiling project at market_intel...
  Project: market_intel v0.1.0
  Schemas: 2 models, Sources: 2, Triggers: 2
  Prompts: 2 files, Folders: 1 subgraphs
  Manifest: target/manifest.json
```

### Запуск — граф работает
```
abt run -v
→ [sequential/daily_briefing/classify]   ← LLM классифицирует
  [sequential/send_briefing/compose]     ← LLM готовит отправку
  Ошибка 401 — нужен реальный DeepSeek API ключ
```

Граф отработал оба узла по порядку: RSS → классификация → отправка.
Нужен настоящий DEEPSEEK_API_KEY для завершения.
Первый узел (daily_briefing) генерирует текст через LLM. Второй узел (send_briefing)
читает текст через `ref()` и отправляет через внешний API. В dbt `ref()` связывает
таблицы в одной базе данных. В ABT `ref()` связывает решения: один узел подумал,
второй — доставил результат адресату.

---

## Шаг 5: Триггеры

Создали `triggers/briefing.triggers.yml`.

### Два триггера:
1. **morning_briefing** (schedule) — каждый будний день в 9:00.
   Автоматический запуск сводки. Как cron в dbt Cloud.

2. **briefing_now** (message) — ручной запуск через сообщение.
   Пользователь пишет «сводка» — агент выполняет ту же цепочку.

### Итоговая структура проекта:
```
market_intel/
├── abt_project.yml
├── rss_fetcher.py              # Python: RSS → статьи
├── telegram_sender.py          # Python: сообщение в Telegram
├── sources/news.yml            # Два источника: RSS + Telegram
├── schemas/briefing.yml        # Схема: news_item + daily_briefing
├── triggers/briefing.triggers.yml
└── prompts/sequential/
    ├── daily_briefing.prompt   # RSS → LLM-анализ → сводка
    └── send_briefing.prompt    # ref(сводка) → Telegram
```

### Чтобы запустить:
```bash
pip install feedparser
export DEEPSEEK_API_KEY=sk-...
export TELEGRAM_BOT_TOKEN=123:abc
# В abt_project.yml → vars.telegram_chat_id: "-123456"
cd market_intel
abt compile
abt run -v
abt serve              # Запуск крона + вебхуков
```

### dbt vs ABT — итоговая разница на этом проекте:

| | dbt | ABT (market_intel) |
|---|---|---|
| Источник данных | Таблица в БД | RSS-ленты из интернета |
| Трансформация | SQL (SELECT, JOIN) | LLM (прочитай, классифицируй, оцени важность) |
| Зависимости | ref() между моделями | ref() между промптами |
| Валидация | Тесты (unique, not_null) | Pydantic (enum, required) + LLM сам исправляет |
| Выход | Таблица в БД | Сообщение в Telegram |
| Запуск | dbt run / dbt cloud | abt run / abt serve (крон) |

Ключевое: dbt оперирует данными в колонках. ABT оперирует суждениями в тексте.
dbt знает, что в колонке `price` число. ABT знает, что новость про конкурента —
это «Запуск продукта» с важностью «Критично». Первое считает, второе рассуждает.

