import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import client_usage_export
import monitor


class UsageHistoryIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_history_path = monitor.USAGE_HISTORY_JSON
        monitor.USAGE_HISTORY_JSON = Path(self.temporary_directory.name) / "usage_history.json"
        self.day = monitor.today_key()

    def tearDown(self) -> None:
        monitor.USAGE_HISTORY_JSON = self.original_history_path
        self.temporary_directory.cleanup()

    def seed_history(self, source: str = "both") -> None:
        monitor.write_json_atomic(
            monitor.USAGE_HISTORY_JSON,
            {
                "schema": 1,
                "days": {
                    self.day: {
                        "date": self.day,
                        "source": source,
                        "requests": 100,
                        "tokens": 1_000_000,
                        "cost": 10.0,
                        "source_date": self.day,
                    }
                },
            },
        )

    def test_combined_usage_accepts_service_day_reset(self) -> None:
        self.seed_history("both")
        state = monitor.MonitorState(
            usage_source="both",
            today_requests=10,
            today_tokens=100_000,
            today_account_cost=1.0,
            client_usage={"date": self.day, "providers": []},
        )

        monitor.update_usage_history(state)

        saved = monitor.load_usage_history()["days"][self.day]
        self.assertEqual(state.today_tokens, 100_000)
        self.assertEqual(saved["tokens"], 100_000)

    def test_local_history_high_water_never_mutates_live_state(self) -> None:
        self.seed_history("local")
        state = monitor.MonitorState(
            usage_source="local",
            today_requests=10,
            today_tokens=100_000,
            today_account_cost=1.0,
            client_usage={"date": self.day, "providers": []},
        )

        monitor.update_usage_history(state)

        saved = monitor.load_usage_history()["days"][self.day]
        self.assertEqual(state.today_tokens, 100_000)
        self.assertEqual(saved["tokens"], 1_000_000)


class LocalExportHighWaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.output_path = Path(self.temporary_directory.name) / "client_usage_today.json"
        self.day = date.today()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def snapshot(self, snapshot_day: date, tokens: int) -> dict:
        return {
            "date": snapshot_day.isoformat(),
            "today": {
                "requests": 10,
                "tokens": tokens,
                "input_tokens": tokens,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 0,
                "cost": 1.0,
            },
            "providers": [
                {
                    "name": "Codex local - account@example.com",
                    "requests": 10,
                    "tokens": tokens,
                    "input_tokens": tokens,
                    "cached_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 0,
                    "cost": 1.0,
                    "window_7d": {
                        "requests": 20,
                        "tokens": tokens * 2,
                        "cost": 2.0,
                        "quota_available": True,
                    },
                }
            ],
            "latest_request": {
                "provider": "Codex local - account@example.com",
                "model": "gpt-test",
                "created_at": f"{snapshot_day.isoformat()}T09:00:00+08:00",
                "kind": "success",
            },
            "dashboard": {
                "hourly_today": [
                    {"hour": 9, "requests": 10, "tokens": tokens, "cost": 1.0}
                ]
            },
        }

    def test_same_day_switch_preserves_totals_but_uses_current_quota(self) -> None:
        previous = self.snapshot(self.day, 1_000_000)
        current = self.snapshot(self.day, 100_000)
        previous["providers"][0]["window_7d"].update(
            {"remaining_percent": 31.0, "utilization": 69.0}
        )
        current["providers"][0]["window_7d"].update(
            {"remaining_percent": 17.0, "utilization": 83.0}
        )
        self.output_path.write_text(json.dumps(previous), encoding="utf-8")

        client_usage_export.same_day_output_high_water(current, self.output_path, self.day)

        self.assertEqual(current["today"]["tokens"], 1_000_000)
        self.assertEqual(current["providers"][0]["window_7d"]["tokens"], 200_000)
        self.assertEqual(current["providers"][0]["window_7d"]["utilization"], 83.0)
        self.assertEqual(current["dashboard"]["hourly_today"][0]["tokens"], 1_000_000)
        self.assertEqual(current["latest_request"]["model"], "gpt-test")

    def test_new_day_never_inherits_previous_day_high_water(self) -> None:
        yesterday = self.day - timedelta(days=1)
        previous = self.snapshot(yesterday, 1_000_000)
        current = self.snapshot(self.day, 100_000)
        self.output_path.write_text(json.dumps(previous), encoding="utf-8")

        client_usage_export.same_day_output_high_water(current, self.output_path, self.day)

        self.assertEqual(current["today"]["tokens"], 100_000)
        self.assertEqual(current["providers"][0]["window_7d"]["tokens"], 200_000)


if __name__ == "__main__":
    unittest.main()
