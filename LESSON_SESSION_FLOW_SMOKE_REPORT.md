# LESSON_SESSION_FLOW_SMOKE_REPORT

Дата: 2026-02-25
Репозиторий: `/Users/artur/Desktop/tutor-assistant-server-snapshot`

## 1) Обязательные проверки окружения

### docker compose ps
```text
backend, worker, postgres, redis -> Up
backend published on 18000->8000
```

### Alembic current
```text
20260225_0002 (head)
```

### Alembic check
```text
No new upgrade operations detected.
```

### alembic_version
```text
20260225_0002
```

### \dt
```text
alembic_version, tutors, students, tutor_student, invites, lessons, lesson_chunks, artifacts
```

### /health
```json
{"status":"ok","postgres":true,"redis":true,"details":{}}
```

## 2) Lesson session flow smoke (text MVP v2)

Проверка выполнена через синтетические Telegram updates (`process_update`) с мокированием Telegram API вызовов (`Bot.__call__`), чтобы проверить именно бизнес-логику и БД переходы без внешней сети.

Сценарий:
1. `/start` (tutor)
2. `/add_student ...`
3. `/lesson_start <student_uuid>`
4. `/lesson_add` x2
5. `/lesson_finish`
6. ожидание worker -> `draft_ready`
7. `/lesson_send`

Фактический результат:
- `SMOKE_OK`
- `lesson_id`: `58174965-1575-44a2-aeb4-e624a9be1282`
- `chunks_count`: `2`
- `processing_status_after_send`: `done`
- `sent_at`: `2026-02-25T12:38:35.589781`
- созданы артефакты: `summary`, `homework`

Фактически отправленные бот-сообщения (мок):
- tutor: приветствие и команды
- tutor: student создан/привязан
- tutor: lesson started
- tutor: chunk #1 сохранен
- tutor: chunk #2 сохранен
- tutor: lesson sent for processing
- student: итоги урока
- tutor: подтверждение отправки

## 3) Итого
- Text lifecycle `in_progress -> processing -> draft_ready -> sent` подтвержден end-to-end.
- Очередь worker для `task_type=generate_artifacts` работает.
- Статус `sent` и `sent_at` фиксируются после `/lesson_send`.
- Данные smoke были очищены из БД после проверки.
