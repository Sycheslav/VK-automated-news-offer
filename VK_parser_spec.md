# VK Parser Specification (project `C:\PROGA\Social_Graph\parser_VK`)

## Назначение
Парсер обрабатывает задания на построение графов VK (друзья, подписчики, сообщества) в два этапа, выгружает сырье в S3 и отправляет обновления статусов/результаты в бекенд через Redis Streams. Особенности: работа от пользовательских токенов с fallback на пул глобальных, продвинутое управление rate limit, маскировка под обычного пользователя и идемпотентная обработка сообщений.

## Архитектура и потоки данных
- **Вход**: Redis Stream `stream:parser:vk` с событиями `ReportCreatedEvent` (stage 1) и `Stage2CreatedEvent` (stage 2). Слушатели `report_created_listener` и `stage2_listener` запускаются через `app/main.py` → `consume()`.
- **Бизнес-обработчик**: `VKParser` (наследник `BaseParser`) управляет стадиями `parse_stage1`/`parse_stage2`, валидацией и загрузкой данных.
- **Статусы**: обновления шлются в Redis Stream `stream:report:status-change` методом `send_status_update`. При успешном stage 2 отправляется событие в `stream:graph-worker`.
- **Хранилище**: `YandexS3` сохраняет parquet/JSON дампы (raw tuples). Для stage1/2 используется префикс `reportType/mainIds/jobId_stage`.
- **Идемпотентность**: `IdempotencyManager` отмечает обработанные message_id, чтобы не дублировать работу.

## Токены и сессии VK
- **UserToken (предпочтительно)**: грузится по user_id через сервис токенов (`token-manager`), поддерживает proxy (`proxy_url`, `proxy_api_key`). При истечении срока `access_expires_at` триггерится refresh.
- **Global tokens (fallback)**: арендуются у `token-manager` (`lease_global_token`) с возможным прокси; по завершении/ошибке обязательно `release_global_token` с кодом причины.
- **Orchestrator (`VkTokenOrchestrator`)**:
  - Первая ошибка авторизации/бан на первом запросе → `JobFatalException` с `VK_RECONNECT_REQUIRED` (джоб завершается).
  - Повторные auth/ban или rate-limit → переключение на глобальные токены (до `max_global_retries`, экспоненциальный backoff).
  - Категоризация ошибок по VK-кодам: [5] auth, [6]/[9]/[29] rate limit, [15]/[30] access denied, [18] user not found, IP mismatch — отдельная ветка.
  - Сессии создаются с proxy, если оно задано; `cleanup` гарантирует релиз глобального токена.

## Антидетект, rate limit и ретраи
- Глобальный декоратор `vk_api_decorator` оборачивает все вызовы VK API:
  - RPS guard: минимум 0.42s между запросами (RateLimits.global_rps≈2.5).
  - Адаптивные паузы + случайные задержки; «разогрев» первых батчей увеличивает паузы.
  - Повторные попытки для кода [29] (Too many requests) и [9] с конфигурируемыми паузами; при [29] возможна смена токена.
  - Детальное логирование, извлечение кодов ошибок, защита от ошибок прокси (пересоздание сессии).
- Fake трафик: `ExecuteBuilder` и `FakeRequestManager` периодически добавляют «фейковые» вызовы (например, `utils.getServerTime`, `users.get`) в execute/отдельные запросы для маскировки.
- «Умная» ротация UserToken в `Friends._smart_token_rotation`: смена токена каждые ~30–40 запросов или при пачке ошибок/пустых ответов.

## Использование VK API
- **Основные методы**:
  - `friends.get` — получение друзей; вызывается массово через VKScript `execute` с fork/wait (батчи ~17 ID), в том числе маскируемые ID.
  - `users.getFollowers` — подписчики (батчи ~20 ID).
  - `users.get` — профили (поля `first_name,last_name,is_closed,deactivated,counters`); применяется и для резолва screen name → id.
  - `groups.getById` и смежные — для информации о сообществах при subs_graph.
- **VKScript execute**:
  - Генерация кода для friends/followers с параллельными fork/wait; опциональная вставка фейковых запросов.
  - Валидируется полнота ответа (`validate_execute_response`), есть fallback последовательные версии.
- **Прокси**: если токен содержит proxy, запросы к `api.vk.com/id.vk.ru/oauth.vk.com` переписываются на прокси с заголовками `X-Target-Host`, `X-Proxy-Key`.
- **Кэширование**: разрешение screen name → id кэшируется в `_screen_name_cache` на уровне класса `Friends`.

## Логика Stage 1
- Инициализация UserToken (`_init_token`), резолв входных `socialNetworkIds` в числовые VK ID (с fallback на `userId` автора).
- По типам графов:
  - **DEFAULT**: `get_main_friends` (друзья/подписчики основных ID), затем `loading_cycle` профилей (fast_graph/subs_graph флаги), маркировка `is_main_id`, добавление relation-колонок, возврат `(df_main_friends, df_users, subscriptions_df, valid_users)`.
  - **HANDSHAKES**: только списки друзей/подписчиков и валидация `(df_main_friends, valid_users)`.
  - **SUB_GRAPH**: `get_main_friends`, затем профили + «сырые» подписки `(df_main_friends, df_users, df_subs_raw)`.
