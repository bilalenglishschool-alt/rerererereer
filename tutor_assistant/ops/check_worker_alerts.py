from __future__ import annotations

import os
from typing import Any

import requests


def evaluate_alert_payload(payload: dict[str, Any]) -> tuple[int, str]:
    status = str(payload.get("status", "")).strip().lower()
    alerts = payload.get("alerts")

    if status == "ok":
        return 0, "Worker alerts status=ok"

    if status == "alert":
        alert_lines: list[str] = []
        if isinstance(alerts, list):
            alert_lines = [str(item) for item in alerts if str(item).strip()]
        if not alert_lines:
            alert_lines = ["unknown worker alert (alerts list is empty)"]
        message = "Worker alerts status=alert\n" + "\n".join(f"- {line}" for line in alert_lines)
        return 2, message

    return 1, f"Unexpected worker alert payload status: {status or '<empty>'}"


def check_worker_alerts(
    alert_url: str,
    timeout_seconds: int = 10,
    token: str = "",
) -> tuple[int, str]:
    headers = {"X-Ops-Token": token} if token else None
    try:
        response = requests.get(
            alert_url,
            timeout=max(1, int(timeout_seconds)),
            headers=headers,
        )
    except requests.RequestException as exc:
        return 1, f"Failed to call {alert_url}: {exc}"

    if response.status_code != 200:
        return 1, f"Non-200 response from {alert_url}: {response.status_code}"

    try:
        payload = response.json()
    except ValueError as exc:
        return 1, f"Invalid JSON from {alert_url}: {exc}"

    if not isinstance(payload, dict):
        return 1, f"Unexpected JSON type from {alert_url}: {type(payload).__name__}"

    return evaluate_alert_payload(payload)


def main() -> int:
    alert_url = os.getenv("WORKER_ALERT_URL", "").strip()
    if not alert_url:
        print("WORKER_ALERT_URL is not set")
        return 1

    timeout_seconds_raw = os.getenv("ALERT_TIMEOUT_SECONDS", "10").strip() or "10"
    try:
        timeout_seconds = int(timeout_seconds_raw)
    except ValueError:
        print(f"Invalid ALERT_TIMEOUT_SECONDS value: {timeout_seconds_raw}")
        return 1

    token = os.getenv("WORKER_ALERT_TOKEN", "").strip()
    code, message = check_worker_alerts(
        alert_url=alert_url,
        timeout_seconds=timeout_seconds,
        token=token,
    )
    print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
