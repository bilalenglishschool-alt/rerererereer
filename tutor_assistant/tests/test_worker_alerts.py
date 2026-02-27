from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from tutor_assistant.backend import app
from tutor_assistant.queue import (
    LESSON_DEAD_LETTER_QUEUE_NAME,
    LESSON_PROCESSING_QUEUE_NAME,
    LESSON_QUEUE_NAME,
    TASK_GENERATE_ARTIFACTS,
    TASK_PROCESS_AUDIO,
    TASK_TRANSCRIBE_JOB,
    build_task_payload,
)


class _AlertRedisStub:
    def __init__(
        self,
        *,
        processed_total: int = 0,
        failed_total: int = 0,
        failures_last_10m: int = 0,
        dead_letter_requeued_last_10m: int = 0,
        queue_depth: int = 0,
        processing_depth: int = 0,
        dead_letter_depth: int = 0,
        heartbeat_ts: int = 0,
        queue_items: dict[str, list[str]] | None = None,
    ) -> None:
        self._values = {
            "lesson_metrics:tasks_processed_total": processed_total,
            "lesson_metrics:task_failures_total": failed_total,
            "lesson_metrics:worker_failures": failures_last_10m,
            "lesson_metrics:dead_letter_requeued_events": dead_letter_requeued_last_10m,
            "lesson_metrics:worker_heartbeat_ts": heartbeat_ts,
            "lesson_tasks": queue_depth,
            "lesson_tasks:processing": processing_depth,
            "lesson_tasks:dead": dead_letter_depth,
        }
        self._queue_items = queue_items or {}
        self.closed = False

    def get(self, key: str):
        return self._values.get(key, 0)

    def zcount(self, key: str, min_score: int, max_score: int):  # noqa: ARG002
        return self._values.get(key, 0)

    def llen(self, key: str):
        return self._values.get(key, 0)

    def lrange(self, key: str, start: int, end: int):  # noqa: ARG002
        return list(self._queue_items.get(key, []))

    def close(self) -> None:
        self.closed = True


