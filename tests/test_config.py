"""Unit tests covering external configuration loading, merging, validation, and CLI integration."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_router import cli, config, router, utils
from ai_router.config import (
    DEFAULT_CANDIDATES,
    DEFAULT_FALLBACK_DECISION,
    DEFAULT_MODEL_PRESETS,
    DEFAULT_SUPPORTED_TOOLS,
    AppConfig,
    load_config,
    load_config_from_dict,
    set_active_config,
)
from ai_router.errors import AiRouteError, ConfigError


class TestConfigLoadingAndMerging(unittest.TestCase):
    def test_default_config_matches_builtin_constants(self) -> None:
        cfg = load_config_from_dict({})
        self.assertEqual(cfg.supported_tools, DEFAULT_SUPPORTED_TOOLS)
        self.assertEqual(cfg.candidates, DEFAULT_CANDIDATES)
        self.assertEqual(cfg.presets["fast"], DEFAULT_MODEL_PRESETS["fast"])
        self.assertEqual(cfg.presets["balanced"], DEFAULT_MODEL_PRESETS["balanced"])
        self.assertEqual(cfg.presets["strong"], DEFAULT_MODEL_PRESETS["strong"])
        self.assertEqual(cfg.fallback_decision, DEFAULT_FALLBACK_DECISION)
        self.assertEqual(cfg.allowed_pairs, {(c["tool"], c["model"]) for c in DEFAULT_CANDIDATES})

    def test_add_and_override_candidates(self) -> None:
        raw = {
            "candidates": [
                {
                    "tool": "claude",
                    "model": "sonnet",
                    "difficulty": "high",
                    "description": "Overridden description",
                },
                {
                    "tool": "agy",
                    "model": "gemini-ultra-custom",
                    "difficulty": "high",
                    "description": "New custom model",
                },
            ]
        }
        cfg = load_config_from_dict(raw)
        # Check override
        sonnet = next(c for c in cfg.candidates if c["tool"] == "claude" and c["model"] == "sonnet")
        self.assertEqual(sonnet["difficulty"], "high")
        self.assertEqual(sonnet["description"], "Overridden description")
        # Check addition
        self.assertTrue(any(c["model"] == "gemini-ultra-custom" for c in cfg.candidates))
        self.assertIn(("agy", "gemini-ultra-custom"), cfg.allowed_pairs)

    def test_remove_candidates(self) -> None:
        raw = {
            "remove_candidates": [
                "opencode:opencode-go/deepseek-v4.1-flash",
                {"tool": "agy", "model": "gemini-4-neon-high"},
            ]
        }
        cfg = load_config_from_dict(raw)
        pairs = {(c["tool"], c["model"]) for c in cfg.candidates}
        self.assertNotIn(("opencode", "opencode-go/deepseek-v4.1-flash"), pairs)
        self.assertNotIn(("agy", "gemini-4-neon-high"), pairs)
        self.assertNotIn(("opencode", "opencode-go/deepseek-v4.1-flash"), cfg.allowed_pairs)
        self.assertNotIn(("agy", "gemini-4-neon-high"), cfg.allowed_pairs)
        # 'all' preset should reflect the removals
        self.assertNotIn(("opencode", "opencode-go/deepseek-v4.1-flash"), cfg.presets["all"])

    def test_reset_candidates(self) -> None:
        raw = {
            "reset_candidates": True,
            "candidates": [
                {
                    "tool": "claude",
                    "model": "custom-only",
                    "difficulty": "medium",
                    "description": "Only model",
                }
            ],
            "fallback": {
                "tool": "claude",
                "model": "custom-only",
            },
            "presets": {
                "custom": [["claude", "custom-only"]],
            },
        }
        cfg = load_config_from_dict(raw)
        self.assertEqual(len(cfg.candidates), 1)
        self.assertEqual(cfg.candidates[0]["model"], "custom-only")
        self.assertEqual(cfg.fallback_decision["model"], "custom-only")

    def test_preset_addition_and_override(self) -> None:
        raw = {
            "presets": {
                "fast": [
                    ["claude", "sonnet"],
                ],
                "team_x": [
                    ["agy", "gemini-3.8-flash-medium"],
                    ["claude", "opus"],
                ],
            }
        }
        cfg = load_config_from_dict(raw)
        self.assertEqual(cfg.presets["fast"], [("claude", "sonnet")])
        self.assertEqual(
            cfg.presets["team_x"],
            [("agy", "gemini-3.8-flash-medium"), ("claude", "opus")],
        )

    def test_allowed_models_subscription_declaration(self) -> None:
        raw = {
            "allowed_models": [
                "claude:sonnet",
                "claude:opus",
            ]
        }
        cfg = load_config_from_dict(raw)
        pairs = {(c["tool"], c["model"]) for c in cfg.candidates}
        self.assertEqual(pairs, {("claude", "sonnet"), ("claude", "opus")})
        self.assertEqual(cfg.available_tools, ("claude",))
        # Presets that referenced opencode or agy have those models filtered out or presets removed
        self.assertNotIn("fast", cfg.presets)
        self.assertIn("strong", cfg.presets)
        for tool, _ in cfg.presets["strong"]:
            self.assertEqual(tool, "claude")
        # Fallback automatically defaults to an allowed model if default was disallow (claude:sonnet is allowed anyway)
        self.assertEqual(cfg.fallback_decision["tool"], "claude")
        self.assertEqual(cfg.fallback_decision["model"], "sonnet")


class TestConfigValidationErrors(unittest.TestCase):
    def test_unsupported_tool_in_candidates(self) -> None:
        raw = {
            "candidates": [
                {
                    "tool": "unknown_tool",
                    "model": "model-x",
                    "difficulty": "medium",
                    "description": "test",
                }
            ]
        }
        with self.assertRaises(ConfigError) as ctx:
            load_config_from_dict(raw)
        self.assertIn("unsupported tool 'unknown_tool'", str(ctx.exception))

    def test_empty_model_name(self) -> None:
        raw = {
            "candidates": [
                {
                    "tool": "claude",
                    "model": "   ",
                    "difficulty": "medium",
                    "description": "test",
                }
            ]
        }
        with self.assertRaises(ConfigError) as ctx:
            load_config_from_dict(raw)
        self.assertIn("empty model", str(ctx.exception))

    def test_invalid_difficulty(self) -> None:
        raw = {
            "candidates": [
                {
                    "tool": "claude",
                    "model": "sonnet",
                    "difficulty": "extreme",
                    "description": "test",
                }
            ]
        }
        with self.assertRaises(ConfigError) as ctx:
            load_config_from_dict(raw)
        self.assertIn("invalid difficulty 'extreme'", str(ctx.exception))

    def test_duplicate_candidate_in_toml(self) -> None:
        raw = {
            "reset_candidates": True,
            "candidates": [
                {"tool": "claude", "model": "sonnet", "difficulty": "medium", "description": "a"},
                {"tool": "claude", "model": "sonnet", "difficulty": "high", "description": "b"},
            ],
        }
        with self.assertRaises(ConfigError) as ctx:
            load_config_from_dict(raw)
        self.assertIn("Duplicate candidate", str(ctx.exception))

    def test_fallback_not_in_allowed_candidates(self) -> None:
        raw = {
            "fallback": {
                "tool": "claude",
                "model": "non-existent-model",
            }
        }
        with self.assertRaises(ConfigError) as ctx:
            load_config_from_dict(raw)
        self.assertIn("FALLBACK_DECISION", str(ctx.exception))
        self.assertIn("not among the allowed candidates", str(ctx.exception))

    def test_preset_referencing_disallowed_model(self) -> None:
        raw = {
            "presets": {
                "broken": [["claude", "ghost-model"]],
            }
        }
        with self.assertRaises(ConfigError) as ctx:
            load_config_from_dict(raw)
        self.assertIn("Preset 'broken' references model claude:ghost-model", str(ctx.exception))


class TestConfigPriorityAndCliIntegration(unittest.TestCase):
    def test_priority_env_over_config_file(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as tf:
            tf.write("""
