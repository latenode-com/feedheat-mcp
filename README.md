# Taskospot — MCP-сервер администратора

MCP-сервер поверх админского HTTP API Taskospot. Позволяет из Claude Desktop /
Claude Code (или любого MCP-клиента) смотреть клиентов, проекты и исполнителей —
и **создавать реальные оплачиваемые заказы** от имени настоящего админского аккаунта.

Транспорт — stdio. Ходит по HTTP на `https://taskospot.com` (или на локальный
`http://localhost:8100`), авторизуется отзываемым API-ключом `fhk_...`.

> Всё, что делает сервер, попадает в аудит-лог платформы под тем админом, чьим ключом
> он ходит. Это не «бот» — это ваш аккаунт.

## Сетевой режим (основной)

Ставить ничего не нужно: сервер работает на Taskospot, подключение — адрес и ключ.

```bash
claude mcp add --transport http taskospot https://taskospot.com/mcp/admin --header "Authorization: Bearer fhk_..."
```

или в конфиге MCP-клиента:

```json
{"mcpServers": {"taskospot": {"type": "http", "url": "https://taskospot.com/mcp/admin",
  "headers": {"Authorization": "Bearer fhk_..."}}}}
```

Заказчику — тот же формат на `https://taskospot.com/mcp`. Один процесс обслуживает
всех: ключ берётся из заголовка каждого запроса (`feedheat_mcp.http`,
`feedheat-mcp-http`). Локальная установка ниже остаётся для разработки и для
клиентов, которые не умеют HTTP-транспорт.

## Установка

Ставить руками не нужно — MCP-клиент скачает и запустит пакет сам. Нужен только
[uv](https://docs.astral.sh/uv/) и Python 3.13.

Проверить, что запускается:

```bash
FEEDHEAT_API_KEY=fhk_ваш_ключ uvx --from git+https://github.com/latenode-com/feedheat-mcp feedheat-mcp
```

Команда молчит и ждёт ввода — так и должно быть: сервер говорит по stdio с клиентом, а не с человеком. Ctrl-C.

## Ключ

Ключ выпускается под конкретного админа — в панели: **Настройки → MCP**
(`/panel/mcp`). Там же лежат готовый конфиг и промт, которым агент настраивает себя сам.

Кому ближе консоль — `POST /api/admin/api-keys` по живой сессии. Сама выдача ключей
ключам запрещена: иначе отзыв ничего не решает, скомпрометированный ключ выпишет себе
новый.

1. Ключ показывается **один раз** при создании — сохраните его в менеджер паролей.
2. Ключ отзываемый: `DELETE /api/admin/api-keys/{id}`; после отзыва сервер сразу
   начинает получать 401.
3. Ключ умирает вместе с владельцем: если админа заблокировали или разжаловали,
   ключ перестаёт работать.
4. Скоупы сервера:
   * `orders:read` — клиенты, проекты, исполнители, заказы;
   * `orders:write` — создание, назначение, публикация, отмена заказов;
   * `emails:read` / `emails:write` — email-кампании (CRUD, превью аудитории, send/schedule).
   Скоуп `users:read` живёт на `/api/admin/users` — сервер туда не ходит, **не выдавайте
   его**. Без `orders:write` / `emails:write` соответствующие инструменты записи отвечают
   403.

Ключ передаётся серверу **только через переменные окружения**, в аргументах командной
строки его писать нельзя (видно в `ps`).

| Переменная          | По умолчанию                | Смысл                                  |
|---------------------|-----------------------------|----------------------------------------|
| `FEEDHEAT_API_KEY`  | — (обязательна)             | Ключ `fhk_...`                         |
| `FEEDHEAT_API_URL`  | `https://taskospot.com` | Корень API; локально `http://localhost:8100` |

## Подключение к Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "taskospot": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/latenode-com/feedheat-mcp", "feedheat-mcp"],
      "env": {
        "FEEDHEAT_API_URL": "https://taskospot.com",
        "FEEDHEAT_API_KEY": "fhk_ваш_ключ"
      }
    }
  }
}
```

`uvx` каждый запуск сверяется с гитом; чтобы этого не было, поставьте пакет разово и
пропишите путь до исполняемого файла:

```bash
uv tool install git+https://github.com/latenode-com/feedheat-mcp
```

```json
{
  "mcpServers": {
    "taskospot": {
      "command": "feedheat-mcp",
      "env": { "FEEDHEAT_API_KEY": "fhk_ваш_ключ" }
    }
  }
}
```

Обновляется такой вариант руками: `uv tool upgrade feedheat-mcp`.

## Подключение к Claude Code

```bash
claude mcp add taskospot \
  -e FEEDHEAT_API_KEY=fhk_ваш_ключ \
  -e FEEDHEAT_API_URL=https://taskospot.com \
  -- uvx --from git+https://github.com/latenode-com/feedheat-mcp feedheat-mcp
