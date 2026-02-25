# CURRENT_STATE

Дата: 2026-02-24
Репозиторий: `/Users/artur/Desktop/tutor-assistant-server-snapshot`

## 1) Инвентаризация БД/Compose

Команда:
```bash
docker compose ps
```
Вывод:
```text
NAME                                         IMAGE                COMMAND                  SERVICE    CREATED              STATUS                        PORTS
tutor-assistant-server-snapshot-postgres-1   postgres:16-alpine   "docker-entrypoint.s…"   postgres   About a minute ago   Up About a minute (healthy)   5432/tcp
tutor-assistant-server-snapshot-redis-1      redis:7-alpine       "docker-entrypoint.s…"   redis      About a minute ago   Up About a minute (healthy)   6379/tcp
```

Команда:
```bash
docker compose run --rm backend alembic -c /app/alembic.ini current
```
Вывод:
```text
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
```
(текущая ревизия не показана, `alembic_version` отсутствует)

Команда:
```bash
docker compose exec postgres psql -U tutor_assistant -d tutor_assistant -c "select version_num from alembic_version;"
```
Вывод:
```text
ERROR:  relation "alembic_version" does not exist
LINE 1: select version_num from alembic_version;
                                ^
```

Команда:
```bash
docker compose exec postgres psql -U tutor_assistant -d tutor_assistant -c "\\dt"
```
Вывод:
```text
Did not find any relations.
```

Проверка таблиц:
```bash
\d students
\d teachers
\d tutors
\d invites
\d teacher_student
```
Результат: во всех случаях `Did not find any relation named ...`.

## 2) ORM-модели (текущие)

Ключевые факты из `tutor_assistant/models.py`:
- есть `Tutor`/`Student`/`Lesson`/`LessonChunk`/`Artifact`
- `Tutor.id` и `Student.id` сейчас `Integer`
- ownership сейчас через `students.tutor_id`
- `Invite` и `tutor_student/teacher_student` в ORM отсутствуют

## 3) Где вызывается create_all

Команда:
```bash
rg -n "create_all\\(" /Users/artur/Desktop/tutor-assistant-server-snapshot
```
Вывод:
```text
/Users/artur/Desktop/tutor-assistant-server-snapshot/tutor_assistant/database.py:27:    Base.metadata.create_all(bind=engine)
```

## 4) Наблюдаемые расхождения

- Миграции в `alembic/versions` используют `teachers`/UUID и `teacher_student`, но runtime-модели используют `tutors`/int и `students.tutor_id`.
- `/start invite_<token>` в bot runtime отсутствует.
- Webhook логирует полный payload.
- `backups/` попал в git-index.

## 5) Решение по данным (обязательный вопрос)

Решение: **данные не важны**.

Выбранный подход: **reset DB + clean schema + clean migrations**.

Обоснование:
- быстрее и безопаснее, чем конверсия int→UUID с промежуточной совместимостью;
- устраняет исторические расхождения одним проходом;
- снижает риск скрытых edge-case в миграциях переноса.
