# DEPLOY_RESET_DB

Final runbook для production rollout в сценарии **RESET DB + clean migrations**.

Использовать только если владелец системы явно подтвердил, что существующие данные можно удалить.

---

## 1) Preconditions

```bash
cd /opt/tutor-assistant
git rev-parse --short HEAD
docker compose ls
docker compose ps
docker compose config --services
```

Проверь:
- ты в правильном каталоге (`/opt/tutor-assistant`),
- сервисы compose корректные,
- понимаешь текущий commit до деплоя.

---

## 2) Backup (обязателен даже при reset)

```bash
cd /opt/tutor-assistant
mkdir -p /opt/tutor-assistant-backups
ts=$(date +%Y%m%d_%H%M%S)
project=${COMPOSE_PROJECT_NAME:-$(basename "$PWD")}
```

### 2.1 SQL dump

```bash
docker compose exec -T postgres pg_dump -U tutor_assistant -d tutor_assistant \
  > /opt/tutor-assistant-backups/db_${ts}.sql
ls -lh /opt/tutor-assistant-backups/db_${ts}.sql
```

### 2.2 Volume backup: postgres_data

```bash
docker volume ls | grep "${project}_postgres_data"
docker run --rm \
  -v ${project}_postgres_data:/src:ro \
  -v /opt/tutor-assistant-backups:/dst \
  alpine sh -c "tar -czf /dst/postgres_data_${ts}.tar.gz -C /src ."
ls -lh /opt/tutor-assistant-backups/postgres_data_${ts}.tar.gz
```

### 2.3 Volume backup: tutor_data

```bash
docker volume ls | grep "${project}_tutor_data"
docker run --rm \
  -v ${project}_tutor_data:/src:ro \
  -v /opt/tutor-assistant-backups:/dst \
  alpine sh -c "tar -czf /dst/tutor_data_${ts}.tar.gz -C /src ."
ls -lh /opt/tutor-assistant-backups/tutor_data_${ts}.tar.gz
```

Сохрани пути backup-файлов в финальный отчёт.

---

## 3) Reset volumes + restart

```bash
cd /opt/tutor-assistant
docker compose down -v
docker compose up -d postgres redis
docker compose up -d backend worker
```

---

## 4) Migrations

```bash
docker compose exec backend alembic -c /app/alembic.ini upgrade head
docker compose exec backend alembic -c /app/alembic.ini current
docker compose exec backend alembic -c /app/alembic.ini check
```

Ожидаем:
- `20260224_0001 (head)`
- `No new upgrade operations detected.`

---

## 5) Schema verification

```bash
docker compose exec postgres psql -U tutor_assistant -d tutor_assistant -c "select version_num from alembic_version;"
docker compose exec postgres psql -U tutor_assistant -d tutor_assistant -c "\dt"
docker compose exec postgres psql -U tutor_assistant -d tutor_assistant -c "\d tutor_student"
docker compose exec postgres psql -U tutor_assistant -d tutor_assistant -c "\d invites"
docker compose exec postgres psql -U tutor_assistant -d tutor_assistant -c "\d students"
```

Проверить обязательно:
- PK `(tutor_id, student_id)` на `tutor_student`
- unique `invites.token`
- partial unique `students.tg_user_id where tg_user_id is not null`

---

## 6) App health

```bash
docker compose ps
host_port=${HOST_PORT:-8000}
curl -sS "http://localhost:${host_port}/health"
docker compose logs --tail=80 backend
docker compose logs --tail=80 worker
```

Ожидаем health:
```json
{"status":"ok","postgres":true,"redis":true,"details":{}}
```

---

## 7) Invite smoke test

### 7.1 Create invite token (DB-side smoke helper)

```bash
docker compose exec backend python - <<'PY'
from datetime import datetime, timedelta
import secrets
from tutor_assistant.database import SessionLocal
from tutor_assistant.models import Tutor, Invite

with SessionLocal() as db:
    tutor = Tutor(tg_user_id=999000111, tg_username='smoke_teacher', full_name='Smoke Teacher')
    db.add(tutor)
    db.flush()
    token = secrets.token_urlsafe(16)
    db.add(Invite(token=token, tutor_id=tutor.id, expires_at=datetime.utcnow() + timedelta(days=1)))
    db.commit()
    print(token)
PY
```

### 7.2 Claim in Telegram
- выполнить `/start invite_<token>`
- повторить тот же `/start invite_<token>`

### 7.3 Verify DB link

```bash
docker compose exec postgres psql -U tutor_assistant -d tutor_assistant -c "select tutor_id, student_id, is_active from tutor_student;"
```

Ожидаем:
- первый claim: success,
- второй claim: invite already used,
- запись в `tutor_student` присутствует.

---

## 8) Rollback

Если rollout неуспешен:

```bash
cd /opt/tutor-assistant
docker compose down -v
# checkout previous known-good commit
git checkout <old_commit>
docker compose up -d
```

Восстановление SQL:
```bash
cat /opt/tutor-assistant-backups/db_<timestamp>.sql | docker compose exec -T postgres psql -U tutor_assistant -d tutor_assistant
```

Восстановление volume (пример postgres_data):
```bash
docker volume create ${project}_postgres_data
docker run --rm \
  -v ${project}_postgres_data:/dst \
  -v /opt/tutor-assistant-backups:/src \
  alpine sh -c "cd /dst && tar xzf /src/postgres_data_<timestamp>.tar.gz"
```

---

## 9) Final report (обязательный)
В отчёте указать:
- deployed commit hash
- backup paths
- `docker compose ps`
- alembic `current` + `check`
- `alembic_version`
- tables list (`\dt`)
- health output
- invite smoke result (first/second claim)
- последние 80 строк backend/worker логов (или “no errors”).
