# Анализ проекта ABT

Проект имеет сильный концепт и хороший потенциал: “dbt для агентов” понятен, полезен и хорошо ложится на ценности удобства, структуры и простоты для разработчика. Главная ценность ABT не в “ещё одном агент-фреймворке”, а в дисциплине: файлы как юниты, `ref/source/config`, манифест, селекторы, тесты, трассировка, воспроизводимость. Это действительно может быть удобно для разработчиков, которым нужен не чат-бот, а поддерживаемый агентный проект.

Но сейчас есть важный разрыв между обещанной моделью и фактическим рантаймом. Самый критичный пример: README обещает явное `SELECT`-прохождение контекста как защиту от context bloat (`README.md:103`), но рантайм фактически передаёт весь output referenced node, не исполняя `SELECT`/`WHERE` из CTE (`abt/runtime/node_runner.py:235`). Это бьёт прямо в ключевую идею проекта.

## Оценка

### Концепт/value: 8/10

Идея сильная, особенно для dbt-minded разработчиков и команд, которым нужны декларативные agent workflows, манифест, тесты и наблюдаемость.

### Юзабилити: 6/10

CLI и структура понятные, но DX пока шероховатый: ошибки компиляции недостаточно обучающие, `compile --select` объявлен, но не используется (`abt/cli.py:40`), часть README обещает больше, чем реализовано, а DSL может ломаться на неочевидных regex-ограничениях.

### Техническая реализация: 6/10

Архитектура модульная и читаемая, но несколько core-механик слишком хрупкие: regex-парсер `.prompt` (`abt/compiler/cte_parser.py:53`), псевдо-SQL без реального исполнения, примитивный tool parameter extraction (`abt/runtime/node_runner.py:214`), `require_any` не “first success wins”, а запускает все ветки (`abt/runtime/executor.py:331`).

## Главные проблемы

1. `SELECT`/`WHERE` сейчас не является реальным контрактом контекста. Разработчик думает, что выбрал поля, но LLM получает весь output ref-ноды.

2. TOOL CTE поддерживает только первый `source()` и один простой `WHERE key = 'value'`; несколько параметров из примера уже не обрабатываются корректно.

3. Output mapping ломает валидные falsy-значения: `0`, `False`, `[]`, `""` заменяются на placeholder из-за `or` (`abt/runtime/node_runner.py:95`).

4. `provider` в конфиге есть, но рантайм жёстко завязан на OpenAI-compatible DeepSeek env (`abt/runtime/node_runner.py:39`).

5. README заявляет `graphql`, но `ToolTable` уводит неизвестные типы в stub (`abt/runtime/tool_table.py:24`).

6. Leaf-name resolution неоднозначен: если два файла называются `decide.prompt` в разных папках, `ref('decide')` выберет первый найденный (`abt/compiler/graph_builder.py:122`).

7. Тесты и approval expressions используют `eval`; builtins ограничены, но для долгосрочного DX лучше единый безопасный expression evaluator/AST validator (`abt/runtime/test_runner.py:87`).

## План улучшений

### P0: привести core-семантику DSL к правде

1. Реализовать настоящий context projection для `SELECT ... FROM {{ ref() }} WHERE ...`.

Почему: это центральное обещание ABT. Без этого проект теряет главное отличие от “просто склеить промпты”.

Практично: начать с ограниченного AST: `SELECT field[, field] FROM ref WHERE simple_expr`; не надо строить полный SQL.

2. Починить output extraction.

Почему: `False`, `0`, пустой список сейчас могут превращаться в `"[field]"`.

Практично: заменить `final.get(col) or ...` на явную проверку наличия ключа.

3. Сделать tool CTE структурным.

Почему: текущий regex не соответствует даже примеру с двумя условиями `sku` и `horizon_days`.

Практично: парсить source ref + `WHERE` в dict параметров, валидировать against `table.params/input_schema`, показывать понятные ошибки.

4. Синхронизировать README с реализацией.

Почему: для developer-first инструмента доверие к документации критично.

Практично: либо убрать `graphql` и “first success wins”, либо реализовать их.

### P1: улучшить developer experience

5. Добавить `abt debug/inspect`.

