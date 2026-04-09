import unittest

from reactor_app.api import _normalize_requested_by, _validate_process_manual_command_payload


class ProcessManualValidationTests(unittest.TestCase):
    def test_requested_by_accepts_safe_identifier(self):
        self.assertEqual(_normalize_requested_by("ui.process:manual", default="api"), "ui.process:manual")

    def test_requested_by_rejects_spaces(self):
        with self.assertRaisesRegex(ValueError, "requested_by"):
            _normalize_requested_by("manual operator", default="api")

    def test_process_manual_payload_is_sanitized(self):
        payload = _validate_process_manual_command_payload(
            "manual_text",
            {
                "command_text": "  in_name  ",
                "expect_response": "true",
                "strip_response": "1",
                "response_timeout_ms": "2000",
                "recv_size": "1024",
            },
        )

        self.assertEqual(payload["text"], "in_name")
        self.assertNotIn("command_text", payload)
        self.assertIs(payload["expect_response"], True)
        self.assertIs(payload["strip_response"], True)
        self.assertEqual(payload["response_timeout_ms"], 2000)
        self.assertEqual(payload["recv_size"], 1024)

    def test_process_manual_payload_rejects_unexpected_fields(self):
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            _validate_process_manual_command_payload(
                "manual_text",
                {
                    "text": "IN_NAME",
                    "measurement": {"channel_code": "pv4"},
                },
            )

    def test_process_manual_payload_rejects_non_manual_command_name(self):
        with self.assertRaisesRegex(ValueError, "may only execute"):
            _validate_process_manual_command_payload("write_recipe", {"text": "IN_NAME"})


if __name__ == "__main__":
    unittest.main()
