#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Token 统计核心规则的回归测试。"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import token_stats


def write_jsonl(path, records, damaged_line=False):
    """写入测试 JSONL，可附加一行损坏数据。"""
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    if damaged_line:
        lines.insert(1, "{damaged")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class ConfigTests(unittest.TestCase):
    """验证用户配置损坏时仍能安全启动。"""

    def test_load_config_normalizes_invalid_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "x": "320",
                        "y": None,
                        "opacity": 9,
                        "refresh_sec": 0,
                        "topmost": "yes",
                        "claude_dir": "",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(token_stats, "CONFIG_PATH", config_path):
                cfg = token_stats.load_config()

        self.assertEqual(cfg["x"], 320)
        self.assertEqual(cfg["y"], token_stats.DEFAULT_CONFIG["y"])
        self.assertEqual(cfg["opacity"], token_stats.MAX_OPACITY)
        self.assertEqual(cfg["refresh_sec"], token_stats.MIN_REFRESH_SEC)
        self.assertEqual(cfg["topmost"], token_stats.DEFAULT_CONFIG["topmost"])
        self.assertEqual(cfg["claude_dir"], token_stats.DEFAULT_CONFIG["claude_dir"])

    def test_load_config_handles_broken_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text("{broken", encoding="utf-8")
            with mock.patch.object(token_stats, "CONFIG_PATH", config_path):
                cfg = token_stats.load_config()

        self.assertEqual(cfg, token_stats.DEFAULT_CONFIG)


class AdapterTests(unittest.TestCase):
    """验证两个日志适配器的去重、差分和容错规则。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_claude_deduplicates_and_keeps_larger_usage(self):
        path = self.root / "session.jsonl"
        base = {
            "type": "assistant",
            "sessionId": "session-1",
            "timestamp": self.timestamp,
            "message": {
                "id": "message-1",
                "model": "claude-test",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        }
        larger = json.loads(json.dumps(base))
        larger["message"]["usage"]["output_tokens"] = 5
        malformed = {"type": "assistant", "message": ["invalid"]}
        write_jsonl(path, [base, base, larger, malformed], damaged_line=True)

        result = token_stats.scan_claude(str(self.root), days=1)
        summary = token_stats.sum_window(result["by_date"])

        self.assertEqual(summary["total"], 15)
        self.assertEqual(summary["events"], 1)
        self.assertEqual(result["by_model"]["claude-test"]["total"], 15)
        self.assertTrue(any("dup-key-conflict" in item for item in result["warnings"]))
        self.assertTrue(any("invalid-message" in item for item in result["warnings"]))

    def test_codex_differences_cumulative_values_and_handles_reset(self):
        path = self.root / "rollout-test.jsonl"

        def event(usage, rate_limits=None):
            payload = {"type": "token_count", "info": {"total_token_usage": usage}}
            if rate_limits is not None:
                payload["rate_limits"] = rate_limits
            return {"type": "event_msg", "timestamp": self.timestamp, "payload": payload}

        first = {
            "input_tokens": 10,
            "cached_input_tokens": 2,
            "output_tokens": 3,
            "reasoning_output_tokens": 1,
            "total_tokens": 16,
        }
        second = {
            "input_tokens": 15,
            "cached_input_tokens": 4,
            "output_tokens": 8,
            "reasoning_output_tokens": 2,
            "total_tokens": 29,
        }
        reset = {
            "input_tokens": 2,
            "cached_input_tokens": 0,
            "output_tokens": 1,
            "reasoning_output_tokens": 0,
            "total_tokens": 3,
        }
        records = [
            event(first),
            event(second, {"primary": {"used_percent": 12, "window_minutes": 300}}),
            {"type": "event_msg", "payload": ["invalid"]},
            event(["invalid"]),
            event(reset),
        ]
        write_jsonl(path, records, damaged_line=True)

        result = token_stats.scan_codex(str(self.root), days=1)
        summary = token_stats.sum_window(result["by_date"])

        self.assertEqual(summary["total"], 32)
        self.assertEqual(summary["input"], 17)
        self.assertEqual(summary["events"], 3)
        self.assertEqual(result["rate_limit"]["used_percent"], 12)
        self.assertTrue(any("cumulative-reset" in item for item in result["warnings"]))


class UtilityTests(unittest.TestCase):
    """验证基础数值和命令行参数约束。"""

    def test_nn_accepts_only_non_negative_integers(self):
        self.assertEqual(token_stats.nn(3), 3)
        for value in (-1, 2.5, True, "3", None):
            with self.subTest(value=value):
                self.assertEqual(token_stats.nn(value), 0)

    def test_positive_int_rejects_zero(self):
        self.assertEqual(token_stats.positive_int("7"), 7)
        with self.assertRaises(token_stats.argparse.ArgumentTypeError):
            token_stats.positive_int("0")


if __name__ == "__main__":
    unittest.main()
