from __future__ import annotations

import json
import os
from typing import Any


def _fallback(transcript: str) -> dict[str, object]:
    return {
        "summary": "Краткое саммари урока (fallback).",
        "difficulties": ["Не удалось автоматически извлечь трудности."],
        "homework": ["Повторить материал урока и подготовить 3 примера."],
        "next_topic": "",
        "key_points": [],
        "transcript_preview": (transcript or "")[:300],
    }


def _extract_json_payload(content: str) -> str:
    raw = (content or "").strip()
    if not raw:
        return ""

    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw[start : end + 1]

    return raw


def _as_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            text = _as_string(item)
            if text:
                out.append(text)
        return out
    return []


def generate_lesson_draft(transcript: str) -> dict[str, object]:
    provider = (os.getenv("LLM_PROVIDER") or "none").strip().lower()
    model = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    api_key = ((os.getenv("LLM_API_KEY") or "").strip() or (os.getenv("OPENAI_API_KEY") or "").strip())

    if provider != "openai" or not api_key:
        return _fallback(transcript)

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)

        system = (
            "Ты помощник репетитора. "
            "Верни строго JSON без markdown и без пояснений."
        )
        user = (
            "Сделай структурированное саммари по транскрипту урока.\n"
            "Верни JSON вида:\n"
            '{"summary":"...","difficulties":["..."],"homework":["..."],"next_topic":"...","key_points":["..."]}\n\n'
            "Транскрипт:\n"
            + (transcript or "")[:12000]
        )

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )

        content = _extract_json_payload(resp.choices[0].message.content or "")
        data = json.loads(content)

        summary = _as_string(data.get("summary"))
        difficulties = _as_list(data.get("difficulties"))
        homework = _as_list(data.get("homework"))
        next_topic = _as_string(data.get("next_topic"))
        key_points = _as_list(data.get("key_points"))

        if not summary:
            return _fallback(transcript)

        return {
            "summary": summary,
            "difficulties": difficulties,
            "homework": homework,
            "next_topic": next_topic,
            "key_points": key_points,
            "transcript_preview": (transcript or "")[:300],
        }
    except Exception:
        return _fallback(transcript)
