import os
import tempfile
import unittest
from unittest.mock import patch

from chatgpt_signup_to_code import (
    SESSION_DIR_PREFIX,
    cleanup_session_dir,
    parse_args,
    phone_submit_reached_password_step,
    should_retry_submitted_step,
    summarize_json_payload,
)
from launcher import MAX_LOG_MESSAGE_LENGTH, truncate_log_message


class LoggingSafetyTests(unittest.TestCase):
    def test_truncate_log_message_keeps_output_bounded(self) -> None:
        message = "a" * (MAX_LOG_MESSAGE_LENGTH + 500)
        result = truncate_log_message(message)
        self.assertIn("日志已截断", result)
        self.assertLessEqual(len(result), MAX_LOG_MESSAGE_LENGTH + 80)

    def test_summarize_json_payload_replaces_large_file_dump_with_summary(self) -> None:
        payload = {
            "files": [
                {"name": f"file-{index}.json", "content": "x" * 4000}
                for index in range(30)
            ],
            "state": "ok",
        }
        result = summarize_json_payload(payload, max_length=600)
        self.assertIn('"filesCount": 30', result)
        self.assertIn('"sampleFiles"', result)
        self.assertNotIn("x" * 200, result)
        self.assertLessEqual(len(result), 600)

    def test_should_retry_submitted_step_only_when_recovering_from_fatal_error(self) -> None:
        self.assertTrue(should_retry_submitted_step("password", "fatal_error", True))
        self.assertTrue(should_retry_submitted_step("signup_phone", "fatal_error", True))
        self.assertFalse(should_retry_submitted_step("password", "sms_code", True))
        self.assertFalse(should_retry_submitted_step("password", "fatal_error", False))
        self.assertFalse(should_retry_submitted_step("sms_code", "fatal_error", True))

    def test_phone_submit_must_land_on_password_url(self) -> None:
        self.assertTrue(
            phone_submit_reached_password_step("password", "https://auth.openai.com/create-account/password")
        )
        self.assertTrue(
            phone_submit_reached_password_step(
                "password",
                "https://auth.openai.com/create-account/password?client_id=test",
            )
        )
        self.assertFalse(phone_submit_reached_password_step("password", "https://auth.openai.com/add-email"))
        self.assertFalse(phone_submit_reached_password_step("fatal_error", "https://auth.openai.com/create-account/password"))

    def test_sms_polling_defaults_match_signup_retry_policy(self) -> None:
        with patch("sys.argv", ["chatgpt_signup_to_code.py"]):
            args = parse_args()
        self.assertEqual(args.phone_poll_interval, 15)
        self.assertEqual(args.max_code_attempts, 6)

    def test_cleanup_session_dir_removes_directory_tree(self) -> None:
        target = tempfile.mkdtemp(prefix=SESSION_DIR_PREFIX)
        marker = os.path.join(target, "marker.txt")
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("ok")
        self.assertTrue(cleanup_session_dir(target))
        self.assertFalse(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
