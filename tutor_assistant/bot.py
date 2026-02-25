from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User
from sqlalchemy import update

from .config import get_settings
from .database import SessionLocal, init_db
from .drafts import generate_draft
from .models import Invite, Lesson, Student, Tutor, TutorStudent
from .telegram_texts import (
    EDIT_DRAFT_PREFIX,
    REGEN_DRAFT_PREFIX,
    SEND_STUDENT_PREFIX,
    build_draft_text,
    build_student_text,
)

logger = logging.getLogger(__name__)
settings = get_settings()

LESSON_PICK_PREFIX = "lesson_pick:"
pending_edits: dict[int, str] = {}


def ensure_tutor(db, tg_user_id: int, tg_username: str | None, full_name: str | None) -> Tutor:
    tutor = db.query(Tutor).filter(Tutor.tg_user_id == tg_user_id).first()
    if tutor:
        tutor.tg_username = tg_username
        tutor.full_name = full_name
        db.commit()
        return tutor

    tutor = Tutor(
        tg_user_id=tg_user_id,
        tg_username=tg_username,
        full_name=full_name,
    )
    db.add(tutor)
    db.commit()
    db.refresh(tutor)
    return tutor


def parse_student_command_payload(raw: str) -> tuple[str, str | None, int | None]:
    if not raw:
        raise ValueError("empty payload")

    if "|" in raw:
        name_part, contact_part = [part.strip() for part in raw.split("|", 1)]
    else:
        parts = raw.split()
        if len(parts) < 2:
            raise ValueError("contact missing")
        name_part = " ".join(parts[:-1]).strip()
        contact_part = parts[-1].strip()

    if not name_part:
        raise ValueError("name missing")

    contact = (contact_part or "").strip()
    tg_username: str | None = None
    tg_user_id: int | None = None

    if contact.startswith("@"):
        tg_username = contact[1:]
    elif contact.isdigit():
        tg_user_id = int(contact)
    elif contact:
        tg_username = contact
    else:
        raise ValueError("contact missing")

    return name_part, tg_username, tg_user_id


def parse_uuid(value: str) -> UUID:
    return UUID(str(value).strip())


def create_lesson(db, tutor: Tutor, student: Student) -> Lesson:
    lesson = Lesson(
        id=str(uuid4()),
        tutor_id=tutor.id,
        student_id=student.id,
        token=secrets.token_urlsafe(24),
        status="created",
        started_at=datetime.utcnow(),
        sent_to_student=False,
    )
    db.add(lesson)
    db.commit()
    db.refresh(lesson)
    return lesson


def parse_draft_edit(text: str) -> dict[str, str] | None:
    parts = [part.strip() for part in text.split("||")]
    if len(parts) == 3 and all(parts):
        return {
            "summary": parts[0],
            "difficulties": parts[1],
            "homework": parts[2],
        }

    values: dict[str, str] = {}
    for line in [line.strip() for line in text.splitlines() if line.strip()]:
        lower = line.lower()
        if lower.startswith("саммари:"):
            values["summary"] = line.split(":", 1)[1].strip()
        elif lower.startswith("трудности:"):
            values["difficulties"] = line.split(":", 1)[1].strip()
        elif lower.startswith("домашка:"):
            values["homework"] = line.split(":", 1)[1].strip()

    if {"summary", "difficulties", "homework"}.issubset(values):
        return values

    return None


def draft_markup(lesson_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Отправить ученику",
                    callback_data=f"{SEND_STUDENT_PREFIX}{lesson_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Исправить",
                    callback_data=f"{EDIT_DRAFT_PREFIX}{lesson_id}",
                ),
                InlineKeyboardButton(
                    text="🔁 Заново",
                    callback_data=f"{REGEN_DRAFT_PREFIX}{lesson_id}",
                ),
            ],
        ]
    )


def ensure_tutor_student_link(db, tutor_id: UUID, student_id: UUID) -> bool:
    link = (
        db.query(TutorStudent)
        .filter(
            TutorStudent.tutor_id == tutor_id,
            TutorStudent.student_id == student_id,
        )
        .first()
    )
    if link:
        if not link.is_active:
            link.is_active = True
        return False

    db.add(
        TutorStudent(
            tutor_id=tutor_id,
            student_id=student_id,
            is_active=True,
        )
    )
    return True


