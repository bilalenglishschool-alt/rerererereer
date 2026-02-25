# PROJECT_CONTEXT

Дата: 2026-02-25
Репозиторий: `/Users/artur/Desktop/tutor-assistant-server-snapshot`

## Цель текущего этапа
Стабильный MVP backend для tutor-assistant с:
- invite-based multi-teacher model,
- двумя lesson-потоками (web/audio + text session),
- отдельным Whisper transcription flow,
- очередью фоновой обработки,
- предсказуемой схемой через Alembic.

## Что уже готово
- Единая schema-модель (`tutors/students/tutor_student/invites`).
- Invite onboarding через `/start invite_<token>`.
- No-runtime-schema-create policy.
- Безопасный webhook logging без full payload.
- Worker через Redis queue.
- Text lesson lifecycle v2:
  - `/lesson_start`
  - `/lesson_add`
  - `/lesson_finish`
  - `/lesson_send`
  - статусный переход `in_progress -> processing -> draft_ready -> sent`.
- Invite-flow тесты (invalid/expired/used/idempotent).
- Webhook privacy regression test (metadata-only logging).
- Worker retry/dead-letter policy + базовые reliability counters.
- Endpoint alerting: `GET /alerts/worker` по порогам `worker_errors_last_10m`, `dead_letter_depth`, `queue_depth`, `worker_heartbeat_age_seconds`.
- Whisper transcription jobs:
  - `POST /api/transcribe/jobs`
  - `GET /api/transcribe/jobs`
  - `GET /api/transcribe/jobs/{job_id}`
  - `GET /api/transcribe/jobs/{job_id}/transcript`
  - `POST /api/transcribe/jobs/{job_id}/retry`
  - web UI `/transcribe`.
  - защита API: размер/тип файла + rate limit (`6` загрузок в минуту на IP).
  - retention cleanup: worker удаляет старые `done/failed` jobs и их файлы.

## Критичные инварианты
- Один `in_progress` lesson на tutor (partial unique index).
- Все schema changes только миграциями Alembic.
- Queue payload содержит `task_type` + `lesson_id` + `enqueued_at`.
- `task_type=transcribe_job` использует тот же payload-формат (в `lesson_id` передаётся `job_id`).
- `lesson_chunks` поддерживает и audio (`path`), и text (`content`).
- `artifacts` поддерживает file (`path`) и text (`content`).
- `transcription_jobs` хранит retry state (`processing_attempts`, `processing_error`).

## Ограничения MVP
- LLM зависит от конфигурации (`LLM_PROVIDER=openai` + API key).
- При недоступном LLM включается безопасный fallback без падения worker.

## Где смотреть
- Архитектура: `ARCHITECTURE_OVERVIEW.md`
- Runbook reset rollout: `DEPLOY_RESET_DB.md`
- Snapshot окружения: `CURRENT_STATE.md`
- Базовый вход: `README.md`

## Следующий этап (рекомендуемо)
1. Уточнить стратегию non-reset migrations для будущего прод-retention.
