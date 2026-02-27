from __future__ import annotations

import json
import unittest
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
    WORKER_FAILURE_EVENTS_ZSET_KEY,
    WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY,
    WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY,
    WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY,
    WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY,
    WORKER_METRIC_HEARTBEAT_TS_KEY,
    WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY,
    WORKER_METRIC_DEAD_LETTER_REQUEUED_EVENTS_ZSET_KEY,
    WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY,
    WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY,
    WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY,
    WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY,
    WORKER_METRIC_TASKS_FAILED_KEY,
    WORKER_METRIC_TASKS_PROCESSED_KEY,
    build_task_payload,
)


class _MetricsRedisStub:
    def __init__(
        self,
        *,
        values: dict[str, int] | None = None,
        queue_depth: int = 0,
        processing_depth: int = 0,
        dead_letter_depth: int = 0,
        queue_items: dict[str, list[str]] | None = None,
    ) -> None:
        self._values = values or {}
        self._depths = {
            LESSON_QUEUE_NAME: queue_depth,
            LESSON_PROCESSING_QUEUE_NAME: processing_depth,
            LESSON_DEAD_LETTER_QUEUE_NAME: dead_letter_depth,
        }
        self._queue_items = queue_items or {}
        self.closed = False

    def get(self, key: str):
        return self._values.get(key, 0)

    def zcount(self, key: str, min_score: int, max_score: int):  # noqa: ARG002
        return self._values.get(key, 0)

    def llen(self, key: str):
        return self._depths.get(key, 0)

    def lrange(self, key: str, start: int, end: int):  # noqa: ARG002
        return list(self._queue_items.get(key, []))

    def close(self) -> None:
        self.closed = True