def derive_student_name(user: User) -> str:
    if user.full_name:
        return user.full_name
    if user.username:
        return user.username
    return f"student-{user.id}"


def claim_invite(db, token: str, user: User) -> tuple[bool, str]:
    invite = db.query(Invite).filter(Invite.token == token).with_for_update().first()
    if not invite:
        return False, "Инвайт не найден. Проверьте ссылку."

    now = datetime.utcnow()
    if invite.used_at is not None:
        return False, "Этот инвайт уже использован."

    if invite.expires_at < now:
        return False, "Срок действия инвайта истек."

    student: Student | None = None
    if invite.student_id is not None:
        student = db.query(Student).filter(Student.id == invite.student_id).first()

    if student is None:
        student = db.query(Student).filter(Student.tg_user_id == user.id).first()

    if student is None:
        student = Student(
            name=derive_student_name(user),
            tg_user_id=user.id,
            tg_username=user.username,
        )
        db.add(student)
        db.flush()
    else:
        if student.tg_user_id is None:
            student.tg_user_id = user.id
        if user.username:
            student.tg_username = user.username
        if not student.name:
            student.name = derive_student_name(user)

    ensure_tutor_student_link(db, invite.tutor_id, student.id)
    invite.student_id = student.id
    invite.used_at = now

    db.commit()

    return True, "Готово: вас привязали к преподавателю. Теперь можно продолжать в боте."


async def send_draft_preview(chat_id: int, lesson: Lesson, student_name: str, bot: Bot) -> None:
    text = build_draft_text(
        lesson_id=lesson.id,
        student_name=student_name,
        summary=lesson.draft_summary or "",
        difficulties=lesson.draft_difficulties or "",
        homework=lesson.draft_homework or "",
    )
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=draft_markup(lesson.id))


def format_start_link(lesson_id: str, token: str) -> str:
    return f"{settings.base_url}/lesson/{lesson_id}?token={token}"


def require_message_user(message: Message) -> User:
    if not message.from_user:
        raise RuntimeError("Message has no sender")
    return message.from_user


def require_callback_user(query: CallbackQuery) -> User:
    if not query.from_user:
        raise RuntimeError("Callback has no sender")
    return query.from_user


