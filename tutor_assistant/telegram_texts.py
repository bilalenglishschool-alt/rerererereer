from __future__ import annotations

SEND_STUDENT_PREFIX = "send_student:"
EDIT_DRAFT_PREFIX = "edit_draft:"
REGEN_DRAFT_PREFIX = "regen_draft:"


def build_draft_text(
    lesson_id: str,
    student_name: str,
    summary: str,
    difficulties: str,
    homework: str,
) -> str:
    return (
        f"Черновик урока #{lesson_id} для {student_name}\n\n"
        f"Саммари:\n{summary}\n\n"
        f"Трудности:\n{difficulties}\n\n"
        f"Домашка:\n{homework}"
    )


def build_student_text(
    student_name: str,
    summary: str,
    difficulties: str,
    homework: str,
) -> str:
    return (
        f"Итоги урока для {student_name}:\n\n"
        f"Что сделали:\n{summary}\n\n"
        f"Что было сложно:\n{difficulties}\n\n"
        f"Домашнее задание:\n{homework}"
    )


def build_draft_reply_markup(lesson_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Отправить ученику",
                    "callback_data": f"{SEND_STUDENT_PREFIX}{lesson_id}",
                }
            ],
            [
                {
                    "text": "✏️ Исправить",
                    "callback_data": f"{EDIT_DRAFT_PREFIX}{lesson_id}",
                },
                {
                    "text": "🔁 Заново",
                    "callback_data": f"{REGEN_DRAFT_PREFIX}{lesson_id}",
                },
            ],
        ]
    }
