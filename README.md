# Tutor Assistant MVP

Docker Compose проект с сервисами:
- `backend` (FastAPI + Telegram webhook)
- `bot` (Telegram polling, опционально для локальной разработки)
- `worker` (очередь обработки уроков)
- `postgres`
- `redis`

## Что изменено
- База данных: Postgres через `DATABASE_URL` (единый источник правды для backend/bot/worker).
- `finish` в backend только ставит задачу в Redis, тяжёлая обработка выполняется в `worker`.
- Транскрибация: локальный `faster-whisper` на CPU.
- Идемпотентность: повторные задачи не дублируют результат; отправка ученику остаётся одноразовой.

## Переменные окружения (.env)
- `BOT_TOKEN`
- `BASE_URL`
- `STORAGE_PATH`
- `LLM_PROVIDER`
- `LLM_API_KEY`
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

Runbooks:
- reset deploy: `DEPLOY_RESET_DB.md`
- state snapshot: `CURRENT_STATE.md`

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
- `/lesson_now [student_uuid]`
- `/create_invite [student_uuid]`

## Каноничная схема (сейчас)
- `tutors`
- `students`
- `tutor_student`
- `invites`
- `lessons`
- `lesson_chunks`
- `artifacts`
- `alembic_version`

## Whisper (локально)
- Worker использует `faster-whisper` на CPU и автоматически скачивает модель при первом запуске.
- Выбор модели через `WHISPER_MODEL`:
  - `base` (default)
  - `small` (лучше качество, тяжелее)
- Кэш модели: `${STORAGE_PATH}/whisper-cache`.

## Поток обработки урока
1. Web-страница отправляет чанки в backend.
2. `finish` в backend:
   - отмечает урок `finished`
   - ставит задачу в Redis queue.
3. Worker получает `process_lesson(lesson_id)` и делает:
   - склейку записи
   - транскрибацию через `faster-whisper`
   - генерацию summary/difficulties/homework
   - сохранение артефактов и черновика в БД
   - отправку черновика учителю в Telegram.

## Где смотреть транскрипт
- В БД: поле `lessons.transcript_text`.
- В файле: `${STORAGE_PATH}/lessons/<lesson_id>/transcript.txt`.

## Логи

```bash
docker compose logs -f worker
docker compose logs -f backend
docker compose logs -f bot
```

В логах worker есть этапы транскрибации: `transcribing...`, `done transcribing`, длительность.