class WorkerAlertsEndpointTest(unittest.TestCase):
    def _run_with_settings(
        self,
        errors_threshold: int,
        dead_letter_requeued_last_10m_threshold: int,
        dead_threshold: int,
        queue_depth_threshold: int,
        transcribe_queue_depth_threshold: int,
        transcribe_oldest_queue_age_seconds_threshold: int,
        transcribe_oldest_processing_age_seconds_threshold: int,
        transcribe_dead_letter_threshold: int,
        transcribe_oldest_dead_letter_age_seconds_threshold: int,
        heartbeat_age_threshold: int,
    ):
        return patch(
            "tutor_assistant.backend.settings",
            SimpleNamespace(
                worker_alert_errors_last_10m_threshold=errors_threshold,
                worker_alert_dead_letter_requeued_last_10m_threshold=(
                    dead_letter_requeued_last_10m_threshold
                ),
                worker_alert_dead_letter_threshold=dead_threshold,
                worker_alert_queue_depth_threshold=queue_depth_threshold,
                worker_alert_transcribe_queue_depth_threshold=transcribe_queue_depth_threshold,
                worker_alert_transcribe_oldest_queue_age_seconds_threshold=(
                    transcribe_oldest_queue_age_seconds_threshold
                ),
                worker_alert_transcribe_oldest_processing_age_seconds_threshold=(
                    transcribe_oldest_processing_age_seconds_threshold
                ),
                worker_alert_transcribe_dead_letter_threshold=transcribe_dead_letter_threshold,
                worker_alert_transcribe_oldest_dead_letter_age_seconds_threshold=(
                    transcribe_oldest_dead_letter_age_seconds_threshold
                ),
                worker_alert_heartbeat_age_seconds_threshold=heartbeat_age_threshold,
            ),
        )

    def test_alerts_ok_within_thresholds(self) -> None:
        redis_stub = _AlertRedisStub(
            processed_total=15,
            failed_total=1,
            failures_last_10m=2,
            dead_letter_requeued_last_10m=1,
            queue_depth=2,
            dead_letter_depth=1,
            heartbeat_ts=1995,
            queue_items={
                LESSON_QUEUE_NAME: [
                    build_task_payload("lesson-1", TASK_TRANSCRIBE_JOB, enqueued_at=1990000),
                    build_task_payload("lesson-2", TASK_GENERATE_ARTIFACTS),
                ],
                LESSON_PROCESSING_QUEUE_NAME: [],
                LESSON_DEAD_LETTER_QUEUE_NAME: [
                    json.dumps(
                        {
                            "raw_task": build_task_payload("lesson-3", TASK_TRANSCRIBE_JOB),
                            "reason": "x",
                            "task_type": TASK_TRANSCRIBE_JOB,
                            "lesson_id": "lesson-3",
                            "failed_at": "1970-01-01T00:33:15+00:00",
                        }
                    )
                ],
            },
        )
        with self._run_with_settings(
            errors_threshold=2,
            dead_letter_requeued_last_10m_threshold=1,
            dead_threshold=1,
            queue_depth_threshold=2,
            transcribe_queue_depth_threshold=2,
            transcribe_oldest_queue_age_seconds_threshold=20,
            transcribe_oldest_processing_age_seconds_threshold=20,
            transcribe_dead_letter_threshold=1,
            transcribe_oldest_dead_letter_age_seconds_threshold=10,
            heartbeat_age_threshold=10,
        ):
            with patch("tutor_assistant.backend.time.time", return_value=2000):
                with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
                    with TestClient(app) as client:
                        response = client.get("/alerts/worker")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["alerts"], [])
        self.assertEqual(payload["metrics"]["worker_errors_last_10m"], 2)
        self.assertEqual(payload["metrics"]["dead_letter_requeued_last_10m"], 1)
        self.assertEqual(payload["metrics"]["dead_letter_depth"], 1)
        self.assertEqual(payload["metrics"]["worker_heartbeat_age_seconds"], 5)
        self.assertEqual(payload["metrics"]["transcribe_oldest_queue_age_seconds"], 10)
        self.assertEqual(payload["metrics"]["transcribe_oldest_processing_age_seconds"], -1)
        self.assertEqual(payload["metrics"]["transcribe_dead_letter_depth"], 1)
        self.assertEqual(payload["metrics"]["transcribe_oldest_dead_letter_age_seconds"], 5)
        self.assertTrue(redis_stub.closed)

    def test_alerts_trigger_when_thresholds_exceeded(self) -> None:
        redis_stub = _AlertRedisStub(
            processed_total=100,
            failed_total=7,
            failures_last_10m=3,
            dead_letter_requeued_last_10m=5,
            queue_depth=5,
            dead_letter_depth=2,
            heartbeat_ts=1900,
            queue_items={
                LESSON_QUEUE_NAME: [
                    build_task_payload("lesson-1", TASK_TRANSCRIBE_JOB, enqueued_at=1980000),
                    build_task_payload("lesson-2", TASK_TRANSCRIBE_JOB, enqueued_at=1990000),
                    build_task_payload("lesson-3", TASK_TRANSCRIBE_JOB, enqueued_at=1995000),
                    build_task_payload("lesson-4", TASK_PROCESS_AUDIO),
                    build_task_payload("lesson-5", TASK_GENERATE_ARTIFACTS),
                ],
                LESSON_PROCESSING_QUEUE_NAME: [
                    build_task_payload("lesson-6", TASK_TRANSCRIBE_JOB, enqueued_at=1987000),
                ],
                LESSON_DEAD_LETTER_QUEUE_NAME: [
                    json.dumps(
                        {
                            "raw_task": build_task_payload("lesson-7", TASK_TRANSCRIBE_JOB),
                            "reason": "x",
                            "task_type": TASK_TRANSCRIBE_JOB,
                            "lesson_id": "lesson-7",
                            "failed_at": "1970-01-01T00:33:00+00:00",
                        }
                    ),
                    json.dumps(
                        {
                            "raw_task": build_task_payload("lesson-8", TASK_PROCESS_AUDIO),
                            "reason": "y",
                            "task_type": TASK_PROCESS_AUDIO,
                            "lesson_id": "lesson-8",
                            "failed_at": "1970-01-01T00:33:10+00:00",
                        }
                    ),
                ],
            },
        )
        with self._run_with_settings(
            errors_threshold=1,
            dead_letter_requeued_last_10m_threshold=1,
            dead_threshold=0,
            queue_depth_threshold=1,
            transcribe_queue_depth_threshold=1,
            transcribe_oldest_queue_age_seconds_threshold=5,
            transcribe_oldest_processing_age_seconds_threshold=5,
            transcribe_dead_letter_threshold=0,
            transcribe_oldest_dead_letter_age_seconds_threshold=5,
            heartbeat_age_threshold=30,
        ):
            with patch("tutor_assistant.backend.time.time", return_value=2000):
                with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
                    with TestClient(app) as client:
                        response = client.get("/alerts/worker")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "alert")
        self.assertGreaterEqual(len(payload["alerts"]), 10)
        self.assertIn("worker_errors_last_10m exceeded threshold", payload["alerts"][0])
        self.assertIn("dead_letter_requeued_last_10m exceeded threshold", " ".join(payload["alerts"]))
        self.assertIn("queue_depth", " ".join(payload["alerts"]))
        self.assertIn("transcribe_queue_depth exceeded threshold", " ".join(payload["alerts"]))
        self.assertIn(
            "transcribe_oldest_queue_age_seconds exceeded threshold",
            " ".join(payload["alerts"]),
        )
        self.assertIn(
            "transcribe_oldest_processing_age_seconds exceeded threshold",
            " ".join(payload["alerts"]),
        )
        self.assertIn("transcribe_dead_letter_depth exceeded threshold", " ".join(payload["alerts"]))
        self.assertIn(
            "transcribe_oldest_dead_letter_age_seconds exceeded threshold",
            " ".join(payload["alerts"]),
        )
        self.assertIn("worker_heartbeat_age_seconds exceeded threshold", " ".join(payload["alerts"]))
        self.assertEqual(payload["thresholds"]["queue_depth"], 1)
        self.assertEqual(payload["thresholds"]["dead_letter_requeued_last_10m"], 1)
        self.assertEqual(payload["thresholds"]["transcribe_queue_depth"], 1)
        self.assertEqual(payload["thresholds"]["transcribe_oldest_queue_age_seconds"], 5)
        self.assertEqual(payload["thresholds"]["transcribe_oldest_processing_age_seconds"], 5)
        self.assertEqual(payload["thresholds"]["transcribe_dead_letter_depth"], 0)
        self.assertEqual(payload["thresholds"]["transcribe_oldest_dead_letter_age_seconds"], 5)
        self.assertEqual(payload["thresholds"]["worker_heartbeat_age_seconds"], 30)
        self.assertTrue(redis_stub.closed)

    def test_alerts_trigger_when_heartbeat_missing(self) -> None:
        redis_stub = _AlertRedisStub(
            processed_total=1,
            failed_total=0,
            failures_last_10m=0,
            dead_letter_requeued_last_10m=0,
            queue_depth=0,
            dead_letter_depth=0,
            heartbeat_ts=0,
        )
        with self._run_with_settings(
            errors_threshold=10,
            dead_letter_requeued_last_10m_threshold=10,
            dead_threshold=10,
            queue_depth_threshold=10,
            transcribe_queue_depth_threshold=10,
            transcribe_oldest_queue_age_seconds_threshold=10,
            transcribe_oldest_processing_age_seconds_threshold=10,
            transcribe_dead_letter_threshold=10,
            transcribe_oldest_dead_letter_age_seconds_threshold=10,
            heartbeat_age_threshold=60,
        ):
            with patch("tutor_assistant.backend.time.time", return_value=2000):
                with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
                    with TestClient(app) as client:
                        response = client.get("/alerts/worker")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "alert")
        self.assertIn("worker heartbeat missing", payload["alerts"])
        self.assertEqual(payload["metrics"]["worker_heartbeat_age_seconds"], -1)
        self.assertTrue(redis_stub.closed)

    def test_alerts_returns_503_when_redis_unavailable(self) -> None:
        with self._run_with_settings(
            errors_threshold=0,
            dead_letter_requeued_last_10m_threshold=0,
            dead_threshold=0,
            queue_depth_threshold=0,
            transcribe_queue_depth_threshold=0,
            transcribe_oldest_queue_age_seconds_threshold=0,
            transcribe_oldest_processing_age_seconds_threshold=0,
            transcribe_dead_letter_threshold=0,
            transcribe_oldest_dead_letter_age_seconds_threshold=0,
            heartbeat_age_threshold=0,
        ):
            with patch("tutor_assistant.backend.get_redis_client", side_effect=RedisError("down")):
                with TestClient(app) as client:
                    response = client.get("/alerts/worker")

        self.assertEqual(response.status_code, 503)
        self.assertIn("Failed to read worker metrics", response.json().get("detail", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
