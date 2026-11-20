"""Tests for blocked (waiting-for-decision) detection."""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr
from unittest import mock

from ai_router import blocking, config
from ai_router.blocking import detect_blocked, parse_session_log, strip_ansi
from ai_router.dispatch import RC_COMMAND_NOT_FOUND, RC_TIMEOUT

MARKER = config.BLOCKED_MARKER


class TestMarker(unittest.TestCase):
    def test_marker_extracts_fields(self) -> None:
        out = (
            "調査しました。\n"
            f"{MARKER}\n"
            "質問: 認証方式をどちらにしますか\n"
            "選択肢: JWT / セッションCookie\n"
            "推奨: JWT（既存APIがステートレスのため）\n"
        )
        sig = detect_blocked(0, out)
        assert sig is not None
        self.assertEqual(sig.kind, "marker")
        self.assertEqual(sig.reason, "blocked")
        self.assertEqual(sig.question, "認証方式をどちらにしますか")
        self.assertEqual(sig.options, ["JWT", "セッションCookie"])
        self.assertTrue(sig.recommendation.startswith("JWT"))
        self.assertIn(MARKER, sig.excerpt)

    def test_marker_bullet_options_and_english_fields(self) -> None:
        out = f"{MARKER}\nQuestion: Which DB?\nOptions:\n- Postgres\n- SQLite\nRecommendation: SQLite\n"
        sig = detect_blocked(0, out)
        assert sig is not None
        self.assertEqual(sig.question, "Which DB?")
        self.assertEqual(sig.options, ["Postgres", "SQLite"])
        self.assertEqual(sig.recommendation, "SQLite")

    def test_marker_takes_priority_over_rc(self) -> None:
        sig = detect_blocked(RC_TIMEOUT, f"{MARKER}\n質問: 続行しますか\n")
        assert sig is not None
        self.assertEqual(sig.kind, "marker")

    def test_echoed_template_is_ignored(self) -> None:
        sig = detect_blocked(0, config.HEADLESS_BLOCKED_INSTRUCTION + "\n\n実装を完了しました。")
        self.assertIsNone(sig)


class TestReturnCode(unittest.TestCase):
    def test_timeout(self) -> None:
        sig = detect_blocked(RC_TIMEOUT, "[error] Timed out")
        assert sig is not None
        self.assertEqual((sig.kind, sig.reason), ("exit", "timeout"))

    def test_launch_failed(self) -> None:
        sig = detect_blocked(RC_COMMAND_NOT_FOUND, "[error] Failed to launch")
        assert sig is not None
        self.assertEqual((sig.kind, sig.reason), ("exit", "launch_failed"))

    def test_nonzero_with_question_tail_is_blocked(self) -> None:
        sig = detect_blocked(1, "途中まで実装しました。\nこのまま既存APIを変更しても大丈夫?")
        assert sig is not None
        self.assertEqual((sig.kind, sig.reason), ("exit", "blocked"))
        self.assertIn("大丈夫", sig.question)

    def test_nonzero_without_question_is_none(self) -> None:
        self.assertIsNone(detect_blocked(2, "Traceback: boom\nValueError"))

    def test_none_rc_skips_rc_step(self) -> None:
        self.assertIsNone(detect_blocked(None, "still running..."))


class TestPatterns(unittest.TestCase):
    def test_japanese_pattern(self) -> None:
        sig = detect_blocked(0, "変更案は2つあります。\nどちらにしますか？")
        assert sig is not None
        self.assertEqual((sig.kind, sig.reason), ("pattern", "blocked"))
        self.assertEqual(sig.question, "どちらにしますか？")

    def test_english_pattern(self) -> None:
        sig = detect_blocked(0, "I found two approaches.\nShould I refactor the parser first?")
        assert sig is not None
        self.assertEqual(sig.kind, "pattern")

    def test_plain_question_mark_with_rc0_is_not_blocked(self) -> None:
        self.assertIsNone(detect_blocked(0, "完了しました。\n他に何かありますか？"))

    def test_completed_output_is_none(self) -> None:
        self.assertIsNone(detect_blocked(0, "All tests pass. Done."))

    def test_only_tail_is_scanned(self) -> None:
        head = "Should I do X?\n"
        filler = "\n".join(f"line {i}" for i in range(config.BLOCKED_TAIL_LINES + 5))
        self.assertIsNone(detect_blocked(0, head + filler))

    def test_env_adds_patterns(self) -> None:
        out = "作業メモ\nREQUIRES-HUMAN: pick a schema"
        self.assertIsNone(detect_blocked(0, out))
        with mock.patch.dict(os.environ, {config.BLOCKED_PATTERNS_ENV: "REQUIRES-HUMAN;;foo("}):
            with redirect_stderr(io.StringIO()) as err:
                sig = detect_blocked(0, out)
        assert sig is not None
        self.assertIn("REQUIRES-HUMAN", sig.question)
        self.assertIn("invalid blocked pattern", err.getvalue())


class TestLogHelpers(unittest.TestCase):
    def test_strip_ansi(self) -> None:
        self.assertEqual(strip_ansi("\x1b[31mred\x1b[0m\r\n"), "red\n")

    def test_parse_session_log(self) -> None:
        rc, out = parse_session_log("hello\nworld\n[ai-route] exit=3\n")
        self.assertEqual(rc, 3)
        self.assertEqual(out, "hello\nworld")
        self.assertEqual(parse_session_log("running")[0], None)

    def test_is_decision_block(self) -> None:
        self.assertTrue(blocking.is_decision_block(detect_blocked(0, "どちらにしますか")))
        self.assertFalse(blocking.is_decision_block(detect_blocked(RC_TIMEOUT, "")))
        self.assertFalse(blocking.is_decision_block(None))


if __name__ == "__main__":
    unittest.main()
