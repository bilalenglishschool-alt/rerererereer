from __future__ import annotations

import unittest
from unittest.mock import patch

from tutor_assistant.ops.check_worker_alerts import (
    check_worker_alerts,
    evaluate_alert_payload,
    main,
)


class _ResponseStub:
    def __init__(self, status_code: int, payload=None, json_error: Exception | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class TestWorkerAlertChecker(unittest.TestCase):
    def test_evaluate_payload_ok(self) -> None:
        code, message = evaluate_alert_payload({"status": "ok", "alerts": []})
        self.assertEqual(code, 0)
        self.assertIn("status=ok", message)

    def test_evaluate_payload_alert(self) -> None:
        code, message = evaluate_alert_payload(
            {"status": "alert", "alerts": ["worker_errors_last_10m exceeded threshold"]}
        )
        self.assertEqual(code, 2)
        self.assertIn("status=alert", message)
        self.assertIn("worker_errors_last_10m exceeded threshold", message)

    def test_check_worker_alerts_non_200(self) -> None:
        with patch(
            "tutor_assistant.ops.check_worker_alerts.requests.get",
            return_value=_ResponseStub(status_code=503, payload={"status": "ok"}),
        ):
            code, message = check_worker_alerts("https://example.com/alerts/worker")

        self.assertEqual(code, 1)
        self.assertIn("Non-200 response", message)

    def test_check_worker_alerts_invalid_json(self) -> None:
        with patch(
            "tutor_assistant.ops.check_worker_alerts.requests.get",
            return_value=_ResponseStub(status_code=200, json_error=ValueError("bad json")),
        ):
            code, message = check_worker_alerts("https://example.com/alerts/worker")

        self.assertEqual(code, 1)
        self.assertIn("Invalid JSON", message)

    def test_check_worker_alerts_sends_ops_token_header(self) -> None:
        with patch(
            "tutor_assistant.ops.check_worker_alerts.requests.get",
            return_value=_ResponseStub(status_code=200, payload={"status": "ok"}),
        ) as requests_get:
            code, message = check_worker_alerts(
                "https://example.com/alerts/worker",
                timeout_seconds=7,
                token="ops-secret",
            )

        self.assertEqual(code, 0)
        self.assertIn("status=ok", message)
        requests_get.assert_called_once_with(
            "https://example.com/alerts/worker",
            timeout=7,
            headers={"X-Ops-Token": "ops-secret"},
        )

    def test_main_fails_when_url_missing(self) -> None:
        with patch("tutor_assistant.ops.check_worker_alerts.print"):
            with patch.dict("os.environ", {}, clear=True):
                code = main()
        self.assertEqual(code, 1)

    def test_main_returns_alert_exit_code(self) -> None:
        with patch("tutor_assistant.ops.check_worker_alerts.print"):
            with patch.dict(
                "os.environ",
                {"WORKER_ALERT_URL": "https://example.com/alerts/worker", "ALERT_TIMEOUT_SECONDS": "5"},
                clear=True,
            ):
                with patch(
                    "tutor_assistant.ops.check_worker_alerts.requests.get",
                    return_value=_ResponseStub(status_code=200, payload={"status": "alert", "alerts": ["x"]}),
                ):
                    code = main()

        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