class WorkerMetricsEndpointTest(unittest.TestCase):
    def test_metrics_endpoint_returns_latency_and_duration_fields(self) -> None:
        redis_stub = _MetricsRedisStub(
            values={
                WORKER_METRIC_TASKS_PROCESSED_KEY: 44,
                WORKER_METRIC_TASKS_FAILED_KEY: 3,
                WORKER_FAILURE_EVENTS_ZSET_KEY: 2,
                WORKER_METRIC_DEAD_LETTER_REQUEUED_EVENTS_ZSET_KEY: 4,
                WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY: 50,
                WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY: 120,
                WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY: 210,
                WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY: 3,
                WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY: 180,
                WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY: 250,
                WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY: 301,
                WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY: 2,
                WORKER_METRIC_HEARTBEAT_TS_KEY: 1995,
                WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY: 9,
                f"{WORKER_METRIC_TASKS_PROCESSED_KEY}:{TASK_PROCESS_AUDIO}": 11,
                f"{WORKER_METRIC_TASKS_PROCESSED_KEY}:{TASK_GENERATE_ARTIFACTS}": 22,
                f"{WORKER_METRIC_TASKS_PROCESSED_KEY}:{TASK_TRANSCRIBE_JOB}": 33,
                f"{WORKER_METRIC_TASKS_FAILED_KEY}:{TASK_PROCESS_AUDIO}": 1,
                f"{WORKER_METRIC_TASKS_FAILED_KEY}:{TASK_GENERATE_ARTIFACTS}": 2,
                f"{WORKER_METRIC_TASKS_FAILED_KEY}:{TASK_TRANSCRIBE_JOB}": 0,
                f"{WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY}:{TASK_PROCESS_AUDIO}": 3,
                f"{WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY}:{TASK_GENERATE_ARTIFACTS}": 2,
                f"{WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY}:{TASK_TRANSCRIBE_JOB}": 4,
                f"{WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY}:{TASK_PROCESS_AUDIO}": 45,
                f"{WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY}:{TASK_GENERATE_ARTIFACTS}": 60,
                f"{WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY}:{TASK_TRANSCRIBE_JOB}": 90,
                f"{WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY}:{TASK_PROCESS_AUDIO}": 110,
                f"{WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY}:{TASK_GENERATE_ARTIFACTS}": 130,
                f"{WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY}:{TASK_TRANSCRIBE_JOB}": 180,
                f"{WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY}:{TASK_PROCESS_AUDIO}": 200,
                f"{WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY}:{TASK_GENERATE_ARTIFACTS}": 240,
                f"{WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY}:{TASK_TRANSCRIBE_JOB}": 500,
                f"{WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY}:{TASK_PROCESS_AUDIO}": 4,
                f"{WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY}:{TASK_GENERATE_ARTIFACTS}": 3,
                f"{WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY}:{TASK_TRANSCRIBE_JOB}": 5,
                f"{WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY}:{TASK_PROCESS_AUDIO}": 170,
                f"{WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY}:{TASK_GENERATE_ARTIFACTS}": 220,
                f"{WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY}:{TASK_TRANSCRIBE_JOB}": 410,
                f"{WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY}:{TASK_PROCESS_AUDIO}": 260,
                f"{WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY}:{TASK_GENERATE_ARTIFACTS}": 290,
                f"{WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY}:{TASK_TRANSCRIBE_JOB}": 480,
                f"{WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY}:{TASK_PROCESS_AUDIO}": 600,
                f"{WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY}:{TASK_GENERATE_ARTIFACTS}": 500,
                f"{WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY}:{TASK_TRANSCRIBE_JOB}": 900,
                f"{WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY}:{TASK_PROCESS_AUDIO}": 4,
                f"{WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY}:{TASK_GENERATE_ARTIFACTS}": 2,
                f"{WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY}:{TASK_TRANSCRIBE_JOB}": 3,
            },
            queue_depth=4,
            processing_depth=1,
            dead_letter_depth=2,
            queue_items={
                LESSON_QUEUE_NAME: [
                    build_task_payload("lesson-1", TASK_TRANSCRIBE_JOB, enqueued_at=1986000),
                    build_task_payload("lesson-2", TASK_GENERATE_ARTIFACTS),
                    build_task_payload("lesson-3", TASK_PROCESS_AUDIO),
                    "legacy-lesson-4",
                ],
                LESSON_PROCESSING_QUEUE_NAME: [
                    build_task_payload("lesson-5", TASK_TRANSCRIBE_JOB, enqueued_at=1997000),
                    build_task_payload("lesson-6", TASK_GENERATE_ARTIFACTS),
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
                            "failed_at": "1970-01-01T00:33:15+00:00",
                        }
                    ),
                ],
            },
        )

        with patch("tutor_assistant.backend.time.time", return_value=2000):
            with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
                with TestClient(app) as client:
                    response = client.get("/metrics/worker")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["tasks_processed_total"], 44)
        self.assertEqual(payload["task_failures_total"], 3)
        self.assertEqual(payload["dead_letter_requeued_total"], 9)
        self.assertEqual(payload["dead_letter_requeued_last_10m"], 4)
        self.assertEqual(payload["worker_errors_last_10m"], 2)
        self.assertEqual(payload["queue_depth"], 4)
        self.assertEqual(payload["processing_depth"], 1)
        self.assertEqual(payload["dead_letter_depth"], 2)
        self.assertEqual(payload["queue_latency_ms_last"], 50)
        self.assertEqual(payload["queue_latency_ms_max"], 120)
        self.assertEqual(payload["queue_latency_ms_avg"], 70.0)
        self.assertEqual(payload["processing_duration_ms_last"], 180)
        self.assertEqual(payload["processing_duration_ms_max"], 250)
        self.assertEqual(payload["processing_duration_ms_avg"], 150.5)
        self.assertEqual(payload["worker_heartbeat_ts"], 1995)
        self.assertEqual(payload["worker_heartbeat_age_seconds"], 5)
        self.assertEqual(payload["transcribe_queue_depth"], 1)
        self.assertEqual(payload["transcribe_processing_depth"], 1)
        self.assertEqual(payload["transcribe_oldest_queue_age_seconds"], 14)
        self.assertEqual(payload["transcribe_oldest_processing_age_seconds"], 3)
        self.assertEqual(payload["transcribe_dead_letter_depth"], 1)
        self.assertEqual(payload["transcribe_oldest_dead_letter_age_seconds"], 20)
        self.assertEqual(
            payload["queue_latency_ms_last_by_type"],
            {
                TASK_PROCESS_AUDIO: 45,
                TASK_GENERATE_ARTIFACTS: 60,
                TASK_TRANSCRIBE_JOB: 90,
            },
        )
        self.assertEqual(
            payload["queue_latency_ms_max_by_type"],
            {
                TASK_PROCESS_AUDIO: 110,
                TASK_GENERATE_ARTIFACTS: 130,
                TASK_TRANSCRIBE_JOB: 180,
            },
        )
        self.assertEqual(
            payload["queue_latency_ms_avg_by_type"],
            {
                TASK_PROCESS_AUDIO: 50.0,
                TASK_GENERATE_ARTIFACTS: 80.0,
                TASK_TRANSCRIBE_JOB: 100.0,
            },
        )
        self.assertEqual(
            payload["processing_duration_ms_last_by_type"],
            {
                TASK_PROCESS_AUDIO: 170,
                TASK_GENERATE_ARTIFACTS: 220,
                TASK_TRANSCRIBE_JOB: 410,
            },
        )
        self.assertEqual(
            payload["processing_duration_ms_max_by_type"],
            {
                TASK_PROCESS_AUDIO: 260,
                TASK_GENERATE_ARTIFACTS: 290,
                TASK_TRANSCRIBE_JOB: 480,
            },
        )
        self.assertEqual(
            payload["processing_duration_ms_avg_by_type"],
            {
                TASK_PROCESS_AUDIO: 150.0,
                TASK_GENERATE_ARTIFACTS: 250.0,
                TASK_TRANSCRIBE_JOB: 300.0,
            },
        )
        self.assertEqual(
            payload["tasks_processed_by_type"],
            {
                TASK_PROCESS_AUDIO: 11,
                TASK_GENERATE_ARTIFACTS: 22,
                TASK_TRANSCRIBE_JOB: 33,
            },
        )
        self.assertEqual(
            payload["queue_depth_by_type"],
            {
                TASK_PROCESS_AUDIO: 2,
                TASK_GENERATE_ARTIFACTS: 1,
                TASK_TRANSCRIBE_JOB: 1,
            },
        )
        self.assertEqual(
            payload["processing_depth_by_type"],
            {
                TASK_PROCESS_AUDIO: 0,
                TASK_GENERATE_ARTIFACTS: 1,
                TASK_TRANSCRIBE_JOB: 1,
            },
        )
        self.assertEqual(
            payload["dead_letter_depth_by_type"],
            {
                TASK_PROCESS_AUDIO: 1,
                TASK_GENERATE_ARTIFACTS: 0,
                TASK_TRANSCRIBE_JOB: 1,
            },
        )
        self.assertEqual(
            payload["task_failures_by_type"],
            {
                TASK_PROCESS_AUDIO: 1,
                TASK_GENERATE_ARTIFACTS: 2,
                TASK_TRANSCRIBE_JOB: 0,
            },
        )
        self.assertEqual(
            payload["dead_letter_requeued_by_type"],
            {
                TASK_PROCESS_AUDIO: 3,
                TASK_GENERATE_ARTIFACTS: 2,
                TASK_TRANSCRIBE_JOB: 4,
            },
        )
        self.assertTrue(redis_stub.closed)

    def test_metrics_avg_fields_are_zero_when_no_samples(self) -> None:
        redis_stub = _MetricsRedisStub(
            values={
                WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY: 0,
                WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY: 0,
                WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY: 0,
                WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY: 0,
            }
        )
        with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
            with TestClient(app) as client:
                response = client.get("/metrics/worker")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["queue_latency_ms_avg"], 0.0)
        self.assertEqual(payload["processing_duration_ms_avg"], 0.0)
        self.assertEqual(payload["dead_letter_requeued_total"], 0)
        self.assertEqual(payload["dead_letter_requeued_last_10m"], 0)
        self.assertEqual(payload["worker_heartbeat_age_seconds"], -1)
        self.assertEqual(payload["transcribe_queue_depth"], 0)
        self.assertEqual(payload["transcribe_processing_depth"], 0)
        self.assertEqual(payload["transcribe_oldest_queue_age_seconds"], -1)
        self.assertEqual(payload["transcribe_oldest_processing_age_seconds"], -1)
        self.assertEqual(payload["transcribe_dead_letter_depth"], 0)
        self.assertEqual(payload["transcribe_oldest_dead_letter_age_seconds"], -1)
        self.assertEqual(
            payload["dead_letter_requeued_by_type"],
            {
                TASK_PROCESS_AUDIO: 0,
                TASK_GENERATE_ARTIFACTS: 0,
                TASK_TRANSCRIBE_JOB: 0,
            },
        )
        self.assertEqual(
            payload["queue_latency_ms_avg_by_type"],
            {
                TASK_PROCESS_AUDIO: 0.0,
                TASK_GENERATE_ARTIFACTS: 0.0,
                TASK_TRANSCRIBE_JOB: 0.0,
            },
        )
        self.assertEqual(
            payload["processing_duration_ms_avg_by_type"],
            {
                TASK_PROCESS_AUDIO: 0.0,
                TASK_GENERATE_ARTIFACTS: 0.0,
                TASK_TRANSCRIBE_JOB: 0.0,
            },
        )
        self.assertTrue(redis_stub.closed)

    def test_metrics_endpoint_returns_503_when_redis_unavailable(self) -> None:
        with patch("tutor_assistant.backend.get_redis_client", side_effect=RedisError("down")):
            with TestClient(app) as client:
                response = client.get("/metrics/worker")

        self.assertEqual(response.status_code, 503)
        self.assertIn("Failed to read worker metrics", response.json().get("detail", ""))

    def test_prometheus_metrics_endpoint_returns_text_payload(self) -> None:
        redis_stub = _MetricsRedisStub(
            values={
                WORKER_METRIC_TASKS_PROCESSED_KEY: 44,
                WORKER_METRIC_TASKS_FAILED_KEY: 3,
                WORKER_FAILURE_EVENTS_ZSET_KEY: 2,
                WORKER_METRIC_DEAD_LETTER_REQUEUED_EVENTS_ZSET_KEY: 4,
                WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY: 50,
                WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY: 120,
                WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY: 210,
                WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY: 3,
                WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY: 180,
                WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY: 250,
                WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY: 301,
                WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY: 2,
                WORKER_METRIC_HEARTBEAT_TS_KEY: 1995,
                WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY: 9,
                f"{WORKER_METRIC_TASKS_PROCESSED_KEY}:{TASK_PROCESS_AUDIO}": 11,
                f"{WORKER_METRIC_TASKS_PROCESSED_KEY}:{TASK_GENERATE_ARTIFACTS}": 22,
                f"{WORKER_METRIC_TASKS_PROCESSED_KEY}:{TASK_TRANSCRIBE_JOB}": 33,
                f"{WORKER_METRIC_TASKS_FAILED_KEY}:{TASK_PROCESS_AUDIO}": 1,
                f"{WORKER_METRIC_TASKS_FAILED_KEY}:{TASK_GENERATE_ARTIFACTS}": 2,
                f"{WORKER_METRIC_TASKS_FAILED_KEY}:{TASK_TRANSCRIBE_JOB}": 0,
                f"{WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY}:{TASK_PROCESS_AUDIO}": 3,
                f"{WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY}:{TASK_GENERATE_ARTIFACTS}": 2,
                f"{WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY}:{TASK_TRANSCRIBE_JOB}": 4,
                f"{WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY}:{TASK_PROCESS_AUDIO}": 45,
                f"{WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY}:{TASK_GENERATE_ARTIFACTS}": 60,
                f"{WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY}:{TASK_TRANSCRIBE_JOB}": 90,
                f"{WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY}:{TASK_PROCESS_AUDIO}": 110,
                f"{WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY}:{TASK_GENERATE_ARTIFACTS}": 130,
                f"{WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY}:{TASK_TRANSCRIBE_JOB}": 180,
                f"{WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY}:{TASK_PROCESS_AUDIO}": 200,
                f"{WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY}:{TASK_GENERATE_ARTIFACTS}": 240,
                f"{WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY}:{TASK_TRANSCRIBE_JOB}": 500,
                f"{WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY}:{TASK_PROCESS_AUDIO}": 4,
                f"{WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY}:{TASK_GENERATE_ARTIFACTS}": 3,
                f"{WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY}:{TASK_TRANSCRIBE_JOB}": 5,
                f"{WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY}:{TASK_PROCESS_AUDIO}": 170,
                f"{WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY}:{TASK_GENERATE_ARTIFACTS}": 220,
                f"{WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY}:{TASK_TRANSCRIBE_JOB}": 410,
                f"{WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY}:{TASK_PROCESS_AUDIO}": 260,
                f"{WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY}:{TASK_GENERATE_ARTIFACTS}": 290,
                f"{WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY}:{TASK_TRANSCRIBE_JOB}": 480,
                f"{WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY}:{TASK_PROCESS_AUDIO}": 600,
                f"{WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY}:{TASK_GENERATE_ARTIFACTS}": 500,
                f"{WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY}:{TASK_TRANSCRIBE_JOB}": 900,
                f"{WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY}:{TASK_PROCESS_AUDIO}": 4,
                f"{WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY}:{TASK_GENERATE_ARTIFACTS}": 2,
                f"{WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY}:{TASK_TRANSCRIBE_JOB}": 3,
            },
            queue_depth=4,
            processing_depth=1,
            dead_letter_depth=2,
            queue_items={
                LESSON_QUEUE_NAME: [
                    build_task_payload("lesson-1", TASK_TRANSCRIBE_JOB, enqueued_at=1986000),
                    build_task_payload("lesson-2", TASK_GENERATE_ARTIFACTS),
                    build_task_payload("lesson-3", TASK_PROCESS_AUDIO),
                    "legacy-lesson-4",
                ],
                LESSON_PROCESSING_QUEUE_NAME: [
                    build_task_payload("lesson-5", TASK_TRANSCRIBE_JOB, enqueued_at=1997000),
                    build_task_payload("lesson-6", TASK_GENERATE_ARTIFACTS),
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
                            "failed_at": "1970-01-01T00:33:15+00:00",
                        }
                    ),
                ],
            },
        )
        with patch("tutor_assistant.backend.time.time", return_value=2000):
            with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
                with TestClient(app) as client:
                    response = client.get("/metrics/worker/prometheus")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers.get("content-type", ""))
        body = response.text
        self.assertIn(
            "tutor_assistant_worker_tasks_processed_total 44",
            body,
        )
        self.assertIn(
            "tutor_assistant_worker_task_failures_total 3",
            body,
        )
        self.assertIn(
            "tutor_assistant_worker_dead_letter_requeued_total 9",
            body,
        )
        self.assertIn(
            "tutor_assistant_worker_dead_letter_requeued_last_10m 4",
            body,
        )
        self.assertIn(
            "tutor_assistant_worker_queue_depth 4",
            body,
        )
        self.assertIn(
            "tutor_assistant_worker_transcribe_queue_depth 1",
            body,
        )
        self.assertIn(
            "tutor_assistant_worker_transcribe_processing_depth 1",
            body,
        )
        self.assertIn(
            "tutor_assistant_worker_transcribe_oldest_queue_age_seconds 14",
            body,
        )
        self.assertIn(
            "tutor_assistant_worker_transcribe_oldest_processing_age_seconds 3",
            body,
        )
        self.assertIn(
            "tutor_assistant_worker_transcribe_dead_letter_depth 1",
            body,
        )
        self.assertIn(
            "tutor_assistant_worker_transcribe_oldest_dead_letter_age_seconds 20",
            body,
        )
        self.assertIn(
            "tutor_assistant_worker_queue_latency_ms_avg 70.0",
            body,
        )
        self.assertIn(
            "tutor_assistant_worker_processing_duration_ms_avg 150.5",
            body,
        )
        self.assertIn(
            "tutor_assistant_worker_heartbeat_timestamp_seconds 1995",
            body,
        )
        self.assertIn(
            "tutor_assistant_worker_heartbeat_age_seconds 5",
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_tasks_processed_by_type_total{{task_type="{TASK_PROCESS_AUDIO}"}} 11',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_tasks_processed_by_type_total{{task_type="{TASK_GENERATE_ARTIFACTS}"}} 22',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_tasks_processed_by_type_total{{task_type="{TASK_TRANSCRIBE_JOB}"}} 33',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_task_failures_by_type_total{{task_type="{TASK_PROCESS_AUDIO}"}} 1',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_task_failures_by_type_total{{task_type="{TASK_GENERATE_ARTIFACTS}"}} 2',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_task_failures_by_type_total{{task_type="{TASK_TRANSCRIBE_JOB}"}} 0',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_dead_letter_requeued_by_type_total{{task_type="{TASK_PROCESS_AUDIO}"}} 3',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_dead_letter_requeued_by_type_total{{task_type="{TASK_GENERATE_ARTIFACTS}"}} 2',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_dead_letter_requeued_by_type_total{{task_type="{TASK_TRANSCRIBE_JOB}"}} 4',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_queue_latency_ms_last_by_type{{task_type="{TASK_PROCESS_AUDIO}"}} 45',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_queue_latency_ms_avg_by_type{{task_type="{TASK_GENERATE_ARTIFACTS}"}} 80.0',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_processing_duration_ms_max_by_type{{task_type="{TASK_TRANSCRIBE_JOB}"}} 480',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_processing_duration_ms_avg_by_type{{task_type="{TASK_TRANSCRIBE_JOB}"}} 300.0',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_queue_depth_by_type{{task_type="{TASK_PROCESS_AUDIO}"}} 2',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_processing_depth_by_type{{task_type="{TASK_TRANSCRIBE_JOB}"}} 1',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_dead_letter_depth_by_type{{task_type="{TASK_PROCESS_AUDIO}"}} 1',
            body,
        )
        self.assertIn(
            f'tutor_assistant_worker_dead_letter_depth_by_type{{task_type="{TASK_TRANSCRIBE_JOB}"}} 1',
            body,
        )
        self.assertTrue(redis_stub.closed)

    def test_prometheus_metrics_endpoint_returns_503_when_redis_unavailable(self) -> None:
        with patch("tutor_assistant.backend.get_redis_client", side_effect=RedisError("down")):
            with TestClient(app) as client:
                response = client.get("/metrics/worker/prometheus")

        self.assertEqual(response.status_code, 503)
        self.assertIn("Failed to read worker metrics", response.json().get("detail", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