Что: показать compiled node, refs, projected context, tool params, output schema, route map.

Почему: разработчику нужно понимать, что реально пойдёт в LLM до запуска денег/токенов.

6. Сделать ошибки компиляции диагностическими.

Что: файл, строка, snippet, список доступных refs/sources/schemas, предложение qualified ref при ambiguity.

Почему: DSL должен помогать, а не заставлять читать код компилятора.

7. Убрать неоднозначность `ref()`.

Что: если leaf name матчится на несколько qualified nodes, падать с ошибкой и вариантами.

Почему: silent wrong dependency в agent workflow будет дорогим багом.

8. Поддержать `compile --select` или удалить флаг.

Почему: объявленные, но неработающие опции ухудшают доверие к CLI.

### P2: укрепить runtime и продуктовую полезность

9. Ввести provider abstraction.

Что: `provider=openai/deepseek/anthropic` должен реально выбирать client factory.

Почему: сейчас конфиг выглядит универсальным, но фактически таким не является.

10. Сделать `require_any` настоящим OR-gate или переименовать.

Почему: “first success wins” подразумевает экономию времени/токенов, а текущая реализация запускает все ветки.

11. Добавить золотые тесты DSL.

Что: fixtures `.prompt -> parsed AST -> execution context`.

Почему: проект держится на языке; тестировать нужно не только happy path интеграции, но и edge cases парсинга.

12. Добавить onboarding-пример без внешних API.

Что: полностью локальный `python_function` или fixture source.

Почему: новый разработчик должен получить зелёный `abt compile`, `abt run`, `abt test` без DeepSeek/API/MCP.

### P2+: расширить примитивы оркестрации

Текущие три примитива (SEQUENTIAL / REQUIRE_ALL / REQUIRE_ANY) + route_on покрывают ~70% агентных паттернов. Чтобы поднять до ~85%, нужно два расширения, которые не ломают DAG-модель и остаются в рамках ментальной модели "думай папками":

13. **Ordered fallback: `require_first/`**

Что: новый тип роутинга — "попробуй по порядку, первый успешный побеждает". В отличие от `require_any` (гонка всех параллельно), `require_first` запускает узлы последовательно. Узел считается проваленным если явно вернул `on_fail_route` или Pydantic-валидация не прошла. Первый успешный → результат идёт дальше. Все провалились → ошибка графа.

```
require_first__high_accuracy/
├── gpt4_analysis.prompt       # дорогой, точный
├── haiku_analysis.prompt      # дешёвый, быстрый
└── heuristic_fallback.prompt  # без LLM, всегда работает
```

Почему: это самый частый паттерн эскалации — "попробуй простое, не вышло → усложняй". Сейчас его можно эмулировать через `route_on` + `on_fail_route`, но неудобно: требует явных связей между каждым шагом каскада. `require_first` делает эскалацию first-class примитивом.

14. **Reusable subgraphs: blueprints**

Что: возможность определить подграф один раз и использовать его в нескольких местах проекта. Не через копипасту папок, а через ссылку.

```
blueprints/approval/
├── request_approval.prompt
└── handle_response.prompt

prompts/
├── new_order/
│   ├── _approval -> ../../blueprints/approval/   # ссылка, не копия
│   ├── validate_order.prompt
│   └── finalize.prompt
├── return_order/
│   ├── _approval -> ../../blueprints/approval/   # та же логика, другой контекст
│   ├── validate_return.prompt
│   └── finalize.prompt
```

Префикс `_` в имени папки означает "это не реальная папка, а ссылка на blueprint". Компилятор разрешает её в те же узлы, но с контекстом текущего места в графе. Один `_approval` может ссылаться на три узла, которые появляются в двух разных ветках графа — каждый экземпляр получает свои входы через `ref()`.

Почему: любой повторяющийся процесс (approval, валидация, обогащение) сейчас требует копипасты. Blueprints дают композицию без усложнения модели — разработчик продолжает думать папками, просто некоторые папки "живут" в одном месте, а "используются" во многих.

### Что сознательно НЕ добавлять (пока)

