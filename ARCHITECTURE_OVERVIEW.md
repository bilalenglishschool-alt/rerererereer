# ARCHITECTURE_OVERVIEW

Дата: 2026-02-25

## 1) Сервисы
- `backend`: FastAPI webhook + web/audio lesson API + whisper transcription API
- `bot`: aiogram handlers (invite, students, lesson commands)
- `worker`: Redis consumer, async lesson processing
- `postgres`: primary DB
- `redis`: очередь задач

## 2) Источник правды по схеме
- Только Alembic (`alembic/versions/*`)
- Runtime не создаёт таблицы (нет `create_all()`)

Текущая линия миграций:
- `20260224_0001_initial_schema`
- `20260225_0002_lesson_text_flow`
- `20260225_0003_transcription_jobs`

## 3) Доменная модель
- `Tutor (tutors)` UUID PK
- `Student (students)` UUID PK
- `TutorStudent (tutor_student)` association, composite PK `(tutor_id, student_id)`
- `Invite (invites)` token-based onboarding
- `Lesson (lessons)`
  - для text flow: `in_progress -> processing -> draft_ready -> sent`
  - `sent_at` фиксирует факт отправки ученику
- `LessonChunk (lesson_chunks)`
  - audio: `path`, `size_bytes`
  - text: `content`
- `Artifact (artifacts)`
  - file-based (`path`) или text-based (`content`)
- `TranscriptionJob (transcription_jobs)`
  - статусы: `queued -> processing -> done` или `failed`
  - хранит `source_path`, `transcript_path`, `transcript_text`, `processing_attempts`

## 4) Основные потоки

### Invite onboarding
1. Tutor создаёт `/create_invite [student_uuid]`.
2. Student выполняет `/start invite_<token>`.
3. Бот валидирует token, создаёт/находит student, создаёт `tutor_student`, помечает `used_at`.

### Web/audio lesson flow
1. `/lesson_now` -> создаётся lesson + web link.
2. Frontend шлёт chunks в backend (`/api/lessons/{id}/chunks`).
3. `/finish` -> enqueue `process_audio_lesson`.
4. Worker: merge audio, transcript, draft, notify tutor.

### Text lesson flow (MVP v2)
1. `/lesson_start <student_uuid>` -> `status=in_progress`.
2. `/lesson_add <text>` -> запись в `lesson_chunks.content`.
3. `/lesson_finish` -> `status=processing`, enqueue `generate_artifacts`.
4. Worker: LLM draft generation (`summary/difficulties/homework`) или fallback, `status=draft_ready`.
5. `/lesson_send` -> отправка student, `status=sent`, `sent_at`.

### Whisper transcription flow
1. Клиент загружает аудио на `POST /api/transcribe/jobs`.
2. Backend сохраняет source file и создаёт `transcription_jobs` (`status=queued`).
3. В очередь уходит `task_type=transcribe_job`.
4. Worker обрабатывает задачу, вызывает Whisper и сохраняет transcript.
5. Клиент читает статус/результат через `GET /api/transcribe/jobs/{job_id}`.
6. Для `failed` доступен `POST /api/transcribe/jobs/{job_id}/retry`.
7. Для защиты backend применяет upload validation и rate limit (`6 req/min` на IP).
8. Worker периодически чистит старые `done/failed` transcription jobs и удаляет их файлы.
9. Для UX доступны `GET /api/transcribe/jobs` (history) и `GET /api/transcribe/jobs/{job_id}/transcript`.

## 5) Queue протокол
Redis list `lesson_tasks`.
In-flight list: `lesson_tasks:processing`.
Dead-letter list: `lesson_tasks:dead`.

Payload JSON:
```json
{"task_type":"...","lesson_id":"...","enqueued_at":1700000000000}
```

Поддерживаемые `task_type`:
- `process_audio_lesson`
- `generate_artifacts`
- `transcribe_job`

Backward compatibility:
- старый raw payload (`lesson_id` строкой) трактуется как `process_audio_lesson`.
- старый JSON payload без `enqueued_at` поддерживается, latency для него не считается.

Retry/failure policy:
- transient failure -> requeue
- max attempts reached -> dead-letter
- unknown task_type -> dead-letter

## 6) Reliability metrics
- `lesson_metrics:tasks_processed_total`
- `lesson_metrics:task_failures_total`
- `lesson_metrics:worker_failures` (ZSET, для окна 10 минут)
- `lesson_metrics:queue_latency_ms_last/max/sum/samples`
- `lesson_metrics:processing_duration_ms_last/max/sum/samples`
- endpoint `GET /metrics/worker`
- alert endpoint `GET /alerts/worker` (сравнение с threshold из env)

## 7) Безопасность
- В webhook логах нет полного payload.
- Логируется только минимум метаданных.
- `backups/` не хранится в git.

## 8) Операционные документы
- `DEPLOY_RESET_DB.md` — прод rollout для сценария RESET DB.
- `CURRENT_STATE.md` — актуальный operational snapshot.
