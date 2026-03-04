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
20260225_0003 (head)
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
20260225_0003
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
- `transcription_jobs`

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

### transcription_jobs (new)
- `status` (`queued` / `processing` / `done` / `failed`)
- `processing_attempts`, `processing_error`
- `source_path`, `transcript_path`, `transcript_text`

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
- Worker task `generate_artifacts` обрабатывается и заполняет summary/difficulties/homework (LLM или fallback).

## 7) Whisper transcription flow status
- API endpoints:
  - `GET /transcribe`
  - `POST /api/transcribe/jobs`
  - `GET /api/transcribe/jobs/{job_id}`
  - `POST /api/transcribe/jobs/{job_id}/cancel`
  - `DELETE /api/transcribe/jobs/{job_id}`
  - `POST /api/transcribe/jobs/{job_id}/retry`
- Queue task: `task_type=transcribe_job`
- Worker поддерживает retry/dead-letter policy для transcription-job.
- На upload endpoint включен rate limit: `6` загрузок в минуту на IP.
- Удаление transcription job разрешено только для `done|failed|canceled` (для `queued|processing` -> `409`).
- Worker выполняет periodic retention cleanup старых `done/failed/canceled` transcription jobs.

## 8) Reliability status
- Invite-flow тесты покрыты: invalid / expired / used / idempotent claim.
- Webhook privacy-regression покрыт тестом и CI grep-guard (лог только metadata, без payload).
- Worker policy:
  - transient failure -> requeue
  - max attempts reached -> dead-letter (`lesson_tasks:dead`)
  - unknown `task_type` -> dead-letter
  - transcription-job использует ту же retry/dead-letter политику
- Worker metrics endpoint:
  - `GET /metrics/worker`
  - `GET /metrics/worker/prometheus`
  - `GET /alerts/worker`
  - `GET /ops/worker/dead-letter`
  - `POST /ops/worker/dead-letter/requeue`
  - `task_type` filter валидируется (`process_audio_lesson|generate_artifacts|transcribe_job`), иначе `400`
  - у requeue нужен filter (`task_type` или `lesson_id`), иначе `400` (обход только через `allow_bulk=true`)
- при заданном `OPS_API_TOKEN` endpoints `/metrics/worker*`, `/alerts/worker`, `/ops/worker/*` требуют заголовок `X-Ops-Token`
  - alerts thresholds: `worker_errors_last_10m`, `dead_letter_requeued_last_10m`, `dead_letter_depth`, `queue_depth`, `transcribe_queue_depth`, `transcribe_oldest_queue_age_seconds`, `transcribe_oldest_processing_age_seconds`, `transcribe_dead_letter_depth`, `transcribe_oldest_dead_letter_age_seconds`, `worker_heartbeat_age_seconds`
  - при недоступном Redis endpoints worker metrics/alerts возвращают `503`
  - fields: `tasks_processed_total`, `task_failures_total`, `dead_letter_requeued_total`, `dead_letter_requeued_last_10m`, `worker_errors_last_10m`,
    `queue_depth`, `processing_depth`, `dead_letter_depth`,
    `transcribe_queue_depth`, `transcribe_processing_depth`, `transcribe_oldest_queue_age_seconds`, `transcribe_oldest_processing_age_seconds`, `transcribe_dead_letter_depth`, `transcribe_oldest_dead_letter_age_seconds`,
    `queue_depth_by_type`, `processing_depth_by_type`, `dead_letter_depth_by_type`,
    `queue_latency_ms_last/max/avg`, `processing_duration_ms_last/max/avg`,
    `queue_latency_ms_last_by_type/max_by_type/avg_by_type`,
    `processing_duration_ms_last_by_type/max_by_type/avg_by_type`,
    `worker_heartbeat_ts`, `worker_heartbeat_age_seconds`,
    `tasks_processed_by_type`, `task_failures_by_type`, `dead_letter_requeued_by_type`
  - Prometheus format: text `version=0.0.4`
  - Prometheus includes labeled counters and labeled latency/duration gauges by `task_type`
- External monitor helper:
  - `python -m tutor_assistant.ops.check_worker_alerts`
- scheduled workflow `.github/workflows/worker-alert-monitor.yml` (needs `WORKER_ALERT_URL`, optional `WORKER_ALERT_TOKEN` secret)

## 9) Health

```bash
curl http://localhost:${HOST_PORT:-8000}/health
```

Ожидаемый ответ:
```json
{"status":"ok","postgres":true,"redis":true,"details":{}}
```

## 10) Operational decision state
Текущий rollout-подход: **RESET DB allowed** (только при явном подтверждении владельца данных).

Production reset runbook:
- `DEPLOY_RESET_DB.md`

## 11) Known limitations / next priorities
- При необходимости data retention: подготовить non-reset migration plan.
