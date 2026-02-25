from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tutor_assistant.backend import app
from tutor_assistant.queue import (
    LESSON_DEAD_LETTER_QUEUE_NAME,
    LESSON_PROCESSING_QUEUE_NAME,
    LESSON_QUEUE_NAME,
    WORKER_FAILURE_EVENTS_ZSET_KEY,
    WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY,
    WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY,
    WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY,
    WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY,
    WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY,
    WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY,
    WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY,
    WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY,
    WORKER_METRIC_TASKS_FAILED_KEY,
    WORKER_METRIC_TASKS_PROCESSED_KEY,
)


class _MetricsRedisStub:
    def __init__(
        self,
        *,
        values: dict[str, int] | None = None,
        queue_depth: int = 0,
        processing_depth: int = 0,
        dead_letter_depth: int = 0,
    ) -> None:
        self._values = values or {}
        self._depths = {
            LESSON_QUEUE_NAME: queue_depth,
            LESSON_PROCESSING_QUEUE_NAME: processing_depth,
            LESSON_DEAD_LETTER_QUEUE_NAME: dead_letter_depth,
        }
        self.closed = False

    def get(self, key: str):
        return self._values.get(key, 0)

    def zcount(self, key: str, min_score: int, max_score: int):  # noqa: ARG002
        return self._values.get(key, 0)

    def llen(self, key: str):
        return self._depths.get(key, 0)

    def close(self) -> None:
        self.closed = True


class WorkerMetricsEndpointTest(unittest.TestCase):
    def test_metrics_endpoint_returns_latency_and_duration_fields(self) -> None:
        redis_stub = _MetricsRedisStub(
            values={
                WORKER_METRIC_TASKS_PROCESSED_KEY: 44,
                WORKER_METRIC_TASKS_FAILED_KEY: 3,
                WORKER_FAILURE_EVENTS_ZSET_KEY: 2,
                WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY: 50,
                WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY: 120,
                WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY: 210,
                WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY: 3,
                WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY: 180,
                WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY: 250,
                WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY: 301,
                WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY: 2,
            },
            queue_depth=4,
            processing_depth=1,
            dead_letter_depth=0,
        )

        with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
            with TestClient(app) as client:
                response = client.get("/metrics/worker")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["tasks_processed_total"], 44)
        self.assertEqual(payload["task_failures_total"], 3)
        self.assertEqual(payload["worker_errors_last_10m"], 2)
        self.assertEqual(payload["queue_depth"], 4)
        self.assertEqual(payload["processing_depth"], 1)
        self.assertEqual(payload["dead_letter_depth"], 0)
        self.assertEqual(payload["queue_latency_ms_last"], 50)
        self.assertEqual(payload["queue_latency_ms_max"], 120)
        self.assertEqual(payload["queue_latency_ms_avg"], 70.0)
        self.assertEqual(payload["processing_duration_ms_last"], 180)
        self.assertEqual(payload["processing_duration_ms_max"], 250)
        self.assertEqual(payload["processing_duration_ms_avg"], 150.5)
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
        self.assertTrue(redis_stub.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
