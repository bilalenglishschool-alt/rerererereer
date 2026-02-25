# DEPLOY_RESET_DB

Короткий runbook для деплоя в режиме **reset DB + clean migrations**.

Важно: применять только если подтверждено, что старые данные можно удалять.

## 1) Pre-check (обязательно)

```bash
cd /opt/tutor-assistant
docker compose ls
docker compose ps
docker compose config --services
```

Проверь:
- работаешь в нужном проекте;
- нужные сервисы: `backend`, `worker`, `postgres`, `redis`.

## 2) Backup вне git (даже если данных \"нет\")

### 2.1 Дамп БД

```bash
cd /opt/tutor-assistant
mkdir -p /opt/tutor-assistant-backups
ts=$(date +%Y%m%d_%H%M%S)
docker compose exec -T postgres pg_dump -U tutor_assistant -d tutor_assistant \
  > /opt/tutor-assistant-backups/db_${ts}.sql
```

### 2.2 Backup тома данных уроков

```bash
cd /opt/tutor-assistant
ts=$(date +%Y%m%d_%H%M%S)
project=${COMPOSE_PROJECT_NAME:-$(basename "$PWD")}
docker volume ls | grep "${project}_tutor_data"
docker run --rm \
  -v ${project}_tutor_data:/src:ro \
  -v /opt/tutor-assistant-backups:/dst \
  alpine sh -c "tar -czf /dst/tutor_data_${ts}.tar.gz -C /src ."
```

## 3) Reset БД и данных

```bash
cd /opt/tutor-assistant
docker compose down -v
docker compose up -d postgres redis
docker compose up -d backend worker
docker compose exec backend alembic -c /app/alembic.ini upgrade head
```

Примечание:
- `down -v` удалит compose volumes (`postgres_data`, `tutor_data`) для текущего проекта.

## 4) Проверка после деплоя

```bash
cd /opt/tutor-assistant
docker compose ps
docker compose logs --tail=80 backend
docker compose logs --tail=80 worker
docker compose exec backend alembic -c /app/alembic.ini current
docker compose exec postgres psql -U tutor_assistant -d tutor_assistant -c "select version_num from alembic_version;"
docker compose exec postgres psql -U tutor_assistant -d tutor_assistant -c "\dt"
```

Ожидаем:
- ревизия `20260224_0001`;
- таблицы: `tutors`, `students`, `tutor_student`, `invites`, `lessons`, `lesson_chunks`, `artifacts`, `alembic_version`.

## 5) Smoke onboarding invite

```bash
cd /opt/tutor-assistant
docker compose exec backend python - <<'PY'
from datetime import datetime, timedelta
from tutor_assistant.database import SessionLocal
from tutor_assistant.models import Tutor, Invite
import secrets

with SessionLocal() as db:
    tutor = Tutor(tg_user_id=999000111, tg_username="smoke_teacher", full_name="Smoke Teacher")
    db.add(tutor)
    db.flush()
    token = secrets.token_urlsafe(16)
    db.add(Invite(token=token, tutor_id=tutor.id, expires_at=datetime.utcnow() + timedelta(days=1)))
    db.commit()
    print(token)
PY
```

Дальше в Telegram:
- выполнить `/start invite_<token>`;
- проверить связь:

```bash
docker compose exec postgres psql -U tutor_assistant -d tutor_assistant -c "select tutor_id, student_id, is_active from tutor_student;"
```

## 6) Rollback (если нужно)

1. Остановить сервисы:
```bash
cd /opt/tutor-assistant
docker compose down
```
2. Поднять postgres и восстановить дамп:
```bash
docker compose up -d postgres
cat /opt/tutor-assistant-backups/db_<timestamp>.sql | docker compose exec -T postgres psql -U tutor_assistant -d tutor_assistant
```
3. При необходимости восстановить `tutor_data` из `tar.gz` в соответствующий volume.
