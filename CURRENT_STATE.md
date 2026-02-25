# CURRENT_STATE

Дата snapshot: 2026-02-25
Репозиторий: `/Users/artur/Desktop/tutor-assistant-server-snapshot`

## 1) Runtime status

```bash
docker compose ps
```

```text
backend, worker, postgres, redis -> Up
```

## 2) Alembic / schema status

```bash
docker compose exec backend alembic -c /app/alembic.ini current
```

```text
20260225_0002 (head)
```

```bash
docker compose exec backend alembic -c /app/alembic.ini check
```

```text
No new upgrade operations detected.
```

```bash
docker compose exec postgres psql -U tutor_assistant -d tutor_assistant -c "select version_num from alembic_version;"
```

```text
20260225_0002
```

## 3) Current canonical tables

```bash
docker compose exec postgres psql -U tutor_assistant -d tutor_assistant -c "\dt"
```

Список:
- `alembic_version`
- `tutors`
- `students`
- `tutor_student`
- `invites`
- `lessons`
- `lesson_chunks`
- `artifacts`

## 4) Critical constraints verified

### tutor_student
- composite PK `(tutor_id, student_id)`
- `is_active` boolean

### invites
- `token` unique index
- FK на `tutors` и `students`
- `expires_at`, `used_at`

### students
- `tg_user_id` nullable
- partial unique index `WHERE tg_user_id IS NOT NULL`

### lessons (new)
- partial unique index `ux_lessons_one_in_progress_per_tutor`
- `sent_at` timestamp

### lesson_chunks / artifacts (new)
- `lesson_chunks.content` для text flow
- `lesson_chunks.path/size_bytes` nullable
- `artifacts.content` для text artifacts
- `artifacts.path` nullable

## 5) Runtime invariants
- Runtime не содержит `create_all()`.
- Webhook логирует только metadata (`update_id`, type, `from_user_id`) без полного payload.
- Invite onboarding реализован: `/start invite_<token>`.
- `tutor_student` является источником истины tutor↔student.

## 6) Text lesson flow v2 status
- Реализованы команды:
  - `/lesson_start <student_uuid>`
  - `/lesson_add <text>`
  - `/lesson_finish`
  - `/lesson_send`
- Статус-переходы: `in_progress -> processing -> draft_ready -> sent`.
- Worker task `generate_artifacts` обрабатывается и заполняет summary/homework (stub).

## 7) Health

```bash
curl http://localhost:${HOST_PORT:-8000}/health
```

Ожидаемый ответ:
```json
{"status":"ok","postgres":true,"redis":true,"details":{}}
```

## 8) Operational decision state
Текущий rollout-подход: **RESET DB allowed** (только при явном подтверждении владельца данных).

Production reset runbook:
- `DEPLOY_RESET_DB.md`

## 9) Known limitations / next priorities
- Расширить автотесты на invite-flow, webhook и retry-ветки очереди.
- При необходимости data retention: подготовить non-reset migration plan.
