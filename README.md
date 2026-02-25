# Tutor Assistant MVP

Docker Compose проект с сервисами:
- `backend` (FastAPI + Telegram webhook)
- `bot` (Telegram polling, для локальной отладки)
- `worker` (очередь обработки уроков)
- `postgres`
- `redis`

## Текущий архитектурный статус
- Schema truth: Alembic migrations only (runtime без `create_all()`).
- Канон ownership: `tutors` ↔ `students` через `tutor_student`.
- Invite onboarding работает через `/start invite_<token>`.
- Worker и backend разделены, связь через Redis queue.
- PII-safe webhook logging: логируются только `update_id`, `event_type`, `from_user_id`.
- Отдельный Whisper transcription flow: `/transcribe` + `transcription_jobs`.

## Переменные окружения (.env)
- `BOT_TOKEN`
- `BASE_URL`
- `STORAGE_PATH`
- `LLM_PROVIDER`
- `LLM_API_KEY`
- `OPENAI_API_KEY` (используется как fallback, если `LLM_API_KEY` пустой)
- `OPENAI_MODEL` (по умолчанию `gpt-4o-mini`)
- `WHISPER_MODEL` (`base` по умолчанию, можно `small`)
- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `DATABASE_URL` (пример: `postgresql+psycopg://USER:PASSWORD@postgres:5432/DB`)
- `REDIS_URL`
- `HOST_PORT`

Пример: `.env.example`

## Запуск

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec backend alembic -c /app/alembic.ini upgrade head
```

## Проверка

```bash
curl http://localhost:${HOST_PORT:-8000}/health
```

Ожидаемый формат ответа:
```json
{
  "status": "ok",
  "postgres": true,
  "redis": true,
  "details": {}
}
```

## Команды бота
- `/start`
- `/start invite_<token>`
- `/add_student <имя> | <@username или telegram_id>`
- `/create_invite [student_uuid]`
- `/lesson_now [student_uuid]` (web/audio flow)
- `/lesson_start <student_uuid>`
- `/lesson_add <текст>`
- `/lesson_finish`
- `/lesson_send`

## Lesson flows

### 1) Web/audio flow (существующий)
1. `/lesson_now` создаёт урок и ссылку.
2. Web-страница отправляет audio chunks в backend.
3. `POST /finish` ставит задачу `process_audio_lesson` в Redis.
4. Worker делает merge + transcript + draft и отправляет tutor preview.

### 2) Text session flow (MVP v2)
1. `/lesson_start <student_uuid>` создаёт lesson со статусом `in_progress`.
2. `/lesson_add <text>` добавляет текстовые chunks в `lesson_chunks.content`.
3. `/lesson_finish` переводит lesson в `processing` и ставит задачу `generate_artifacts`.
4. Worker генерирует `summary/difficulties/homework` через LLM (или fallback) и переводит lesson в `draft_ready`.
5. `/lesson_send` отправляет summary ученику и переводит lesson в `sent` (`sent_at` заполняется).

Строгий порядок для text-flow: `in_progress -> processing -> draft_ready -> sent`.

### 3) Whisper transcription flow (web/API)
1. Открыть `/transcribe` и загрузить аудио.
2. Backend создаёт `transcription_jobs` запись в статусе `queued`.
3. В Redis ставится задача `task_type=transcribe_job`.
4. Worker делает транскрибацию через Whisper и сохраняет `transcript.txt`.
5. Статус/результат доступен через `GET /api/transcribe/jobs/{job_id}`.
6. При статусе `failed` можно выполнить `POST /api/transcribe/jobs/{job_id}/retry`.

## Каноничная схема
Таблицы:
- `tutors`
- `students`
- `tutor_student`
- `invites`
- `lessons`
- `lesson_chunks`
- `artifacts`
- `transcription_jobs`
- `alembic_version`

Ключевые инварианты:
- `students.tg_user_id` nullable + partial unique (`IS NOT NULL`).
- `tutor_student` composite PK (`tutor_id`, `student_id`).
- `invites.token` unique.
- Один `in_progress` lesson на tutor (partial unique index в `lessons`).
- Для transcription-job хранится статус/ошибка/число попыток в `transcription_jobs`.

## Документация
- Runtime snapshot: `CURRENT_STATE.md`
- Prod reset rollout: `DEPLOY_RESET_DB.md`
- Prod go-live checklist: `PROD_GO_LIVE_CHECKLIST.md`
- Архитектура: `ARCHITECTURE_OVERVIEW.md`
- Контекст проекта: `PROJECT_CONTEXT.md`

## Логи

```bash
docker compose logs -f backend
docker compose logs -f worker
docker compose logs -f bot
```

## Автотесты

```bash
docker compose exec backend python -m unittest discover -s tutor_assistant/tests -p 'test_*.py' -v
```

Покрытые блоки:
- lesson lifecycle (text flow)
- invite-flow (invalid/expired/used/idempotent)
- whisper transcription flow (create/status/retry + worker success path)
- webhook logging privacy regression
- worker retry policy (requeue/dead-letter)

## Reliability & Observability

- Worker counters в Redis:
  - `lesson_metrics:tasks_processed_total`
  - `lesson_metrics:task_failures_total`
- Failure events (last-10m check): `lesson_metrics:worker_failures` (sorted set)
- Dead-letter queue: `lesson_tasks:dead`
- HTTP endpoint:
  - `GET /metrics/worker`
