import json
import os

def _fallback(transcript: str) -> dict:
    # простая заглушка, если LLM выключен или ошибка
    return {
        "summary": "Краткое саммари урока (заглушка).",
        "difficulties": [],
        "homework": [],
        "next_topic": "",
        "key_points": [],
        "transcript_preview": (transcript or "")[:300],
    }

def generate_lesson_draft(transcript: str) -> dict:
    provider = (os.getenv("LLM_PROVIDER") or "none").strip().lower()
    model = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()

    if provider != "openai" or not api_key:
        return _fallback(transcript)

    try:
        # OpenAI python SDK (v1+)
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        system = "Ты помощник репетитора. Верни СТРОГО JSON."
        user = (
            "Сделай структурированное саммари по транскрипту урока.\n"
            "Верни строго JSON формата:\n"
            '{ "summary": "...", "difficulties": ["..."], "homework": ["..."], "next_topic": "...", "key_points": ["..."] }\n\n'
            "Транскрипт:\n"
            + (transcript or "")
        )

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )

        content = (resp.choices[0].message.content or "").strip()

        # строгий JSON
        data = json.loads(content)

        # нормализация/страховка полей
        out = {
            "summary": data.get("summary", ""),
            "difficulties": data.get("difficulties", []) or [],
            "homework": data.get("homework", []) or [],
            "next_topic": data.get("next_topic", ""),
            "key_points": data.get("key_points", []) or [],
            "transcript_preview": (transcript or "")[:300],
        }
        return out
    except Exception:
        return _fallback(transcript)
