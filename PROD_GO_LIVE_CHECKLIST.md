# PROD_GO_LIVE_CHECKLIST

Production rollout checklist after merge.

## 1) Release Pin

1. Pin release by commit SHA and tag:

```bash
git rev-parse HEAD
git tag -a v0.1.0 -m "Release v0.1.0: lesson flow v2 + reliability"
git push origin main
git push origin v0.1.0
```

2. Record in release note:
- release tag: `v0.1.0`
- commit SHA: `<fill-from-git-rev-parse>`

## 2) Preflight Before RESET Deploy

Run from production project directory (`/opt/tutor-assistant`):

```bash
git rev-parse --short HEAD
docker compose ls
docker compose ps
docker compose config --services
```

Confirm:
- correct directory/project
- correct commit/tag checked out
- reset approved by owner
- backups completed and paths documented

## 3) Backups (Mandatory)

Use `DEPLOY_RESET_DB.md` section "Backup (обязателен даже при reset)".
Must record:
- SQL dump path
- postgres volume tar path
- tutor_data volume tar path

## 4) Reset Deploy

Follow `DEPLOY_RESET_DB.md` exactly:

```bash
docker compose down -v
docker compose up -d postgres redis
docker compose up -d backend worker
docker compose exec backend alembic -c /app/alembic.ini upgrade head
```

## 5) Post-Deploy Verification

```bash
docker compose exec backend alembic -c /app/alembic.ini current
docker compose exec backend alembic -c /app/alembic.ini check
docker compose exec postgres psql -U tutor_assistant -d tutor_assistant -c "select version_num from alembic_version;"
docker compose exec postgres psql -U tutor_assistant -d tutor_assistant -c "\\dt"
curl -sS "http://localhost:${HOST_PORT:-8000}/health"
curl -sS "http://localhost:${HOST_PORT:-8000}/metrics/worker"
```

Expected:
- Alembic: `20260225_0002 (head)`
- `No new upgrade operations detected`
- health: `status=ok`
- metrics endpoint returns counters and queue depths JSON

## 6) Canonical Smoke (Invite + Lesson v2)

1. Tutor: `/start`
2. Tutor: `/create_invite [student_uuid optional]`
3. Student: `/start invite_<token>`
- first claim: success
- second claim: already used
4. Tutor:
- `/lesson_start <student_uuid>`
- `/lesson_add ...` (2-3 times)
- `/lesson_finish`
- wait until `draft_ready`
- `/lesson_send`

DB checks:
- `tutor_student` link exists exactly once
- lesson transitions: `in_progress -> processing -> draft_ready -> sent`
- artifacts include: `summary`, `difficulties`, `homework`

## 7) Reliability Checks

1. Webhook log privacy:
- logs include only `update_id`, `event_type`, `from_user_id`
- no full payload / invite token / message text logs

2. Worker failure handling:
- transient failure -> requeue
- max attempts -> dead-letter list `lesson_tasks:dead`

3. Basic alert condition (manual MVP):
- `worker_errors_last_10m > 0` from `/metrics/worker` requires operator attention

## 8) Final Rollout Report

Must include:
- release tag + commit SHA
- backup artifact paths
- alembic current/check output
- health + worker metrics output
- canonical smoke results
- backend/worker logs summary (or "no errors")
