# CURRENT_STATE

Дата snapshot: 2026-02-25
Репозиторий: `/Users/artur/Desktop/tutor-assistant-server-snapshot`
Коммит: `3e1781d`

## 1) Runtime status

```bash
docker compose ps
```

```text
NAME                                         IMAGE                                     COMMAND                  SERVICE    STATUS
... backend ... worker ... postgres ... redis ... Up
```

(На snapshot backend был поднят на host-port `18000`.)

---

## 2) Alembic / schema status

```bash
docker compose exec backend alembic -c /app/alembic.ini current
```

```text
20260224_0001 (head)
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
20260224_0001
```

---

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

---

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

---

## 5) Runtime invariants
- Runtime не содержит `create_all()`.
- Webhook логирует metadata (`update_id`, type, `from_user_id`) без полного payload.
- Invite onboarding реализован: `/start invite_<token>`.
- `tutor_student` является источником истины для привязки tutor↔student.

---

## 6) Health

```bash
curl http://localhost:18000/health
```

```json
{"status":"ok","postgres":true,"redis":true,"details":{}}
```

---

## 7) Operational decision state
Текущий rollout-подход: **RESET DB allowed** (только при явном подтверждении владельца данных).

Production reset runbook:
- `/opt/tutor-assistant/DEPLOY_RESET_DB.md`

---

## 8) Known limitations / next priorities
- Добавить timezone-aware datetimes.
- Добавить авто-тесты invite/permissions/queue.
- Зафиксировать non-reset migration plan на случай, если data retention станет обязательной.