- **Циклы на уровне графа** — ломают dbt-DAG ментальную модель, привносят проблему остановки. Эмулируются внутри CTE loop.
- **Fan-out (map-reduce)** — дико полезно, но требует динамического создания узлов и сборщика результатов. Это v2.0.

## Верификация

Тесты не удалось запустить в текущем окружении, потому что не установлены зависимости (`click`, `pytest`), а `python` alias отсутствует. Анализ выполнен статически по коду и документации.

Команды, которые проверялись:

```bash
pytest -q
python -m pytest -q
python3 -m pytest -q
python3 -m abt.cli compile
python3 -m abt.cli --help
```

Наблюдения по окружению:

- `pytest`: command not found
- `python`: command not found
- `python3 -m pytest`: No module named pytest
- `python3 -m abt.cli --help`: ModuleNotFoundError: No module named 'click'

## Проверенные области проекта

Основные файлы и модули, которые были просмотрены:

- `README.md`
- `CLAUDE.md`
- `pyproject.toml`
- `example_project/abt_project.yml`
- `example_project/prompts/*.prompt`
- `example_project/prompts/*.test.yml`
- `example_project/schemas/inventory_schema.yml`
- `example_project/sources/apis.yml`
- `example_project/triggers/inventory.triggers.yml`
- `abt/cli.py`
- `abt/project.py`
- `abt/compiler/cte_parser.py`
- `abt/compiler/prompt_compiler.py`
- `abt/compiler/folder_parser.py`
- `abt/compiler/graph_builder.py`
- `abt/compiler/manifest_generator.py`
- `abt/compiler/cache_manager.py`
- `abt/compiler/selector.py`
- `abt/compiler/jinja_env.py`
- `abt/compiler/schema_parser.py`
- `abt/compiler/source_parser.py`
- `abt/compiler/factory.py`
- `abt/runtime/node_runner.py`
- `abt/runtime/executor.py`
- `abt/runtime/tool_table.py`
- `abt/runtime/db.py`
- `abt/runtime/test_runner.py`
- `abt/runtime/trigger_manager.py`
- `abt/runtime/server.py`
- `abt/models/config.py`
- `abt/models/prompt.py`
- `abt/models/node.py`
- `abt/models/graph.py`
- `tests/test_integration.py`
- `tests/test_selectors.py`
- `tests/test_phase5_smoke.py`

## Дополнительные технические замечания

- `ProjectPaths.triggers_paths` по умолчанию требует наличие `triggers/`, а пример `abt_project.yml` не указывает `triggers_paths`; если `triggers/` отсутствует, проект не загрузится (`abt/models/config.py:47`). Для optional triggers это может быть излишне строго.

- `_filter_graph_structure()` делает shallow copy и затем заменяет `root` на результат `_prune_subgraph()`. Если selection пустой, `root` может стать `None`, что потенциально ломает executor (`abt/cli.py:571`).

- `ToolTable.get_tools_for_node()` тихо пропускает отсутствующие tools (`abt/runtime/tool_table.py:141`). Для DX лучше падать на compile/build stage с понятной ошибкой.

- `CacheManager` реконструирует `ParsedPrompt` из manifest, но cached dependencies берутся из уже resolved dependencies. Это хорошо для стабильности, но важно следить, чтобы форма `raw_dependencies` не смешивала raw и resolved semantics.

- README показывает тестовый синтаксис `is not null`, но Python eval понимает `is not None`; в примере проекта используется `None`. Это нужно унифицировать в документации или добавить трансляцию `null -> None`.

- Макросы Jinja загружаются через `env.parse(...).globals`, что не исполняет template как набор экспортируемых macro-функций в привычном смысле. Если macros важны для DX, это место стоит отдельно проверить и покрыть тестом.

## Итог

ABT стоит развивать дальше, но следующий этап должен быть не про добавление новых фич, а про укрепление центральной модели:

1. `.prompt` должен компилироваться в понятный AST.
2. `SELECT` должен реально управлять контекстом.
3. Tool calls должны быть структурными и валидируемыми.
4. Ошибки должны быть обучающими.
5. Документация должна точно соответствовать runtime.

Если сделать эти вещи, проект станет заметно ближе к своему главному обещанию: удобный, структурный и простой для разработчика способ строить агентные workflows.
