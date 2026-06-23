import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

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


class WindowSemanticsTests(unittest.TestCase):
    def test_rolling_usage_is_not_replaced_by_quota_cycle_usage(self) -> None:
        now = datetime(2026, 6, 22, 12, 0, 0)
        label = "Codex local - account@example.com"
        rolling_5h = client_usage_export.UsageBucket(
            requests=8,
            input_tokens=5_000_000,
            cost=5.0,
        )
        rolling_7d = client_usage_export.UsageBucket(
            requests=70,
            input_tokens=66_000_000,
            cost=62.0,
        )
        quota_cycle_5h = client_usage_export.UsageBucket(
            requests=4,
            input_tokens=2_000_000,
            cost=2.0,
        )
        quota_cycle_7d = client_usage_export.UsageBucket(
            requests=40,
            input_tokens=40_000_000,
            cost=38.0,
        )
        quota = {
            label: {
                "window_5h": {
                    "quota_available": True,
                    "remaining_percent": 10.0,
                    "utilization": 90.0,
                    "resets_at": "2026-06-22T14:00:00+08:00",
                },
                "window_7d": {
                    "quota_available": True,
                    "remaining_percent": 17.0,
                    "utilization": 83.0,
                    "resets_at": "2026-06-25T15:00:00+08:00",
                },
            }
        }

        def scan_accounts(_home: Path, start: datetime, _end: datetime):
            return {label: rolling_7d if now - start > timedelta(days=1) else rolling_5h}

        aligned = (
            {label: quota_cycle_5h},
            {label: quota_cycle_7d},
            {},
            {label: now - timedelta(hours=2)},
            {label: now - timedelta(days=4)},
            {},
            {},
        )
        with (
            patch.object(client_usage_export, "cockpit_codex_quota_by_label", return_value=quota),
            patch.object(client_usage_export, "cockpit_codex_speed_by_label", return_value={}),
            patch.object(client_usage_export, "scan_cockpit_codex_accounts", side_effect=scan_accounts),
            patch.object(client_usage_export, "scan_cockpit_codex_quota_windows", return_value=aligned),
            patch.object(client_usage_export, "all_cockpit_codex_account_labels", return_value=[label]),
        ):
            result = client_usage_export.build_codex_window_stats(
                Path("."),
                Path("."),
                now,
                {},
                label,
            )

        window_5h = result[label]["window_5h"]
        window_7d = result[label]["window_7d"]
        self.assertEqual(window_5h["tokens"], 5_000_000)
        self.assertEqual(window_5h["utilization"], 90.0)
        self.assertEqual(window_7d["tokens"], 66_000_000)
        self.assertEqual(window_7d["utilization"], 83.0)
        self.assertTrue(window_7d["start_at"].startswith("2026-06-15T12:00:00"))


class AttributionLedgerTests(unittest.TestCase):
    def test_stable_event_id_wins_when_route_time_changes(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 6, 23, 16, 49, 45),
            request_at=datetime(2026, 6, 23, 16, 2, 46),
            model="gpt-test",
            input_tokens=100,
            cached_tokens=200,
            output_tokens=10,
            session_id="session-1",
        )
        stable_id = client_usage_export.codex_event_id(event)
        legacy_id = client_usage_export.legacy_codex_event_id(event)
        ledger = {
            stable_id: "Codex local - new-account@example.com",
            legacy_id: "Codex local - old-account@example.com",
        }

        attributed = client_usage_export.attribute_codex_events_by_account(
            [event],
            [],
            ledger,
        )

        self.assertIn("Codex local - new-account@example.com", attributed)
        self.assertNotIn("Codex local - old-account@example.com", attributed)

    def test_legacy_event_id_is_migrated_without_losing_attribution(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 6, 23, 16, 49, 45),
            request_at=datetime(2026, 6, 23, 16, 2, 46),
            model="gpt-test",
            input_tokens=100,
            cached_tokens=200,
            output_tokens=10,
            session_id="session-1",
        )
        stable_id = client_usage_export.codex_event_id(event)
        legacy_id = client_usage_export.legacy_codex_event_id(event)
        ledger = {legacy_id: "Codex local - account@example.com"}

        attributed = client_usage_export.attribute_codex_events_by_account(
            [event],
            [],
            ledger,
        )

        self.assertIn("Codex local - account@example.com", attributed)
        self.assertEqual(ledger[stable_id], "Codex local - account@example.com")


if __name__ == "__main__":
    unittest.main()
