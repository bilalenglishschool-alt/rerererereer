# ARCHITECTURE_OVERVIEW

Дата: 2026-02-25

## 1) Сервисы
- `backend`: FastAPI webhook + web/audio lesson API
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
4. Worker stub: summary/homework, `status=draft_ready`.
5. `/lesson_send` -> отправка student, `status=sent`, `sent_at`.

## 5) Queue протокол
Redis list `lesson_tasks`.

Payload JSON:
```json
{"task_type":"...","lesson_id":"..."}
```

Поддерживаемые `task_type`:
- `process_audio_lesson`
- `generate_artifacts`

Backward compatibility:
- старый raw payload (`lesson_id` строкой) трактуется как `process_audio_lesson`.

## 6) Безопасность
- В webhook логах нет полного payload.
- Логируется только минимум метаданных.
- `backups/` не хранится в git.

## 7) Операционные документы
- `DEPLOY_RESET_DB.md` — прод rollout для сценария RESET DB.
- `CURRENT_STATE.md` — актуальный operational snapshot.