- Валидация stage1 гарантирует непустые кортежи.

## Логика Stage 2
- Повторная инициализация токена; загрузка stage1 дампа из S3.
- По типам:
  - **DEFAULT**: `get_connections` для валидных профилей → `(unfiltered_dict, df_connect, df_users, subscriptions_df)`; повторная выгрузка профилей всех задействованных ID; нормализация схемы `df_connect`.
  - **HANDSHAKES**: построение графа 1–3 рукопожатий (`graph_search`), потом догрузка профилей для найденных ID → `(df_connect, df_users)`.
  - **SUB_GRAPH**: агрегирование подписок по группам целевых main_ids, получение сведений о группах → `(df_users, df_subs)`.
- Результаты валидируются по схеме (обязательные колонки, непустые ключевые df).

## Форматы данных и соглашения
- `df_connect`: столбцы `source,target,relation` (int); relation: `2` — дружба (взаимная/исходящая), `1` — подписка.
- `df_users`: профили, флаги `is_main_id`, динамические `relations_{main_id}` с значениями `main_id|mutual|friend|follower`.
- `subscriptions_df`/`df_subs`: `id_group,members_count,name,screen_name,image_url`.
- Stage1/2 сохраняются как tuple структур; при наличии `unfiltered_dict` (сырые связи по main_id) сохраняется отдельным parquet per main_id.

## Обработка ошибок и статусов
- Любая ошибка на стадии → `StatusUpdateEvent` с `ReportStatus.FAILED` и `FailureReason` (коды мапятся по VK ошибкам, сети, rate-limit и т.п.).
- Если токен мёртв на первом запросе → мгновенный FAIL с `VK_RECONNECT_REQUIRED`.
- При исчерпании ретраев слушателя (Redis) — фиксируется `MaxRetriesExceeded`, выставляется причина (rate limit, reconnect, access_denied, network) по тексту ошибки.
- При падении глобального пула токенов → `VK_GLOBAL_POOL_EXHAUSTED`.

## Антиспам/устойчивость в `Friends`
- Трёхэтапный `main_download`: (1) друзья/подписчики целевых, (2) профили с фильтрацией валидных, (3) связи только между валидными, (4) опционально группы.
- `_filter_valid_profiles` допускает закрытые профили, если `can_access_closed`; оставляет пользователей из `df_main_friends` даже с нулевыми счетчиками.
- `_get_batch_connections` (внутри get_connections) работает отдельными батчами по друзьям/подписчикам, с warmup и прогресс-колбэками; после основного прохода есть два retry-прохода с сменой токена.
- Обязательное присутствие `main_id` в `df_connect`: при отсутствии связей добавляется минимальная связь, чтобы граф корректно строился.

## Настройки окружения (кратко)
- `redis_url`, `stream_vk_parser`, `stream_status_change`, `stream_graph_worker`, `cg_vk_parser`, `consumer_vk_parser`.
- Token service: `token_service_url`, `token_service_api_key` (или `API_KEY` fallback).
- S3: `aws_access_key_id`, `aws_secret_access_key`, `aws_endpoint_url`, `bucket_name`.
- Конкурентность: `max_concurrent_messages` (по умолчанию 100), `max_retries` (по умолчанию 3) для слушателей.

## Что полезно для будущего скрипта «предложка в паблики»
- Использовать **UserToken** пользователя-отправителя (обеспечивает права писать в предложку групп, где это разрешено). При отсутствии — заранее валидировать и обновлять токен через `token-manager`.
- Повторить практики:
  - RPS-guard и случайные паузы (ориентир: ≥0.4s между вызовами, адаптивные задержки).
  - Категоризация VK ошибок и быстрый фейл на первом auth/ban.
  - При rate-limit — смена токена/прокси или backoff.
  - Логирование запросов с суффиксами токенов, хранение контекста ошибки.
- Экономить вызовы: резолв short name → id батчами (до 25), кэшировать, как в `resolve_screen_names_to_ids`.
- При массовой отправке в предложку — группировать по токенам, контролировать паузы, проверять доступность `can_post`/права группы перед отправкой, учитывать private/deactivated статусы как делает фильтрация в парсере.

## VK API (добавка для сервиса «предложка»)
- `wall.post` (основной вызов):
  - `owner_id` (int, обяз.) — стенка получателя. Для сообщества: отрицательный `-group_id`.
  - `message` (string) — текст предложки.
  - `attachments` (string) — через запятую `photo{owner_id}_{media_id}`, `doc{owner_id}_{id}`, `video...` и т.д.
  - `from_group` (0/1) — постить от имени сообщества (1) или пользователя (0). Для предложки обычно `0` (от лица пользователя).
  - `publish_date` (unixtime) — отложка; для предложки обычно не ставим.
  - `signed` (0/1) — подписывать автора, если постится от сообщества.
  - `guid` (string) — идемпотентность; можно ставить UUID на пост.
  - Результат для не-админов в сообществе с включёнными предложками — пост создаётся в статусе `post_type = suggest`.
