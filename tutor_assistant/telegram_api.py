from __future__ import annotations

import logging

import httpx

from .telegram_texts import build_draft_reply_markup, build_draft_text

logger = logging.getLogger(__name__)


async def send_message(
    bot_token: str,
    chat_id: int | str,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is empty")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, json=payload)

    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")


async def send_draft_to_tutor(
    bot_token: str,
    tutor_chat_id: int,
    lesson_id: str,
    student_name: str,
    summary: str,
    difficulties: str,
    homework: str,
) -> None:
    text = build_draft_text(
        lesson_id=lesson_id,
        student_name=student_name,
        summary=summary,
        difficulties=difficulties,
        homework=homework,
    )
    await send_message(
        bot_token=bot_token,
        chat_id=tutor_chat_id,
        text=text,
        reply_markup=build_draft_reply_markup(lesson_id),
    )
