from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tutor_assistant.backend import app


class WebhookLoggingSecurityTest(unittest.TestCase):
    def test_webhook_logs_only_metadata(self) -> None:
        payload = {
            "update_id": 123456,
            "message": {
                "from": {"id": 555001},
                "text": "/start invite_sensitive_token_123",
            },
        }

        with patch("tutor_assistant.bot.process_update", new=AsyncMock(return_value=None)):
            with patch("tutor_assistant.backend.logger.info") as mocked_info:
                with TestClient(app) as client:
                    response = client.post("/webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

        webhook_calls = [
            call
            for call in mocked_info.call_args_list
            if call.args and call.args[0] == "TG webhook update_id=%s event=%s from_user_id=%s"
        ]
        self.assertEqual(len(webhook_calls), 1)
        args = webhook_calls[0].args
        self.assertEqual(args[1:], (123456, "message", 555001))

        # Ensure sensitive message text/token are not included in metadata log args.
        flattened = " ".join(str(part) for part in args[1:])
        self.assertNotIn("invite_sensitive_token_123", flattened)


if __name__ == "__main__":
    unittest.main(verbosity=2)
