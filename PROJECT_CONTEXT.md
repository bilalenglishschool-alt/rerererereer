# PROJECT_CONTEXT

Дата: 2026-02-25
Репозиторий: `/Users/artur/Desktop/tutor-assistant-server-snapshot`

## Цель текущего этапа
Стабильный MVP backend для tutor-assistant с:
- invite-based multi-teacher model,
- двумя lesson-потоками (web/audio + text session),
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

## Критичные инварианты
- Один `in_progress` lesson на tutor (partial unique index).
- Все schema changes только миграциями Alembic.
- Queue payload содержит `task_type` + `lesson_id`.
- `lesson_chunks` поддерживает и audio (`path`), и text (`content`).
- `artifacts` поддерживает file (`path`) и text (`content`).

## Ограничения MVP
- Для text flow используется stub-обработка (без LLM).
- Summary = конкатенация chunks.
- Homework = статический placeholder.

## Где смотреть
- Архитектура: `ARCHITECTURE_OVERVIEW.md`
- Runbook reset rollout: `DEPLOY_RESET_DB.md`
- Snapshot окружения: `CURRENT_STATE.md`
- Базовый вход: `README.md`

## Следующий этап (рекомендуемо)
1. Добавить авто-тесты на lifecycle и permission checks.
2. Добавить monitoring/metrics по queue latency и fail-rate.
3. Уточнить стратегию non-reset migrations для будущего прод-retention.
4. По готовности подключить LLM генерацию вместо stub для text flow.
