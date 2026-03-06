# VPS_DEPLOY_RUNBOOK

Практический runbook для выката проекта на VPS через Docker Compose.

## 1) Что уже есть в репозитории
- Скрипт: `scripts/deploy_prod.sh`
- Reset runbook: `DEPLOY_RESET_DB.md`
- Go-live checklist: `PROD_GO_LIVE_CHECKLIST.md`

`deploy_prod.sh` поддерживает шаги:
- `preflight`
- `backup`
- `deploy`
- `verify`
- `all`
- `rollback-help`

## 2) Предпосылки на VPS

Пример рабочей директории: `/opt/tutor-assistant`

Установить:
- Docker Engine
- Docker Compose plugin
- Git
- curl

Проверка:

```bash
docker --version
docker compose version
git --version
curl --version
```

## 3) Подготовка проекта на VPS

```bash
cd /opt
git clone <REPO_URL> tutor-assistant
cd tutor-assistant
cp .env.example .env
```

Заполнить `.env` минимум:
- `BOT_TOKEN`
- `BASE_URL` (публичный HTTPS URL backend)
- `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
- `DATABASE_URL`
- `REDIS_URL`
- `HOST_PORT`

Опционально:
- `OPS_API_TOKEN` (для защиты `/metrics/worker*`, `/alerts/worker`, `/ops/worker/*`)

## 4) Безопасный деплой (без сброса БД)

Рекомендуемый вариант по умолчанию.

```bash
cd /opt/tutor-assistant
./scripts/deploy_prod.sh --step all --mode safe --ref <tag_or_sha>
```

Что делает `all`:
1. `preflight` (проверка env/compose/команд)
2. `backup` (SQL + volume archive)
3. `deploy` (build, up postgres/redis, alembic upgrade, up backend/worker)
4. `verify` (alembic current/check, health, metrics)

## 5) Reset deploy (только при явном согласовании)

Если разрешено удаление данных:

```bash
cd /opt/tutor-assistant
./scripts/deploy_prod.sh --step all --mode reset --confirm-reset YES --ref <tag_or_sha>
```

Важно:
- без `--confirm-reset YES` скрипт не выполнит reset.
- перед reset всё равно создаётся backup.

## 6) Отдельные шаги (если нужно вручную)

```bash
./scripts/deploy_prod.sh --step preflight
./scripts/deploy_prod.sh --step backup
./scripts/deploy_prod.sh --step deploy --mode safe --ref <tag_or_sha>
./scripts/deploy_prod.sh --step verify
```

Запуск `bot` (polling profile) вместе с деплоем:

```bash
./scripts/deploy_prod.sh --step deploy --mode safe --ref <tag_or_sha> --with-bot
```

## 7) Проверка после выката

```bash
docker compose ps
curl -sS "http://localhost:${HOST_PORT:-8000}/health"
```

И дополнительно:

```bash
docker compose run --rm backend alembic -c /app/alembic.ini current
docker compose run --rm backend alembic -c /app/alembic.ini check
```

Ожидается:
- health `status=ok`
- `No new upgrade operations detected.`

## 8) Rollback

Быстрые подсказки:

```bash
./scripts/deploy_prod.sh --step rollback-help --ref <previous_tag_or_sha> --backup-dir <backup_path>
```

Для аварийного отката:
1. checkout предыдущего стабильного ref
2. поднять stack
3. при необходимости восстановить SQL/volumes из backup

## 9) GitHub Secrets (для внешнего мониторинга)

Если используете workflow `.github/workflows/worker-alert-monitor.yml`, в репозитории должны быть:
- `WORKER_ALERT_URL` (обязательно)
- `WORKER_ALERT_TOKEN` (опционально, если задан `OPS_API_TOKEN`)

## 10) Рекомендованный порядок для прод-релиза

1. Merge PR в `main`
2. Tag релиза (`vX.Y.Z`) и фиксация commit SHA
3. Deploy на VPS по runbook выше
4. Canonical smoke test (invite + lesson flow + transcription)
5. Запись итогового rollout-отчёта