router_model = "file-router-model"
router_timeout = 120.0
""")
            tf.flush()

            # 1. Config file alone
            cfg = load_config(tf.name)
            self.assertEqual(cfg.router_model, "file-router-model")
            self.assertEqual(cfg.router_timeout_sec, 120.0)

            # 2. Overridden by environment variable
            with mock.patch.dict(os.environ, {"AI_ROUTE_ROUTER_MODEL": "env-router-model"}):
                cfg_env = load_config(tf.name)
                self.assertEqual(cfg_env.router_model, "env-router-model")
                self.assertEqual(cfg_env.router_timeout_sec, 120.0)

    def test_cli_option_config_loading(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as tf:
            tf.write("""
allowed_models = [
    "agy:gemini-3.8-flash-medium",
]
""")
            tf.flush()

            # Test cli.main with --config
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["--config", tf.name, "run", "--tool", "claude", "--model", "sonnet", "task"])
                self.assertEqual(ctx.exception.code, 2)  # argparse choice validation error
            self.assertIn("invalid choice: 'claude'", err.getvalue())

    def test_cli_missing_config_file_error(self) -> None:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["--config", "/nonexistent/config.toml", "run", "task"])
            self.assertEqual(ctx.exception.code, 1)
        self.assertIn("[error] Specified configuration file not found", err.getvalue())

    def test_router_disallowed_pair_falls_back(self) -> None:
        cfg = load_config_from_dict({
            "allowed_models": [
                "claude:sonnet",
            ]
        })
        # Simulate router selecting a model not in allowed_pairs
        mock_output = '{"tool": "agy", "model": "gemini-4-neon-high", "difficulty": "high"}'
        with mock.patch("ai_router.router.run_cmd") as mock_cmd:
            mock_cmd.return_value = mock.Mock(returncode=0, stdout=mock_output)
            decision = router.route_task("Task", "", "", False, config=cfg)
            self.assertTrue(decision.is_fallback)
            self.assertEqual(decision.tool, "claude")
            self.assertEqual(decision.model, "sonnet")

    def test_parse_model_specs_resolves_custom_preset(self) -> None:
        cfg = load_config_from_dict({
            "presets": {
                "custom_duo": [["claude", "sonnet"], ["agy", "gemini-3.8-flash-medium"]],
            }
        })
        specs = utils.parse_model_specs("", "custom_duo", presets=cfg.presets)
        self.assertEqual(specs, [("claude", "sonnet"), ("agy", "gemini-3.8-flash-medium")])

        # Active config integration
        set_active_config(cfg)
        try:
            specs_active = utils.parse_model_specs("", "custom_duo")
            self.assertEqual(specs_active, [("claude", "sonnet"), ("agy", "gemini-3.8-flash-medium")])
        finally:
            set_active_config(load_config_from_dict({}))