- Загрузка вложений (фото на стену сообщества):
  - `photos.getWallUploadServer` → `upload_url`
  - POST multipart на `upload_url` (`photo` файл) → ответ `server, photo, hash`
  - `photos.saveWallPhoto` c этими полями → `owner_id`, `id`, `access_key` → формируем `photo{owner_id}_{id}_{access_key}`.
- Документы (если нужны):
  - `docs.getUploadServer` (type=wall, group_id) → upload_url
  - POST файл → `file`
  - `docs.save` → `doc{owner_id}_{id}_{access_key}`.
- Проверка прав/возможности публикации:
  - `groups.getById` (`group_ids`, `fields=can_post,can_suggest,can_upload_story`) — косвенно видно разрешена ли стена/предложка.
  - Альтернатива: тестовый `wall.post` с пустым `message` возвращает `[214] access denied: wall is disabled` — ловить как сигнал закрытой стены.
- Общие параметры любого вызова:
  - `access_token`, `v=5.131` (или актуальная), `lang`, `https`.
- Ошибки VK, важные для сервиса:
  - `[5]` auth failed / IP mismatch → немедленный фейл токена.
  - `[6]/[9]/[29]` too many requests → пауза/смена токена.
  - `[15]/[30]/[214]` access denied / wall closed → пропуск сообщества, лог причины.
  - `[14]` captcha needed — в автомате лучше скипать группу.

## План сервиса автопубликации в предложку
1) Вход: список `group_ids` (или screen name) + `message` + опционально вложения (пути к файлам/URL).
2) Токен:
   - Загружаем `UserToken` отправителя (`provider=vk`), обновляем при необходимости; проверяем, что это *user* токен с правом писать на стены сообществ (обычно достаточно общих прав, т.к. предложка открыта).
   - Если нужно много отправок — предусмотреть пул пользовательских токенов и ротацию при rate-limit.
3) Подготовка групп:
   - Резолв `screen_name` → id через `users.get`/`resolve_screen_names_to_ids` (batched).
   - Для групп используем отрицательные id (`owner_id = -gid`).
   - По желанию: `groups.getById` с `fields=can_post,can_suggest,is_closed,wall` — быстро отсекать закрытые стены/выключенную предложку.
4) Подготовка вложений (если есть):
   - Фото: `photos.getWallUploadServer` (group_id) → загрузка → `photos.saveWallPhoto` → собрать строки `photo{owner_id}_{id}_{access_key}`.
   - Док: `docs.getUploadServer` (type=wall, group_id) → загрузка → `docs.save` → `doc{owner_id}_{id}_{access_key}`.
   - Собрать `attachments` в одну строку (до 10 фото/доков на пост).
5) Публикация в предложку:
   - Вызов `wall.post` с:
     - `owner_id=-gid`
     - `message=<текст>`
     - `attachments=<сформировано>`
     - `from_group=0` (от лица пользователя; так пост уходит в предложку, если пользователь не админ).
     - `guid=<uuid>` для идемпотентности.
   - Обрабатывать:
     - `[214] access denied: wall is disabled` → скипнуть группу.
     - `[9]/[29]` → backoff/смена токена.
     - `[5]/IP mismatch` → фейл токена.
     - `[14] captcha` → скип/лог, повторять не стоит.
6) Ограничения и паузы:
   - Соблюдать паузы ≥0.4–0.5s между вызовами (лучше адаптивно, как в парсере).
   - Группировать вызовы: сначала массовая загрузка вложений (по группе), затем `wall.post`.
7) Логирование и идемпотентность:
   - Лог: группа, результат/код ошибки, суффикс токена, guid.
   - При повторном запуске с тем же `guid` — `wall.post` не создаст дубликат.
8) Валидация результата:
   - Успех: `response.post_id` возвращён; пост попадёт в очередь предложки, тип `suggest`.
   - Можно дополнительно дернуть `wall.getById` (`owner_id_post_id`) для проверки статуса, если нужно подтверждение.

## Быстрый справочник параметров (для разработки)
- `wall.post`: `owner_id`, `message`, `attachments`, `from_group`, `publish_date`, `signed`, `guid`, `lat`, `long`, `copyright`, `close_comments`, `mute_notifications`.
- `photos.getWallUploadServer`: `group_id` → `upload_url`.
- `photos.saveWallPhoto`: `group_id`, `photo`, `server`, `hash`.
- `docs.getUploadServer`: `type=wall`, `group_id` → `upload_url`.
- `docs.save`: `file`, `title` → doc id.
- `groups.getById`: `group_ids`, `fields=can_post,can_suggest,is_closed,wall`.


Ссылка для генерации ключа формируется следующим образом:
https://oauth.vk.com/authorize
  ?client_id=YOUR_CLIENT_ID
  &display=page
  &scope=wall,photos,docs,offline,groups
  &redirect_uri=https://oauth.vk.com/blank.html
  &response_type=token
  &v=5.131
  &state=123456