def parse_start_payload(text: str | None) -> str:
    parts = (text or "").split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def handle_start(message: Message) -> None:
        user = require_message_user(message)
        payload = parse_start_payload(message.text)

        if payload.startswith("invite_"):
            token = payload.removeprefix("invite_").strip()
            if not token:
                await message.answer("Пустой токен инвайта. Проверьте ссылку.")
                return

            with SessionLocal() as db:
                ok, response_text = claim_invite(db, token=token, user=user)

            await message.answer(response_text)
            return

        with SessionLocal() as db:
            ensure_tutor(
                db,
                tg_user_id=user.id,
                tg_username=user.username,
                full_name=user.full_name,
            )

        await message.answer(
            "Привет. Я бот-ассистент репетитора.\n\n"
            "Команды:\n"
            "/add_student <имя> | <@username или telegram_id>\n"
            "/lesson_now [student_uuid]\n"
            "/create_invite [student_uuid]"
        )

    @dp.message(Command("add_student"))
    async def handle_add_student(message: Message) -> None:
        user = require_message_user(message)
        payload = (message.text or "").split(maxsplit=1)
        if len(payload) < 2:
            await message.answer(
                "Использование: /add_student Иван Петров | @student_username\n"
                "или /add_student Иван Петров 123456789"
            )
            return

        try:
            name, tg_username, tg_user_id = parse_student_command_payload(payload[1].strip())
        except ValueError:
            await message.answer(
                "Не удалось разобрать команду.\n"
                "Формат: /add_student Иван Петров | @student_username"
            )
            return

        with SessionLocal() as db:
            tutor = ensure_tutor(
                db,
                tg_user_id=user.id,
                tg_username=user.username,
                full_name=user.full_name,
            )

            student: Student | None = None
            if tg_user_id is not None:
                student = db.query(Student).filter(Student.tg_user_id == tg_user_id).first()

            if student is None:
                student = Student(
                    name=name,
                    tg_username=tg_username,
                    tg_user_id=tg_user_id,
                )
                db.add(student)
                db.flush()
            else:
                student.name = name
                if tg_username is not None:
                    student.tg_username = tg_username

            was_created = ensure_tutor_student_link(db, tutor.id, student.id)
            db.commit()
            db.refresh(student)

        contact = f"@{tg_username}" if tg_username else str(tg_user_id)
        link_info = "новая связь" if was_created else "связь уже была"
        await message.answer(
            f"Ученик доступен: {student.name} ({contact})\n"
            f"UUID: {student.id}\n"
            f"Статус: {link_info}."
        )

    @dp.message(Command("create_invite"))
    async def handle_create_invite(message: Message) -> None:
        user = require_message_user(message)
        payload = (message.text or "").split(maxsplit=1)

        with SessionLocal() as db:
            tutor = ensure_tutor(
                db,
                tg_user_id=user.id,
                tg_username=user.username,
                full_name=user.full_name,
            )

            student_id: UUID | None = None
            if len(payload) > 1 and payload[1].strip():
                try:
                    student_id = parse_uuid(payload[1].strip())
                except ValueError:
                    await message.answer("student_uuid должен быть в формате UUID.")
                    return

                student_exists = (
                    db.query(TutorStudent)
                    .filter(
                        TutorStudent.tutor_id == tutor.id,
                        TutorStudent.student_id == student_id,
                        TutorStudent.is_active.is_(True),
                    )
                    .first()
                )
                if not student_exists:
                    await message.answer("Этот student_uuid не привязан к вам.")
                    return

            invite = Invite(
                token=secrets.token_urlsafe(24),
                tutor_id=tutor.id,
                student_id=student_id,
                expires_at=datetime.utcnow() + timedelta(days=7),
            )
            db.add(invite)
            db.commit()
            db.refresh(invite)

        await message.answer(
            "Инвайт создан (срок 7 дней).\n"
            "Передайте ученику команду:\n"
            f"/start invite_{invite.token}"
        )

    async def send_lesson_link(tutor_user_id: int, student_id: UUID, bot: Bot) -> None:
        with SessionLocal() as db:
            tutor = db.query(Tutor).filter(Tutor.tg_user_id == tutor_user_id).first()
            if not tutor:
                await bot.send_message(tutor_user_id, "Сначала выполните /start")
                return

            student = (
                db.query(Student)
                .join(TutorStudent, TutorStudent.student_id == Student.id)
                .filter(
                    Student.id == student_id,
                    TutorStudent.tutor_id == tutor.id,
                    TutorStudent.is_active.is_(True),
                )
                .first()
            )
            if not student:
                await bot.send_message(tutor_user_id, "Ученик не найден")
                return

            lesson = create_lesson(db, tutor=tutor, student=student)
            student_name = student.name

        start_url = format_start_link(lesson.id, lesson.token)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Начать урок", url=start_url)]]
        )
        await bot.send_message(
            chat_id=tutor_user_id,
            text=(
                f"Урок создан для ученика {student_name}.\n"
                f"ID урока: {lesson.id}\n"
                "Откройте страницу урока по кнопке ниже."
            ),
            reply_markup=markup,
        )

    @dp.message(Command("lesson_now"))
    async def handle_lesson_now(message: Message, bot: Bot) -> None:
        user = require_message_user(message)

        payload = (message.text or "").split(maxsplit=1)
        if len(payload) > 1 and payload[1].strip():
            try:
                student_id = parse_uuid(payload[1].strip())
            except ValueError:
                await message.answer("student_uuid должен быть в формате UUID.")
                return

            await send_lesson_link(user.id, student_id, bot)
            return

        with SessionLocal() as db:
            tutor = ensure_tutor(
                db,
                tg_user_id=user.id,
                tg_username=user.username,
                full_name=user.full_name,
            )
            students = (
                db.query(Student)
                .join(TutorStudent, TutorStudent.student_id == Student.id)
                .filter(
                    TutorStudent.tutor_id == tutor.id,
                    TutorStudent.is_active.is_(True),
                )
                .all()
            )

        if not students:
            await message.answer("Нет учеников. Сначала добавьте: /add_student")
            return

        keyboard_rows = [
            [
                InlineKeyboardButton(
                    text=f"{student.name} ({str(student.id)[:8]})",
                    callback_data=f"{LESSON_PICK_PREFIX}{student.id}",
                )
            ]
            for student in students
        ]
        await message.answer(
            "Выберите ученика для урока прямо сейчас:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )

    @dp.callback_query(F.data.startswith(LESSON_PICK_PREFIX))
    async def handle_pick_student(query: CallbackQuery, bot: Bot) -> None:
        user = require_callback_user(query)
        student_id_text = (query.data or "").replace(LESSON_PICK_PREFIX, "", 1)

        try:
            student_id = parse_uuid(student_id_text)
        except ValueError:
            await query.answer("Некорректный ID", show_alert=True)
            return

        await query.answer()
        await send_lesson_link(user.id, student_id, bot)

    @dp.callback_query(F.data.startswith(SEND_STUDENT_PREFIX))
    async def handle_send_student(query: CallbackQuery, bot: Bot) -> None:
        user = require_callback_user(query)
        lesson_id = (query.data or "").replace(SEND_STUDENT_PREFIX, "", 1)

        with SessionLocal() as db:
            lesson = (
                db.query(Lesson)
                .join(Tutor, Lesson.tutor_id == Tutor.id)
                .filter(Lesson.id == lesson_id, Tutor.tg_user_id == user.id)
                .first()
            )
            if not lesson:
                await query.answer("Урок не найден", show_alert=True)
                return

            student = lesson.student
            if not student:
                await query.answer("Ученик не найден", show_alert=True)
                return

            target_chat: int | str | None = None
            if student.tg_user_id:
                target_chat = student.tg_user_id
            elif student.tg_username:
                target_chat = f"@{student.tg_username}"

            if target_chat is None:
                await query.answer("Нет telegram username/ID у ученика", show_alert=True)
                return

            updated = db.execute(
                update(Lesson)
                .where(Lesson.id == lesson.id, Lesson.sent_to_student.is_(False))
                .values(sent_to_student=True)
            )
            db.commit()

            if (updated.rowcount or 0) == 0:
                await query.answer("Уже отправлено ученику", show_alert=True)
                return

            student_text = build_student_text(
                student_name=student.name,
                summary=lesson.draft_summary or "",
                difficulties=lesson.draft_difficulties or "",
                homework=lesson.draft_homework or "",
            )

        try:
            await bot.send_message(chat_id=target_chat, text=student_text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to send to student for lesson %s", lesson_id)
            await query.answer("Ошибка при отправке, проверьте вручную", show_alert=True)
            await bot.send_message(
                chat_id=user.id,
                text=(
                    "Отправка ученику завершилась ошибкой после фиксации статуса, "
                    "повторную автоматическую отправку блокирую от дублей."
                ),
            )
            return

        await query.answer("Отправлено")
        await bot.send_message(chat_id=user.id, text="Черновик подтвержден и отправлен ученику.")

    @dp.callback_query(F.data.startswith(EDIT_DRAFT_PREFIX))
    async def handle_edit_draft(query: CallbackQuery) -> None:
        user = require_callback_user(query)
        lesson_id = (query.data or "").replace(EDIT_DRAFT_PREFIX, "", 1)

        with SessionLocal() as db:
            lesson = (
                db.query(Lesson)
                .join(Tutor, Lesson.tutor_id == Tutor.id)
                .filter(Lesson.id == lesson_id, Tutor.tg_user_id == user.id)
                .first()
            )

        if not lesson:
            await query.answer("Урок не найден", show_alert=True)
            return

        target_message = query.message
        await query.answer()
        if not target_message:
            return

        pending_edits[user.id] = lesson_id

        await target_message.answer(
            "Пришлите новый черновик в формате:\n"
            "саммари || трудности || домашка\n\n"
            "Или 3 строки:\n"
            "Саммари: ...\nТрудности: ...\nДомашка: ..."
        )

    @dp.callback_query(F.data.startswith(REGEN_DRAFT_PREFIX))
    async def handle_regen_draft(query: CallbackQuery, bot: Bot) -> None:
        user = require_callback_user(query)
        lesson_id = (query.data or "").replace(REGEN_DRAFT_PREFIX, "", 1)

        with SessionLocal() as db:
            lesson = (
                db.query(Lesson)
                .join(Tutor, Lesson.tutor_id == Tutor.id)
                .filter(Lesson.id == lesson_id, Tutor.tg_user_id == user.id)
                .first()
            )
            if not lesson:
                await query.answer("Урок не найден", show_alert=True)
                return

            draft = generate_draft(lesson.transcript_text or "transcript placeholder", settings.llm_provider)
            lesson.draft_summary = draft["summary"]
            lesson.draft_difficulties = draft["difficulties"]
            lesson.draft_homework = draft["homework"]
            lesson.sent_to_student = False
            student_name = lesson.student.name if lesson.student else "ученик"
            db.commit()
            db.refresh(lesson)

        await query.answer("Черновик обновлен")
        text = build_draft_text(
            lesson_id=lesson.id,
            student_name=student_name,
            summary=lesson.draft_summary or "",
            difficulties=lesson.draft_difficulties or "",
            homework=lesson.draft_homework or "",
        )

        if query.message:
            await query.message.edit_text(text=text, reply_markup=draft_markup(lesson.id))
        else:
            await send_draft_preview(chat_id=user.id, lesson=lesson, student_name=student_name, bot=bot)

    @dp.message(F.text)
    async def handle_pending_edit(message: Message, bot: Bot) -> None:
        user = require_message_user(message)
        lesson_id = pending_edits.get(user.id)
        if not lesson_id:
            return

        text = (message.text or "").strip()
        if text.startswith("/"):
            return

        parsed = parse_draft_edit(text)
        if not parsed:
            await message.answer(
                "Не удалось разобрать текст.\n"
                "Формат: саммари || трудности || домашка"
            )
            return

        with SessionLocal() as db:
            lesson = (
                db.query(Lesson)
                .join(Tutor, Lesson.tutor_id == Tutor.id)
                .filter(Lesson.id == lesson_id, Tutor.tg_user_id == user.id)
                .first()
            )
            if not lesson:
                pending_edits.pop(user.id, None)
                await message.answer("Урок не найден")
                return

            lesson.draft_summary = parsed["summary"]
            lesson.draft_difficulties = parsed["difficulties"]
            lesson.draft_homework = parsed["homework"]
            lesson.sent_to_student = False
            student_name = lesson.student.name if lesson.student else "ученик"
            db.commit()
            db.refresh(lesson)

        pending_edits.pop(user.id, None)
        await message.answer("Черновик сохранен.")
        await send_draft_preview(chat_id=user.id, lesson=lesson, student_name=student_name, bot=bot)

    return dp


_webhook_bot = None
_webhook_dp = None
_webhook_lock = asyncio.Lock()
_webhook_ready = False


async def _ensure_webhook_runtime():
    global _webhook_bot, _webhook_dp, _webhook_ready

    if _webhook_ready:
        return

    async with _webhook_lock:
        if _webhook_ready:
            return

        if not settings.bot_token:
            raise RuntimeError("BOT_TOKEN is required for bot service")

        init_db()

        _webhook_bot = Bot(token=settings.bot_token)
        _webhook_dp = build_dispatcher()

        _webhook_ready = True


async def process_update(update_dict: dict) -> None:
    await _ensure_webhook_runtime()

    update = Update.model_validate(update_dict)
    await _webhook_dp.feed_update(_webhook_bot, update)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required for bot service")

    init_db()

    bot = Bot(token=settings.bot_token)
    dp = build_dispatcher()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