```

## Инструменты

Чтение (безопасно вызывать сколько угодно):

| Инструмент | Что делает |
|---|---|
| `list_clients(include_projects=True)` | Клиенты с проектами и количеством заказов по статусам |
| `list_projects(customer_id)` | Проекты клиента с полным брифом (тон, сабреддиты, ключи, стоп-темы) |
| `list_executors(include_inactive=False)` | Активные исполнители: загрузка, апрувы, заработок |
| `list_orders(status?, customer_id?, limit=50)` | Заказы короткими карточками |
| `get_order(order_id)` | Один заказ целиком: тексты, награда, пруфы, метрики |
| `order_candidates(order_id)` | Кто может взять заказ: закреплённые, если заказ закреплён, иначе все, кто видит его в пуле по скоупам |

Запись (тратит деньги / меняет состояние):

| Инструмент | Что делает |
|---|---|
| `create_order(...)` | Создаёт один заказ. Двухшагово: без `confirm=true` возвращает превью и не создаёт ничего |
| `create_orders_batch(...)` | Масс-заказ по одному на исполнителя (`mass_comment` / `mass_post`), тоже с `confirm` |
| `assign_order(order_id, executor_ids)` | Переназначает заказ (список заменяет назначение целиком) |
| `open_order(order_id)` | Публикует админский черновик: `draft → open` |
| `cancel_order(order_id)` | Отменяет заказ (undo для `create_order`), тоже с `confirm` |

Email-кампании (нужны скоупы `emails:read` / `emails:write`):

| Инструмент | Что делает |
|---|---|
| `list_email_campaigns` / `get_email_campaign` | Список и карточка кампании |
| `create_email_campaign` / `update_email_campaign` | Черновик и частичные правки (HTML, subject, cohort, trigger, trigger_config) |
| `preview_email_campaign_audience` | Count + sample по когорте |
| `preview_email_campaign_html` | Dry-run рендер плейсхолдеров |
| `schedule_email_campaign` / `send_email_campaign` | Расписание / arm события / отправка, двухшагово с `confirm` |
| `pause_email_campaign` / `cancel_email_campaign` | Пауза / отмена |

Когорта — JSON-фильтр (`role`, `status`, `notify_new_tasks`, окна `created_*` /
`last_claim_*`, `never_claimed`, `project_id`, `user_ids`, `emails`, …).

Триггеры:

| trigger | Когда | trigger_config |
|---|---|---|
| `manual` | только `send_email_campaign` | пусто |
| `schedule` | `schedule_at` и/или 5-полевой `cron` | пусто |
| `new_task_available` | заказ стал `open` | опц. `min_reward_cents`, `project_ids`, `order_types` |
| `executor_idle` | сканер в том же crontab | **обяз.** `days_idle`; опц. `never_claimed_counts` |
| `order_status` | переход заказа | **обяз.** `to`, `audience` (`customer\|executor\|both`) |
| `assignment_claimed` | клейм | опц. `roles` |
| `assignment_released` | сняли клейм | опц. `reasons`: `released\|expired\|cancelled` |
| `user_registered` / `email_verified` / `user_approved` | регистрация / подтверждение / апрув | опц. `roles` |

Event-кампании после `schedule_email_campaign(confirm=true)` остаются `scheduled`
(armed) и ловят новых подходящих людей; Delivery `(campaign, user)` — не чаще
раза на человека. Раннер `manage.py run_email_campaigns` каждую минуту шлёт due
`schedule` и сканирует `executor_idle`.

### Деньги — в центах

`reward_cents=500` — это **$5.00**. Это общий бюджет заказа, а не ставка за действие:
у `mass_*` с `quantity=N` исполнитель получает `reward_cents × (принятых пруфов / N)`.

Защиты от ошибки на два порядка:

* превью перед созданием показывает и центы, и доллары, и итог по batch (N × ставка);
* `reward_cents > 1 000 000` ($10 000) отклоняется до сети;
* `reward_cents ≥ 20 000` ($200) помечается предупреждением «возможно, вы имели в виду доллары»;
* `reward_cents=0` помечается предупреждением «исполнитель увидит неоплачиваемое задание».

### Двухшаговое подтверждение

`create_order`, `create_orders_batch`, `cancel_order`, `schedule_email_campaign` и
`send_email_campaign` с `confirm=false` (по умолчанию) возвращают
`status: CONFIRMATION_REQUIRED` и ничего не меняют. Модель обязана показать превью
человеку и повторить вызов с `confirm=true`.

* создание с `confirm=false` **не делает HTTP-запрос вообще** — только payload и стоимость;
* отмена с `confirm=false` делает **один GET** заказа, чтобы показать, что именно исчезнет
  (тип, статус, награда в долларах, клиент, кому назначен), и не трогает ничего больше.

### Отмена — это не «удалить черновик»

`cancel_order` отменяет заказ в статусах `draft`, `pending`, `open`, `needs_revision`,
`claimed`. На `claimed` активная работа исполнителя становится `abandoned` — **человек
теряет уже начатую работу**, превью предупреждает об этом отдельной строкой.
`submitted` (пруф ждёт ревью), `retention` и `completed` отменить нельзя — бэкенд ответит
409 с текущим статусом. Уже начисленные в леджер деньги отмена не возвращает.

## Чего ключ (и этот сервер) НЕ может

Сознательное ограничение, обходить его нечем — на эти ручки API-ключ получает 403:

* **войти под чужим аккаунтом** (impersonate) — только живой админ в панели;
* **менять статус пользователя** (активация / блокировка);
* **обрабатывать выплаты** — approve/reject выводов средств.

Дополнительно этот сервер не умеет (и не будет уметь без отдельного решения):

* удалять заказы насовсем (`DELETE /orders/{id}`) — отмена есть, жёсткого удаления нет;
* модерировать сдачу работы (approve/reject пруфов) и заказы заказчиков;
* редактировать существующие заказы, создавать клиентов и проекты;
* менять бюджет уже созданного заказа;
* переписываться с исполнителями в чате заказа.

## Поведение под ошибками

* `401` → ключ отозван, истёк или в `FEEDHEAT_API_KEY` мусор. Сервер прямо говорит, что
  чинится это только выпуском нового ключа, и что повторять запрос бессмысленно.
* `403` → не хватает скоупа **или** действие закрыто для ключей навсегда (см. выше).
* Таймаут — 12 секунд.
* Ретраи — **только на GET** (три попытки, на `429/502/503/504` и сетевых сбоях).
  На создании заказа ретраев нет никогда: повтор после таймаута — это второй заказ и
  вторая выплата. При таймауте на записи сервер прямо просит сначала проверить
  `list_orders`, а не повторять вслепую.
* Ключ вычищается из любого текста, который уходит наружу (включая эхо от бэкенда и
  тексты сетевых исключений), и не печатается в `repr` клиента.

## Разработка

```bash
git clone https://github.com/latenode-com/feedheat-mcp && cd feedheat-mcp
uv sync
```

```
feedheat-mcp/
├── feedheat_mcp/
│   ├── __init__.py
│   ├── __main__.py      # python -m feedheat_mcp
│   ├── client.py        # HTTP: заголовки, ретраи, перевод ошибок
│   └── server.py        # инструменты MCP, валидация, подтверждения
├── tests/               # pytest, HTTP замокан (httpx.MockTransport)
└── pyproject.toml
```

```bash
uv run pytest -q     # тесты, HTTP замокан — бэкенд не нужен
uv run ruff check .  # линт
```

Локально против дев-бэкенда: `FEEDHEAT_API_URL=http://localhost:8100`.

## Зеркало

Код живёт в каталоге `mcp-server` репозитория платформы, а сюда зеркалится — здесь он
открыт только затем, чтобы ставиться одной командой. Правки принимаются в платформе;
пул-реквесты в зеркало уедут при следующей синхронизации:

```bash
git subtree push --prefix=mcp-server git@github.com-latenode:latenode-com/feedheat-mcp.git main
```

Синхронизация — обязательный шаг любой правки сервера, а не уборка на потом.
Ставится пакет ОТСЮДА, поэтому до публикации люди получают предыдущую версию:
изменённый инструмент ведёт себя по-старому, а новый entry point не находится
вовсе. В CI шага нет — зеркало в другом аккаунте.

Проверка, что опубликованное действительно ставится:

```bash
FEEDHEAT_API_KEY=fhk_fake uvx --from "git+https://github.com/latenode-com/feedheat-mcp" feedheat-client-mcp </dev/null
```

Код 0 — пакет собрался и entry point на месте (пустой stdin закрывает сервер
сразу). Ошибка про неизвестную команду — зеркало отстало.

## Лицензия

Внутренний инструмент Taskospot. Репозиторий открыт, чтобы сервер можно было поставить
одной командой, — прав на переиспользование это не даёт.
