"""Tests for planner/implementer separation helpers."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from ai_router import config, twostage
from ai_router.errors import AiRouteError

VALID = {
    "decisions": ["JWTを採用する"],
    "constraints": ["既存APIの互換性を維持"],
    "open_questions": [],
    "implementation_steps": ["auth.pyを追加", "テストを書く"],
    "implementer_difficulty": "low",
}


class TestExtractPlan(unittest.TestCase):
    def test_valid_json_with_surrounding_text(self) -> None:
        plan, ok = twostage.extract_plan("前置き\n```json\n" + json.dumps(VALID, ensure_ascii=False) + "\n```")
        self.assertTrue(ok)
        self.assertEqual(plan["decisions"], ["JWTを採用する"])
        self.assertEqual(plan["implementer_difficulty"], "low")
        self.assertNotIn("raw_notes", plan)

    def test_last_valid_plan_wins_over_unrelated_json(self) -> None:
        raw = 'example: {"foo": 1}\n... summary ...\n' + json.dumps(VALID)
        plan, ok = twostage.extract_plan(raw)
        self.assertTrue(ok)
        self.assertEqual(plan["implementation_steps"], VALID["implementation_steps"])

    def test_normalization(self) -> None:
        plan = twostage.validate_plan(
            {"decisions": "単一の判断", "implementation_steps": ["a", "", 3], "implementer_difficulty": "HIGH"}
        )
        assert plan is not None
        self.assertEqual(plan["decisions"], ["単一の判断"])
        self.assertEqual(plan["implementation_steps"], ["a", "3"])
        self.assertEqual(plan["constraints"], [])
        self.assertEqual(plan["implementer_difficulty"], "high")

    def test_unknown_difficulty_defaults(self) -> None:
        plan = twostage.validate_plan({**VALID, "implementer_difficulty": "extreme"})
        assert plan is not None
        self.assertEqual(plan["implementer_difficulty"], "medium")

    def test_invalid_inputs_fall_back(self) -> None:
        for raw in ("no json here", '{"decisions": [], "implementation_steps": []}',
                    '{"decisions": [{"nested": 1}]}', '["list"]', ""):
            plan, ok = twostage.extract_plan(raw)
            self.assertFalse(ok, msg=raw)
            self.assertEqual(plan["implementer_difficulty"], "medium")
            for key in twostage.PLAN_LIST_KEYS:
                self.assertEqual(plan[key], [])
        plan, _ = twostage.extract_plan("plain prose plan")
        self.assertEqual(plan["raw_notes"], "plain prose plan")


class TestPrompts(unittest.TestCase):
    def test_build_implementation_prompt_sections(self) -> None:
        prompt = twostage.build_implementation_prompt(VALID, "認証を追加", "## 対象・参照ファイル\n- a.py")
        self.assertTrue(prompt.startswith("## 対象・参照ファイル"))
        order = [prompt.index(h) for h in ("## タスク", "## 決定事項", "## 制約", "## 実装手順", "## 未解決点")]
        self.assertEqual(order, sorted(order))
        self.assertIn("- JWTを採用する", prompt)
        self.assertIn("1. auth.pyを追加\n2. テストを書く", prompt)
        self.assertIn("## 未解決点\n- (なし)", prompt)

    def test_raw_notes_are_included_for_fallback(self) -> None:
        plan, _ = twostage.extract_plan("自由記述の計画")
        prompt = twostage.build_implementation_prompt(plan, "task")
        self.assertIn("自由記述の計画", prompt)
        self.assertFalse(prompt.startswith("\n"))

    def test_planner_prompt_forbids_edits_and_has_schema(self) -> None:
        prompt = twostage.build_planner_prompt("タスクX", "FILES", "REPO")
        self.assertIn("編集", prompt)
        self.assertIn('"implementer_difficulty"', prompt)
        self.assertIn("タスクX", prompt)
        self.assertIn("FILES", prompt)

    def test_escalation_prompt_contains_session_context(self) -> None:
        meta = {
            "task": "T",
            "refined_prompt": "RP",
            "blocked": {"kind": "marker", "reason": "blocked", "question": "A or B?", "options": ["A", "B"],
                        "recommendation": "A"},
            "last_output_summary": "OUT",
            "diff_stat": "a.py | 2 +-",
        }
        prompt = twostage.build_escalation_prompt(meta, note="セキュリティ優先")
        for text in ("T", "RP", "A or B?", "A / B", "OUT", "a.py | 2 +-", "セキュリティ優先", "編集"):
            self.assertIn(text, prompt)


class TestSelectCandidate(unittest.TestCase):
    def test_exact_match_first_in_order(self) -> None:
        self.assertEqual(twostage.select_candidate("high"), ("agy", "gemini-4-neon-high"))
        self.assertEqual(twostage.select_candidate("low"), ("opencode", "opencode-go/deepseek-v4.1-flash"))

    def test_tool_filter(self) -> None:
        self.assertEqual(twostage.select_candidate("high", "claude"), ("claude", "opus"))
        self.assertEqual(twostage.select_candidate("medium", "claude"), ("claude", "sonnet"))

    def test_nearest_difficulty_prefers_stronger(self) -> None:
        # claude has no "low" candidate -> nearest is medium
        self.assertEqual(twostage.select_candidate("low", "claude"), ("claude", "sonnet"))
        # opencode has only "low"
        self.assertEqual(twostage.select_candidate("high", "opencode")[0], "opencode")

    def test_unknown_difficulty_and_tool(self) -> None:
        self.assertEqual(twostage.select_candidate("weird", "claude"), ("claude", "sonnet"))
        with self.assertRaises(AiRouteError):
            twostage.select_candidate("low", "nope")


class TestStageResolution(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(config.PLANNER_ENV, None)
        os.environ.pop(config.IMPLEMENTER_ENV, None)

    def test_defaults(self) -> None:
        self.assertEqual(twostage.resolve_planner(), ("claude", "opus"))
        self.assertEqual(twostage.resolve_implementer("low"), ("opencode", "opencode-go/deepseek-v4.1-flash"))

    def test_presets(self) -> None:
        for name, preset in config.STAGE_PRESETS.items():
            self.assertEqual(":".join(twostage.resolve_planner(None, name)), preset["planner"])
            self.assertEqual(":".join(twostage.resolve_implementer("high", None, name)), preset["implementer"])
            for spec in preset.values():
                self.assertIn(spec.split(":", 1)[0], config.SUPPORTED_TOOLS)

    def test_env_overrides_preset_and_cli_overrides_env(self) -> None:
        os.environ[config.PLANNER_ENV] = "agy:gemini-4-neon-high"
        os.environ[config.IMPLEMENTER_ENV] = "claude"
        self.assertEqual(twostage.resolve_planner(None, "claude"), ("agy", "gemini-4-neon-high"))
        self.assertEqual(twostage.resolve_implementer("high", None, "cheap"), ("claude", "opus"))
        self.assertEqual(twostage.resolve_planner("claude:sonnet", "gemini"), ("claude", "sonnet"))
        self.assertEqual(twostage.resolve_implementer("low", "agy:x", "cheap"), ("agy", "x"))

    def test_invalid_specs(self) -> None:
        with self.assertRaises(AiRouteError):
            twostage.resolve_implementer("low", "bogus")
        with self.assertRaises(AiRouteError):
            twostage.resolve_planner("bogus:model")
        with self.assertRaises(AiRouteError):
            twostage.resolve_planner(None, "no-such-preset")


if __name__ == "__main__":
    unittest.main()
