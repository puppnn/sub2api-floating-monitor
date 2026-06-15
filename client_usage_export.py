from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


APP_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = APP_DIR / "client_usage_today.json"
CONFIG_PATH = Path(os.environ.get("CLIENT_USAGE_CONFIG") or APP_DIR / "client_usage_config.json")
SPEED_HISTORY_PATH = Path(os.environ.get("CLIENT_USAGE_SPEED_HISTORY") or APP_DIR / "client_usage_speed_history.json")
CODEX_DEFAULT_MODEL = os.environ.get("CLIENT_USAGE_CODEX_DEFAULT_MODEL", "gpt-5.5")
MAX_SINGLE_EVENT_TOKENS = int(os.environ.get("CLIENT_USAGE_MAX_SINGLE_EVENT_TOKENS", "2000000"))
CODEX_ACCOUNT_MATCH_WINDOW_SECONDS = int(os.environ.get("CLIENT_USAGE_CODEX_ACCOUNT_MATCH_WINDOW_SECONDS", "600"))
UNASSIGNED_CODEX_LABEL = os.environ.get("CLIENT_USAGE_UNASSIGNED_CODEX_LABEL", "Unassigned local")
CODEX_FAST_COST_MULTIPLIER = env_float("CLIENT_USAGE_CODEX_FAST_COST_MULTIPLIER", 2.0)
CODEX_FORCE_SPEED = os.environ.get("CLIENT_USAGE_CODEX_FORCE_SPEED", "").strip().lower()
CODEX_SPEED_OVERRIDES = os.environ.get("CLIENT_USAGE_CODEX_SPEED_OVERRIDES", "").strip()
LOCAL_TZ = timezone(timedelta(hours=8))
JSON_DECODER = json.JSONDecoder()
LOG_FIELD_RE = re.compile(r'(?<![A-Za-z0-9_.-])(?P<key>[A-Za-z0-9_.-]+)=(?P<value>"[^"]*"|\S+)')
INTERNAL_SERVICE_TIER_RE = re.compile(
    r'service_tier:\s*Some\((?:Some\()?\"(?P<tier>[^\"]+)\"'
)
PROMPT_CACHE_KEY_RE = re.compile(r'prompt_cache_key:\s*Some\(\"(?P<key>[^\"]+)\"\)')
JSON_PROMPT_CACHE_KEY_RE = re.compile(r'"prompt_cache_key"\s*:\s*"(?P<key>[^"]+)"')
THREAD_ID_RE = re.compile(r'\bthread\.id=(?P<key>[A-Za-z0-9_-]+)')
CONVERSATION_ID_RE = re.compile(r'\bconversation\.id=(?P<key>[A-Za-z0-9_-]+)')


@dataclass
class UsageBucket:
    requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost: float = 0.0
    models: dict[str, int] = field(default_factory=dict)
    latest_at: datetime | None = None
    latest_model: str = ""
    latest_app_speed: str = ""
    latest_cost_multiplier: float | None = None
    latest_speed_badge: str = ""

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cached_input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def add_model(self, model: str, tokens: int) -> None:
        model = (model or "unknown").strip() or "unknown"
        self.models[model] = self.models.get(model, 0) + max(0, int(tokens or 0))

    def mark_latest(
        self,
        when: datetime | None,
        model: str,
        app_speed: str = "",
        cost_multiplier: float | None = None,
    ) -> None:
        if when is None:
            return
        if self.latest_at is None or when > self.latest_at:
            self.latest_at = when
            self.latest_model = (model or "unknown").strip() or "unknown"
            normalized_speed = normalize_codex_speed(app_speed)
            self.latest_app_speed = normalized_speed
            self.latest_cost_multiplier = cost_multiplier
            self.latest_speed_badge = speed_badge(cost_multiplier)


@dataclass
class UsageEvent:
    when: datetime
    model: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    app_speed: str = ""
    cost_multiplier: float | None = None

    @property
    def total_tokens(self) -> int:
        return max(0, self.input_tokens) + max(0, self.cached_tokens) + max(0, self.output_tokens)


@dataclass
class AccountMarker:
    when: datetime
    label: str
    model: str = ""
    kind: str = "request"


@dataclass
class SpeedMarker:
    when: datetime
    speed: str


PRICE_PER_MILLION: list[tuple[str, tuple[float, float, float]]] = [
    ("gpt-5.5", (5.0, 0.5, 30.0)),
    ("gpt-5.4-mini", (0.75, 0.075, 4.5)),
    ("gpt-5.4", (2.5, 0.25, 15.0)),
    ("gpt-5.3", (2.5, 0.25, 15.0)),
    ("gpt-5.2", (2.5, 0.25, 15.0)),
    ("opus", (15.0, 1.5, 75.0)),
    ("sonnet", (3.0, 0.3, 15.0)),
    ("haiku", (0.8, 0.08, 4.0)),
]


def model_price(model: str) -> tuple[float, float, float]:
    name = (model or "").lower()
    for needle, price in PRICE_PER_MILLION:
        if needle in name:
            return price
    return (0.0, 0.0, 0.0)


def estimate_cost(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    input_price, cache_price, output_price = model_price(model)
    return (
        max(0, input_tokens) * input_price
        + max(0, cached_tokens) * cache_price
        + max(0, output_tokens) * output_price
    ) / 1_000_000


def codex_speed_cost_multiplier(speed: str) -> float:
    normalized = (speed or "").strip().lower()
    if normalized in {"fast", "quick", "turbo"}:
        return max(1.0, CODEX_FAST_COST_MULTIPLIER)
    return 1.0


def speed_badge(cost_multiplier: float | None) -> str:
    try:
        multiplier = float(cost_multiplier or 1.0)
    except (TypeError, ValueError):
        multiplier = 1.0
    return f"FAST x{multiplier:g}" if multiplier > 1 else ""


def codex_service_tier_to_speed(service_tier: Any) -> str:
    tier = str(service_tier or "").strip().lower()
    if tier in {"priority", "fast", "flex"}:
        return "fast"
    if tier in {"standard"}:
        return "standard"
    if tier in {"default", "auto", "none", "null", ""}:
        return ""
    return ""


def codex_internal_service_tier_speed(text: str) -> str:
    match = INTERNAL_SERVICE_TIER_RE.search(text or "")
    if not match:
        return ""
    return codex_service_tier_to_speed(match.group("tier"))


def codex_log_request_key(text: str, response: dict[str, Any] | None = None) -> str:
    if response:
        key = str(response.get("prompt_cache_key") or "").strip()
        if key:
            return key
    for pattern in (PROMPT_CACHE_KEY_RE, JSON_PROMPT_CACHE_KEY_RE, CONVERSATION_ID_RE, THREAD_ID_RE):
        match = pattern.search(text or "")
        if match:
            return match.group("key").strip()
    return ""


def codex_model_name(model: str) -> str:
    name = (model or "").strip()
    if not name or name.lower() in {"codex", "unknown"}:
        return CODEX_DEFAULT_MODEL
    return name


def usage_int(usage: dict[str, Any], key: str) -> int:
    try:
        return int(usage.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def add_codex_usage(
    bucket: UsageBucket,
    model: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    when: datetime | None = None,
    cost_multiplier: float = 1.0,
) -> None:
    uncached_input = max(0, input_tokens - max(0, cached_tokens))
    cached_input = max(0, cached_tokens)
    output = max(0, output_tokens)
    total = uncached_input + cached_input + output
    if total <= 0 or total > MAX_SINGLE_EVENT_TOKENS:
        return
    bucket.requests += 1
    bucket.input_tokens += uncached_input
    bucket.cached_input_tokens += cached_input
    bucket.output_tokens += output
    multiplier = max(1.0, cost_multiplier)
    bucket.cost += estimate_cost(model, uncached_input, cached_input, output) * multiplier
    bucket.add_model(model, total)
    bucket.mark_latest(when, model, "fast" if multiplier > 1 else "", multiplier)


def make_codex_event(
    model: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    when: datetime | None,
    app_speed: str = "",
    cost_multiplier: float | None = None,
) -> UsageEvent | None:
    if when is None:
        return None
    uncached_input = max(0, input_tokens - max(0, cached_tokens))
    cached_input = max(0, cached_tokens)
    output = max(0, output_tokens)
    total = uncached_input + cached_input + output
    if total <= 0 or total > MAX_SINGLE_EVENT_TOKENS:
        return None
    return UsageEvent(
        when=when,
        model=codex_model_name(model),
        input_tokens=uncached_input,
        cached_tokens=cached_input,
        output_tokens=output,
        app_speed=normalize_codex_speed(app_speed),
        cost_multiplier=cost_multiplier,
    )


def add_codex_event_to_bucket(bucket: UsageBucket, event: UsageEvent, cost_multiplier: float = 1.0) -> None:
    effective_multiplier = event.cost_multiplier if event.cost_multiplier is not None else cost_multiplier
    effective_multiplier = max(1.0, float(effective_multiplier or 1.0))
    bucket.requests += 1
    bucket.input_tokens += event.input_tokens
    bucket.cached_input_tokens += event.cached_tokens
    bucket.output_tokens += event.output_tokens
    bucket.cost += (
        estimate_cost(event.model, event.input_tokens, event.cached_tokens, event.output_tokens)
        * effective_multiplier
    )
    bucket.add_model(event.model, event.total_tokens)
    event_speed = event.app_speed or ("fast" if effective_multiplier > 1 else "")
    bucket.mark_latest(event.when, event.model, event_speed, effective_multiplier)


def add_bucket(target: UsageBucket, source: UsageBucket) -> None:
    target.requests += source.requests
    target.input_tokens += source.input_tokens
    target.cached_input_tokens += source.cached_input_tokens
    target.output_tokens += source.output_tokens
    target.cache_creation_input_tokens += source.cache_creation_input_tokens
    target.cache_read_input_tokens += source.cache_read_input_tokens
    target.cost += source.cost
    for model, tokens in source.models.items():
        target.models[model] = target.models.get(model, 0) + tokens
    target.mark_latest(
        source.latest_at,
        source.latest_model,
        source.latest_app_speed,
        source.latest_cost_multiplier,
    )


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone(LOCAL_TZ).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def epoch_to_local_datetime(value: Any) -> datetime | None:
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=LOCAL_TZ).replace(tzinfo=None)
    except (OSError, OverflowError, ValueError):
        return None


def parse_json_after_marker(text: str, marker: str) -> dict[str, Any] | None:
    pos = text.find(marker)
    if pos < 0:
        return None
    payload = text[pos + len(marker):].lstrip()
    try:
        value, _ = JSON_DECODER.raw_decode(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict):
        return value
    return None


def parse_log_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in LOG_FIELD_RE.finditer(text):
        value = match.group("value")
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        fields[match.group("key")] = value
    return fields


def field_int(fields: dict[str, str], key: str) -> int:
    try:
        return int(fields.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def codex_event_from_log_fields(text: str, ts: Any) -> UsageEvent | None:
    if "event.kind=response.completed" not in text or "input_token_count=" not in text:
        return None
    fields = parse_log_fields(text)
    input_tokens = field_int(fields, "input_token_count")
    cached_tokens = field_int(fields, "cached_token_count")
    output_tokens = field_int(fields, "output_token_count")
    if input_tokens <= 0 and cached_tokens <= 0 and output_tokens <= 0:
        return None
    when = parse_dt(fields.get("event.timestamp")) or epoch_to_local_datetime(ts)
    app_speed = codex_service_tier_to_speed(fields.get("service_tier"))
    multiplier = codex_speed_cost_multiplier(app_speed) if app_speed else None
    return make_codex_event(
        fields.get("slug") or fields.get("model") or CODEX_DEFAULT_MODEL,
        input_tokens,
        cached_tokens,
        output_tokens,
        when,
        app_speed,
        multiplier,
    )


def codex_fork_replay_cutoff(lines: list[str]) -> datetime | None:
    for line in lines[:20]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") != "session_meta":
            continue
        payload = row.get("payload") or {}
        if payload.get("forked_from_id"):
            started = parse_dt(row.get("timestamp"))
            if started is None:
                return None
            return started + timedelta(seconds=2)
    return None


def iter_recent_jsonl(root: Path, start: datetime) -> list[Path]:
    if not root.exists():
        return []
    paths: list[Path] = []
    seen: set[Path] = set()

    def add_path(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in seen:
            return
        parts = {part.lower() for part in resolved.parts}
        if any(part.startswith("backup-") for part in parts) or ".tmp" in parts:
            return
        seen.add(resolved)
        paths.append(resolved)

    day_dir = root / f"{start.year:04d}" / f"{start.month:02d}" / f"{start.day:02d}"
    if day_dir.exists():
        for path in day_dir.glob("*.jsonl"):
            add_path(path)
    for path in root.rglob("*.jsonl"):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if modified >= start - timedelta(hours=2):
            add_path(path)
    return paths


def scan_codex_events(root: Path, start: datetime, end: datetime) -> list[UsageEvent]:
    events: list[UsageEvent] = []
    seen_events: set[tuple[str, str, int, int, int, int]] = set()
    seen_totals: set[tuple[str, int, int, int, int]] = set()
    for path in iter_recent_jsonl(root, start):
        last_total = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        seen: set[tuple[int, int, int, int]] = set()
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        fork_replay_cutoff = codex_fork_replay_cutoff(lines)
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "event_msg":
                continue
            payload = row.get("payload") or {}
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            total = info.get("total_token_usage") or {}
            ts = parse_dt(row.get("timestamp"))
            current = {
                "input_tokens": usage_int(total, "input_tokens"),
                "cached_input_tokens": usage_int(total, "cached_input_tokens"),
                "output_tokens": usage_int(total, "output_tokens"),
            }
            key = (
                current["input_tokens"],
                current["cached_input_tokens"],
                current["output_tokens"],
                usage_int(total, "reasoning_output_tokens"),
            )
            if key in seen:
                continue
            seen.add(key)
            if ts is None or ts < start:
                last_total = current
                continue
            if ts >= end:
                continue
            model = codex_model_name(str(row.get("model") or payload.get("model") or "codex"))
            total_key = (
                model,
                current["input_tokens"],
                current["cached_input_tokens"],
                current["output_tokens"],
                usage_int(total, "reasoning_output_tokens"),
            )
            if fork_replay_cutoff is not None and ts <= fork_replay_cutoff:
                seen_totals.add(total_key)
                last_total = current
                continue
            if total_key in seen_totals:
                last_total = current
                continue
            seen_totals.add(total_key)
            last_usage = info.get("last_token_usage") or {}
            if last_usage:
                input_tokens = usage_int(last_usage, "input_tokens")
                cached_tokens = usage_int(last_usage, "cached_input_tokens")
                output_tokens = usage_int(last_usage, "output_tokens")
                event_key = (
                    str(row.get("timestamp") or ""),
                    model,
                    input_tokens,
                    cached_tokens,
                    output_tokens,
                    usage_int(last_usage, "reasoning_output_tokens"),
                )
                if event_key not in seen_events:
                    seen_events.add(event_key)
                    event = make_codex_event(model, input_tokens, cached_tokens, output_tokens, ts)
                    if event is not None:
                        events.append(event)
                last_total = current
                continue
            delta_input = current["input_tokens"] - last_total["input_tokens"]
            delta_cached = current["cached_input_tokens"] - last_total["cached_input_tokens"]
            delta_output = current["output_tokens"] - last_total["output_tokens"]
            if delta_input < 0 or delta_cached < 0 or delta_output < 0:
                delta_input = current["input_tokens"]
                delta_cached = current["cached_input_tokens"]
                delta_output = current["output_tokens"]
            if delta_input <= 0 and delta_cached <= 0 and delta_output <= 0:
                continue
            event_key = (
                str(row.get("timestamp") or ""),
                model,
                delta_input,
                delta_cached,
                delta_output,
                usage_int(total, "reasoning_output_tokens"),
            )
            if event_key not in seen_events:
                seen_events.add(event_key)
                event = make_codex_event(model, delta_input, delta_cached, delta_output, ts)
                if event is not None:
                    events.append(event)
            last_total = current
    events.sort(key=lambda event: event.when)
    return events


def scan_codex_logs2_events(home: Path, start: datetime, end: datetime) -> list[UsageEvent]:
    db_path = home / ".codex" / "logs_2.sqlite"
    if not db_path.exists():
        return []

    marker = "Received message "
    start_epoch = int(start.replace(tzinfo=LOCAL_TZ).timestamp()) - 300
    end_epoch = int(end.replace(tzinfo=LOCAL_TZ).timestamp()) + 300
    events: list[UsageEvent] = []
    seen_response_ids: set[str] = set()
    speed_by_request: dict[str, str] = {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = con.execute(
            """
            SELECT ts, feedback_log_body
            FROM logs
            WHERE ts >= ?
              AND ts < ?
              AND (
                feedback_log_body LIKE '%response.completed%'
                OR feedback_log_body LIKE '%service_tier: Some(%'
              )
            ORDER BY ts ASC, ts_nanos ASC
            """,
            (start_epoch, end_epoch),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []

    for ts, body in rows:
        text = str(body or "")
        request_key = codex_log_request_key(text)
        internal_speed = codex_internal_service_tier_speed(text)
        if request_key and internal_speed:
            speed_by_request[request_key] = internal_speed
        message = parse_json_after_marker(text, marker)
        if message is None:
            event = codex_event_from_log_fields(text, ts)
            if event is not None:
                event_key = codex_log_request_key(text)
                if event_key and event_key in speed_by_request:
                    event.app_speed = speed_by_request[event_key]
                    event.cost_multiplier = codex_speed_cost_multiplier(event.app_speed)
            if event is not None:
                events.append(event)
            continue
        if message.get("type") != "response.completed":
            event = codex_event_from_log_fields(text, ts)
            if event is not None:
                event_key = codex_log_request_key(text)
                if event_key and event_key in speed_by_request:
                    event.app_speed = speed_by_request[event_key]
                    event.cost_multiplier = codex_speed_cost_multiplier(event.app_speed)
            if event is not None:
                events.append(event)
            continue
        response = message.get("response") or {}
        if not isinstance(response, dict):
            continue
        response_id = str(response.get("id") or "")
        if response_id and response_id in seen_response_ids:
            continue
        usage = response.get("usage") or {}
        if not isinstance(usage, dict):
            continue
        details = usage.get("input_tokens_details") or {}
        if not isinstance(details, dict):
            details = {}
        when = (
            epoch_to_local_datetime(response.get("completed_at"))
            or epoch_to_local_datetime(response.get("created_at"))
            or epoch_to_local_datetime(ts)
        )
        response_key = codex_log_request_key(text, response)
        app_speed = internal_speed or ""
        if response_key and not app_speed:
            app_speed = speed_by_request.get(response_key, "")
        if not app_speed:
            app_speed = codex_service_tier_to_speed(response.get("service_tier"))
        multiplier = codex_speed_cost_multiplier(app_speed) if app_speed else None
        if when is None or when < start or when >= end:
            continue
        event = make_codex_event(
            str(response.get("model") or CODEX_DEFAULT_MODEL),
            usage_int(usage, "input_tokens"),
            usage_int(details, "cached_tokens"),
            usage_int(usage, "output_tokens"),
            when,
            app_speed,
            multiplier,
        )
        if event is None:
            continue
        if response_id:
            seen_response_ids.add(response_id)
        events.append(event)
    return events


def dedupe_usage_events(events: list[UsageEvent]) -> list[UsageEvent]:
    seen: dict[tuple[int, str, int, int, int], int] = {}
    result: list[UsageEvent] = []
    for event in sorted(events, key=lambda item: item.when):
        key = (
            int(event.when.replace(tzinfo=LOCAL_TZ).timestamp()),
            event.model,
            event.input_tokens,
            event.cached_tokens,
            event.output_tokens,
        )
        if key in seen:
            existing_idx = seen[key]
            existing = result[existing_idx]
            if existing.cost_multiplier is None and event.cost_multiplier is not None:
                result[existing_idx] = event
            continue
        seen[key] = len(result)
        result.append(event)
    return result


def scan_all_codex_events(home: Path, sessions_root: Path, start: datetime, end: datetime) -> list[UsageEvent]:
    events = scan_codex_events(sessions_root, start, end)
    events.extend(scan_codex_logs2_events(home, start, end))
    return dedupe_usage_events(events)


def bucket_from_codex_events(events: list[UsageEvent]) -> UsageBucket:
    bucket = UsageBucket()
    for event in events:
        add_codex_event_to_bucket(bucket, event)
    return bucket


def scan_codex(root: Path, start: datetime, end: datetime) -> UsageBucket:
    return bucket_from_codex_events(scan_codex_events(root, start, end))


def local_epoch_ms(value: datetime) -> int:
    return int(value.replace(tzinfo=LOCAL_TZ).timestamp() * 1000)


def ms_to_local_datetime(value: int | float | str | None) -> datetime | None:
    try:
        millis = int(value or 0)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=LOCAL_TZ).replace(tzinfo=None)


def cockpit_account_label(account_id: str, email: str, api_key_label: str) -> str:
    email = (email or "").strip()
    if email:
        return f"Codex local - {email}"
    api_key_label = (api_key_label or "").strip()
    if api_key_label:
        return f"Codex local - {api_key_label}"
    account_id = (account_id or "").strip()
    if account_id:
        return f"Codex local - {account_id}"
    return "Codex local - Unknown"


def current_codex_account_label(home: Path) -> str:
    codex_dir = home / ".codex"
    for name in (".cockpit_codex_auth.json", "auth.json"):
        path = codex_dir / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        email = str(data.get("email") or data.get("OPENAI_EMAIL") or "").strip()
        if not email:
            tokens = data.get("tokens") if isinstance(data, dict) else None
            if isinstance(tokens, dict):
                email = str(tokens.get("email") or "").strip()
        if email:
            return f"Codex local - {email}"
        api_provider_name = str(data.get("api_provider_name") or "").strip()
        if api_provider_name:
            return f"Codex local - {api_provider_name}"
        api_key_id = str(data.get("api_provider_id") or "").strip()
        if api_key_id:
            return f"Codex local - {api_key_id}"
        account_id = str(data.get("account_id") or "").strip()
        if account_id:
            return f"Codex local - {account_id}"
    return "Codex local"


def all_cockpit_codex_account_labels(home: Path) -> list[str]:
    path = home / ".antigravity_cockpit" / "codex_accounts.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, list):
        return []
    labels: list[str] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        label = cockpit_account_label(
            str(account.get("id") or ""),
            str(account.get("email") or ""),
            str(account.get("api_provider_name") or account.get("name") or ""),
        )
        if label not in labels:
            labels.append(label)
    return labels


def cockpit_codex_account_label_by_id(home: Path) -> dict[str, str]:
    path = home / ".antigravity_cockpit" / "codex_accounts.json"
    labels: dict[str, str] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            data = {}
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if isinstance(accounts, list):
            for account in accounts:
                if not isinstance(account, dict):
                    continue
                account_id = str(account.get("id") or "").strip()
                if not account_id:
                    continue
                labels[account_id] = cockpit_account_label(
                    account_id,
                    str(account.get("email") or ""),
                    str(account.get("api_provider_name") or account.get("name") or ""),
                )

    accounts_dir = home / ".antigravity_cockpit" / "codex_accounts"
    if accounts_dir.exists():
        for path in accounts_dir.glob("*.json*"):
            try:
                account = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            if not isinstance(account, dict):
                continue
            account_id = str(account.get("id") or path.stem).strip()
            if not account_id or account_id in labels:
                continue
            labels[account_id] = cockpit_account_label(
                account_id,
                str(account.get("email") or ""),
                str(account.get("api_provider_name") or account.get("name") or ""),
            )
    return labels


def normalize_codex_speed(speed: Any) -> str:
    value = str(speed or "").strip().lower()
    if value in {"fast", "quick", "turbo"}:
        return "fast"
    if value in {"standard", "normal", "default"}:
        return "standard"
    if value in {"auto", "detect"}:
        return ""
    return value


def codex_speed_meta(speed: str) -> dict[str, Any]:
    normalized = normalize_codex_speed(speed) or "standard"
    multiplier = codex_speed_cost_multiplier(normalized)
    return {
        "app_speed": normalized,
        "cost_multiplier": multiplier,
        "speed_badge": f"FAST x{multiplier:g}" if multiplier > 1 else "",
    }


def load_client_usage_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def parse_speed_overrides(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in (value or "").split(","):
        if "=" not in item:
            continue
        key, speed = item.split("=", 1)
        key = key.strip().lower()
        speed = normalize_codex_speed(speed)
        if key and speed:
            result[key] = speed
    return result


def config_speed_overrides(config: dict[str, Any]) -> dict[str, str]:
    codex_config = config.get("codex") if isinstance(config, dict) else None
    overrides = codex_config.get("speed_overrides") if isinstance(codex_config, dict) else None
    result: dict[str, str] = {}
    if isinstance(overrides, dict):
        for key, speed in overrides.items():
            normalized = normalize_codex_speed(speed)
            if normalized:
                result[str(key).strip().lower()] = normalized
    result.update(parse_speed_overrides(CODEX_SPEED_OVERRIDES))
    return result


def config_current_speed(config: dict[str, Any]) -> str:
    codex_config = config.get("codex") if isinstance(config, dict) else None
    if CODEX_FORCE_SPEED:
        return normalize_codex_speed(CODEX_FORCE_SPEED)
    if isinstance(codex_config, dict):
        return normalize_codex_speed(codex_config.get("current_speed"))
    return ""


def codex_config_service_tier_speed(config_path: Path) -> str:
    if not config_path.exists():
        return ""
    try:
        text = config_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        text = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != "service_tier":
            continue
        tier = value.split("#", 1)[0].strip().strip('"').strip("'").lower()
        if tier in {"priority", "fast"}:
            return "fast"
        if tier in {"standard", "default", "auto", "none", "null", ""}:
            return "standard"
    return "standard"


def file_mtime_local(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def codex_service_tier_speed(home: Path) -> str:
    config_path = home / ".codex" / "config.toml"
    speed = codex_config_service_tier_speed(config_path)
    if speed:
        return speed

    state_path = home / ".codex" / ".codex-global-state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            state = {}
        if isinstance(state, dict):
            tier = str(state.get("default-service-tier") or "").strip().lower()
            if tier in {"priority", "fast"}:
                return "fast"
            if tier in {"standard", "default", "auto", "none", "null", ""}:
                return "standard"
    return ""


def load_speed_history() -> list[SpeedMarker]:
    if not SPEED_HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(SPEED_HISTORY_PATH.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    raw_records = data.get("records") if isinstance(data, dict) else data
    if not isinstance(raw_records, list):
        return []
    records: list[SpeedMarker] = []
    for item in raw_records:
        if not isinstance(item, dict):
            continue
        when = parse_dt(item.get("at"))
        speed = normalize_codex_speed(item.get("speed"))
        if when is not None and speed:
            records.append(SpeedMarker(when, speed))
    records.sort(key=lambda marker: marker.when)
    return records


def save_speed_history(records: list[SpeedMarker]) -> None:
    compact: list[SpeedMarker] = []
    for marker in sorted(records, key=lambda item: item.when):
        if compact and compact[-1].speed == marker.speed:
            continue
        compact.append(marker)
    try:
        SPEED_HISTORY_PATH.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "updated_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
                    "records": [
                        {
                            "at": marker.when.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
                            "speed": marker.speed,
                        }
                        for marker in compact
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def codex_speed_history(home: Path, start: datetime, end: datetime) -> list[SpeedMarker]:
    records = load_speed_history()
    config_path = home / ".codex" / "config.toml"
    backup_path = home / ".codex" / "config.toml.bak"
    current_speed = codex_service_tier_speed(home) or "standard"
    change_at = file_mtime_local(config_path) or datetime.now()
    backup_speed = codex_config_service_tier_speed(backup_path)

    if not records:
        if backup_speed and backup_speed != current_speed and start <= change_at < end:
            records.extend([SpeedMarker(start, backup_speed), SpeedMarker(change_at, current_speed)])
        else:
            records.append(SpeedMarker(start, current_speed))
    else:
        last = records[-1]
        if last.speed != current_speed:
            marker_time = change_at if change_at > last.when else datetime.now()
            records.append(SpeedMarker(marker_time, current_speed))

    if records[0].when > start:
        records.insert(0, SpeedMarker(start, records[0].speed))
    save_speed_history(records)
    return sorted(records, key=lambda marker: marker.when)


def codex_speed_at(markers: list[SpeedMarker], when: datetime | None) -> str:
    if when is None or not markers:
        return ""
    speed = markers[0].speed
    for marker in markers:
        if marker.when <= when:
            speed = marker.speed
        else:
            break
    return speed


def apply_codex_speed_fallback(events: list[UsageEvent], markers: list[SpeedMarker]) -> None:
    for event in events:
        if event.cost_multiplier is not None:
            continue
        speed = codex_speed_at(markers, event.when)
        if not speed:
            continue
        event.app_speed = speed
        event.cost_multiplier = codex_speed_cost_multiplier(speed)


def account_speed_override(
    label: str,
    account: dict[str, Any],
    overrides: dict[str, str],
) -> str:
    keys = {
        label,
        str(account.get("email") or ""),
        str(account.get("id") or ""),
        str(account.get("account_id") or ""),
        str(account.get("api_provider_name") or ""),
        str(account.get("name") or ""),
    }
    for key in keys:
        override = overrides.get(key.strip().lower())
        if override:
            return override
    return ""


def cockpit_codex_speed_by_label(home: Path) -> dict[str, dict[str, Any]]:
    accounts_dir = home / ".antigravity_cockpit" / "codex_accounts"
    config = load_client_usage_config()
    overrides = config_speed_overrides(config)
    forced_current_speed = config_current_speed(config)
    detected_current_speed = codex_service_tier_speed(home)
    # Codex service_tier is a global client mode, not a per-account setting.
    # Apply it to every local Codex account unless a user override exists.
    current_speed = forced_current_speed or detected_current_speed

    result: dict[str, dict[str, Any]] = {}
    if accounts_dir.exists():
        for path in accounts_dir.glob("*.json"):
            try:
                account = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            if not isinstance(account, dict):
                continue
            label = cockpit_account_label(
                str(account.get("id") or path.stem),
                str(account.get("email") or ""),
                str(account.get("api_provider_name") or account.get("name") or ""),
            )
            speed = normalize_codex_speed(account.get("app_speed")) or "standard"
            if current_speed:
                speed = current_speed
            override = account_speed_override(label, account, overrides)
            if override:
                speed = override
            result[label] = codex_speed_meta(speed)
    return result


def epoch_seconds_to_local_iso(value: Any) -> str:
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    try:
        return datetime.fromtimestamp(seconds, tz=LOCAL_TZ).isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""


def quota_window_payload(
    percent_remaining: Any,
    reset_at: Any,
    stale: bool,
    window_minutes: int | None = None,
) -> dict[str, Any]:
    window: dict[str, Any] = {
        "quota_available": percent_remaining is not None,
        "quota_stale": stale,
        "resets_at": epoch_seconds_to_local_iso(reset_at),
    }
    if window_minutes:
        window["window_minutes"] = int(window_minutes)
        window["window_days"] = round(float(window_minutes) / (24 * 60), 1)
    if percent_remaining is not None:
        try:
            remaining = max(0.0, min(100.0, float(percent_remaining)))
            window["remaining_percent"] = remaining
            window["utilization"] = 100.0 - remaining
        except (TypeError, ValueError):
            window["quota_available"] = False
    return window


def cockpit_codex_quota_by_label(home: Path) -> dict[str, dict[str, dict[str, Any]]]:
    accounts_dir = home / ".antigravity_cockpit" / "codex_accounts"
    if not accounts_dir.exists():
        return {}

    result: dict[str, dict[str, dict[str, Any]]] = {}
    for path in accounts_dir.glob("*.json"):
        try:
            account = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        if not isinstance(account, dict):
            continue
        quota = account.get("quota")
        if not isinstance(quota, dict):
            continue

        label = cockpit_account_label(
            str(account.get("id") or path.stem),
            str(account.get("email") or ""),
            str(account.get("api_provider_name") or account.get("name") or ""),
        )
        stale = bool(account.get("quota_error"))
        five_hour_value = quota.get("hourly_percentage")
        weekly_value = quota.get("weekly_percentage")
        weekly_available = bool(quota.get("weekly_window_present")) and weekly_value is not None
        try:
            hourly_window_minutes = int(quota.get("hourly_window_minutes") or 5 * 60)
        except (TypeError, ValueError):
            hourly_window_minutes = 5 * 60
        is_cycle_window = hourly_window_minutes > 7 * 24 * 60

        if is_cycle_window:
            five_hour = {"quota_available": False, "quota_stale": stale}
            cycle = quota_window_payload(
                five_hour_value,
                quota.get("hourly_reset_time"),
                stale,
                hourly_window_minutes,
            )
        else:
            five_hour = quota_window_payload(
                five_hour_value,
                quota.get("hourly_reset_time"),
                stale,
                hourly_window_minutes,
            )
            cycle = {"quota_available": False, "quota_stale": stale}

        seven_day = quota_window_payload(
            weekly_value if weekly_available else None,
            quota.get("weekly_reset_time"),
            stale,
            7 * 24 * 60 if weekly_available else None,
        )

        result[label] = {
            "window_5h": five_hour,
            "window_7d": seven_day,
            "window_cycle": cycle,
        }
    return result


def quota_window_start(
    window: dict[str, Any],
    now: datetime,
    duration: timedelta,
) -> datetime | None:
    if not window.get("quota_available") or window.get("quota_stale"):
        return None
    reset_at = parse_dt(window.get("resets_at"))
    if reset_at is None or reset_at <= now or reset_at > now + duration:
        return None
    start_at = reset_at - duration
    return start_at if start_at <= now else None


def add_cockpit_usage_to_bucket(
    bucket: UsageBucket,
    timestamp: Any,
    model: Any,
    input_tokens: Any,
    output_tokens: Any,
    total_tokens: Any,
    cached_tokens: Any,
    estimated_cost_usd: Any,
    cost_multiplier: float = 1.0,
    app_speed: str = "",
) -> bool:
    total_tokens = max(0, int(total_tokens or 0))
    input_tokens = max(0, int(input_tokens or 0))
    output_tokens = max(0, int(output_tokens or 0))
    cached_tokens = max(0, int(cached_tokens or 0))
    if total_tokens <= 0 and input_tokens <= 0 and output_tokens <= 0 and cached_tokens <= 0:
        return False
    model = codex_model_name(str(model or "codex"))
    bucket.requests += 1
    bucket.input_tokens += max(0, input_tokens - cached_tokens)
    bucket.cached_input_tokens += cached_tokens
    bucket.output_tokens += output_tokens
    event_total = total_tokens or (input_tokens + output_tokens + cached_tokens)
    try:
        cost = float(estimated_cost_usd or 0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost <= 0:
        cost = estimate_cost(model, max(0, input_tokens - cached_tokens), cached_tokens, output_tokens)
    multiplier = max(1.0, cost_multiplier)
    bucket.cost += cost * multiplier
    bucket.add_model(model, event_total)
    bucket.mark_latest(ms_to_local_datetime(timestamp), model, app_speed, multiplier)
    return True


def scan_cockpit_codex_accounts(root: Path, start: datetime, end: datetime) -> dict[str, UsageBucket]:
    db_path = root / ".antigravity_cockpit" / "codex_local_access_logs.sqlite"
    if not db_path.exists():
        return {}
    start_ms = local_epoch_ms(start)
    end_ms = local_epoch_ms(end)
    speed_by_label = cockpit_codex_speed_by_label(root)
    speed_markers = codex_speed_history(root, start, end)
    buckets: dict[str, UsageBucket] = {}
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute(
            """
            SELECT
                timestamp,
                account_id,
                email,
                api_key_label,
                model_id,
                input_tokens,
                output_tokens,
                total_tokens,
                cached_tokens,
                estimated_cost_usd
            FROM request_logs
            WHERE timestamp >= ? AND timestamp < ?
            """,
            (start_ms, end_ms),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return {}

    for row in rows:
        (
            timestamp,
            account_id,
            email,
            api_key_label,
            model,
            input_tokens,
            output_tokens,
            total_tokens,
            cached_tokens,
            estimated_cost_usd,
        ) = row
        label = cockpit_account_label(str(account_id or ""), str(email or ""), str(api_key_label or ""))
        when = ms_to_local_datetime(timestamp)
        app_speed = codex_speed_at(speed_markers, when)
        if not app_speed:
            app_speed = str((speed_by_label.get(label) or {}).get("app_speed") or "")
        multiplier = codex_speed_cost_multiplier(app_speed)
        bucket = buckets.setdefault(label, UsageBucket())
        add_cockpit_usage_to_bucket(
            bucket,
            timestamp,
            model,
            input_tokens,
            output_tokens,
            total_tokens,
            cached_tokens,
            estimated_cost_usd,
            multiplier,
            app_speed,
        )
    return buckets


def scan_cockpit_codex_quota_windows(
    root: Path,
    quota_by_account: dict[str, dict[str, dict[str, Any]]],
    now: datetime,
    end: datetime,
) -> tuple[
    dict[str, UsageBucket],
    dict[str, UsageBucket],
    dict[str, UsageBucket],
    dict[str, datetime],
    dict[str, datetime],
    dict[str, datetime],
    dict[str, datetime],
]:
    db_path = root / ".antigravity_cockpit" / "codex_local_access_logs.sqlite"
    if not db_path.exists():
        return {}, {}, {}, {}, {}, {}, {}

    starts_5h: dict[str, datetime] = {}
    starts_7d: dict[str, datetime] = {}
    starts_cycle: dict[str, datetime] = {}
    for label, quota in quota_by_account.items():
        start_5h = quota_window_start(quota.get("window_5h") or {}, now, timedelta(hours=5))
        start_7d = quota_window_start(quota.get("window_7d") or {}, now, timedelta(days=7))
        cycle_window = quota.get("window_cycle") or {}
        try:
            cycle_minutes = int(cycle_window.get("window_minutes") or 0)
        except (TypeError, ValueError):
            cycle_minutes = 0
        start_cycle = (
            quota_window_start(cycle_window, now, timedelta(minutes=cycle_minutes))
            if cycle_minutes > 0
            else None
        )
        if start_5h is not None:
            starts_5h[label] = start_5h
        if start_7d is not None:
            starts_7d[label] = start_7d
        if start_cycle is not None:
            starts_cycle[label] = start_cycle
    all_starts = list(starts_5h.values()) + list(starts_7d.values()) + list(starts_cycle.values())
    if not all_starts:
        return {}, {}, {}, starts_5h, starts_7d, starts_cycle, {}

    speed_by_label = cockpit_codex_speed_by_label(root)
    speed_markers = codex_speed_history(root, min(all_starts), end)
    buckets_5h = {label: UsageBucket() for label in starts_5h}
    buckets_7d = {label: UsageBucket() for label in starts_7d}
    buckets_cycle = {label: UsageBucket() for label in starts_cycle}
    latest_by_label: dict[str, datetime] = {}
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute(
            """
            SELECT
                timestamp,
                account_id,
                email,
                api_key_label,
                model_id,
                input_tokens,
                output_tokens,
                total_tokens,
                cached_tokens,
                estimated_cost_usd
            FROM request_logs
            WHERE timestamp >= ? AND timestamp < ?
            """,
            (local_epoch_ms(min(all_starts)), local_epoch_ms(end)),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return {}, {}, {}, starts_5h, starts_7d, starts_cycle, {}

    for row in rows:
        (
            timestamp,
            account_id,
            email,
            api_key_label,
            model,
            input_tokens,
            output_tokens,
            total_tokens,
            cached_tokens,
            estimated_cost_usd,
        ) = row
        label = cockpit_account_label(str(account_id or ""), str(email or ""), str(api_key_label or ""))
        when = ms_to_local_datetime(timestamp)
        if when is None:
            continue
        previous_latest = latest_by_label.get(label)
        if previous_latest is None or when > previous_latest:
            latest_by_label[label] = when
        app_speed = codex_speed_at(speed_markers, when)
        if not app_speed:
            app_speed = str((speed_by_label.get(label) or {}).get("app_speed") or "")
        multiplier = codex_speed_cost_multiplier(app_speed)
        if label in starts_5h and when >= starts_5h[label]:
            add_cockpit_usage_to_bucket(
                buckets_5h[label],
                timestamp,
                model,
                input_tokens,
                output_tokens,
                total_tokens,
                cached_tokens,
                estimated_cost_usd,
                multiplier,
                app_speed,
            )
        if label in starts_7d and when >= starts_7d[label]:
            add_cockpit_usage_to_bucket(
                buckets_7d[label],
                timestamp,
                model,
                input_tokens,
                output_tokens,
                total_tokens,
                cached_tokens,
                estimated_cost_usd,
                multiplier,
                app_speed,
            )
        if label in starts_cycle and when >= starts_cycle[label]:
            add_cockpit_usage_to_bucket(
                buckets_cycle[label],
                timestamp,
                model,
                input_tokens,
                output_tokens,
                total_tokens,
                cached_tokens,
                estimated_cost_usd,
                multiplier,
                app_speed,
            )
    return buckets_5h, buckets_7d, buckets_cycle, starts_5h, starts_7d, starts_cycle, latest_by_label


def scan_cockpit_codex_account_markers(root: Path, start: datetime, end: datetime) -> list[AccountMarker]:
    db_path = root / ".antigravity_cockpit" / "codex_local_access_logs.sqlite"
    if not db_path.exists():
        return []
    start_ms = local_epoch_ms(start)
    end_ms = local_epoch_ms(end)
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute(
            """
            SELECT
                timestamp,
                account_id,
                email,
                api_key_label,
                model_id
            FROM request_logs
            WHERE timestamp >= ? AND timestamp < ?
            ORDER BY timestamp ASC
            """,
            (start_ms, end_ms),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []

    markers: list[AccountMarker] = []
    for timestamp, account_id, email, api_key_label, model in rows:
        when = ms_to_local_datetime(timestamp)
        if when is None:
            continue
        label = cockpit_account_label(str(account_id or ""), str(email or ""), str(api_key_label or ""))
        if label == "Codex local - Unknown":
            continue
        markers.append(AccountMarker(when=when, label=label, model=codex_model_name(str(model or "codex")), kind="request"))
    return markers


SWITCH_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T[^\s]+)\s+.*?\[Codex[^\]]+\].*?account_id=(?P<account_id>[^,\s]+)"
)


def parse_local_log_dt(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is not None:
            return dt.astimezone(LOCAL_TZ).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def scan_cockpit_codex_switch_markers(root: Path, start: datetime, end: datetime) -> list[AccountMarker]:
    logs_dir = root / ".antigravity_cockpit" / "logs"
    if not logs_dir.exists():
        return []
    labels = cockpit_codex_account_label_by_id(root)
    markers: list[AccountMarker] = []
    scan_start = start - timedelta(days=7)
    for path in sorted(logs_dir.glob("app.log*")):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if modified < scan_start:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            match = SWITCH_LOG_RE.search(line)
            if not match:
                continue
            when = parse_local_log_dt(match.group("ts"))
            if when is None or when >= end:
                continue
            account_id = match.group("account_id").strip()
            label = labels.get(account_id) or cockpit_account_label(account_id, "", "")
            markers.append(AccountMarker(when=when, label=label, model=CODEX_DEFAULT_MODEL, kind="switch"))

    markers.sort(key=lambda marker: marker.when)
    if markers:
        last_before_start = None
        in_range: list[AccountMarker] = []
        for marker in markers:
            if marker.when < start:
                last_before_start = marker
            elif marker.when < end:
                in_range.append(marker)
        if last_before_start is not None:
            in_range.insert(0, AccountMarker(when=start, label=last_before_start.label, model=last_before_start.model, kind="switch"))
        return in_range
    return []


def attribute_codex_events_to_account_markers(
    events: list[UsageEvent],
    markers: list[AccountMarker],
    cost_multiplier_by_label: dict[str, float] | None = None,
) -> dict[str, UsageBucket]:
    attributed = attribute_codex_events_by_account(events, markers)
    multipliers = cost_multiplier_by_label or {}
    buckets: dict[str, UsageBucket] = {}
    for label, account_events in attributed.items():
        bucket = buckets.setdefault(label, UsageBucket())
        multiplier = float(multipliers.get(label) or 1.0)
        for event in account_events:
            add_codex_event_to_bucket(bucket, event, multiplier)
    return buckets


def latest_marker_by_label(markers: list[AccountMarker]) -> dict[str, datetime]:
    latest: dict[str, datetime] = {}
    for marker in markers:
        if marker.kind != "request":
            continue
        previous = latest.get(marker.label)
        if previous is None or marker.when > previous:
            latest[marker.label] = marker.when
    return latest


def attribute_codex_events_by_account(
    events: list[UsageEvent],
    markers: list[AccountMarker],
) -> dict[str, list[UsageEvent]]:
    attributed: dict[str, list[UsageEvent]] = {}
    if not events:
        return attributed
    if not markers:
        account_events = attributed.setdefault(UNASSIGNED_CODEX_LABEL, [])
        for event in events:
            account_events.append(event)
        return attributed

    markers = sorted(markers, key=lambda marker: marker.when)
    switch_markers = [marker for marker in markers if marker.kind == "switch"]
    switch_times = [marker.when for marker in switch_markers]
    request_markers = [marker for marker in markers if marker.kind != "switch"]
    request_times = [marker.when for marker in request_markers]
    for event in events:
        label = ""
        if switch_markers:
            switch_pos = bisect_right(switch_times, event.when) - 1
            if switch_pos >= 0:
                label = switch_markers[switch_pos].label
        if not label:
            pos = bisect_left(request_times, event.when)
            best_marker: AccountMarker | None = None
            best_delta = float("inf")
            for idx in (pos - 1, pos):
                if idx < 0 or idx >= len(request_markers):
                    continue
                delta = abs((event.when - request_markers[idx].when).total_seconds())
                if delta < best_delta:
                    best_delta = delta
                    best_marker = request_markers[idx]
            label = (
                best_marker.label
                if best_marker is not None and best_delta <= CODEX_ACCOUNT_MATCH_WINDOW_SECONDS
                else UNASSIGNED_CODEX_LABEL
            )
        attributed.setdefault(label, []).append(event)
    return attributed


def scan_claude(root: Path, start: datetime, end: datetime) -> UsageBucket:
    bucket = UsageBucket()
    for path in iter_recent_jsonl(root, start):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = parse_dt(row.get("timestamp"))
            if ts is None or ts < start or ts >= end:
                continue
            message = row.get("message") or {}
            if message.get("role") != "assistant":
                continue
            usage = message.get("usage") or {}
            if not usage:
                continue
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            total = input_tokens + output_tokens + cache_creation + cache_read
            if total <= 0:
                continue
            model = str(message.get("model") or row.get("model") or "claude")
            bucket.requests += 1
            bucket.input_tokens += input_tokens
            bucket.output_tokens += output_tokens
            bucket.cache_creation_input_tokens += cache_creation
            bucket.cache_read_input_tokens += cache_read
            bucket.cost += estimate_cost(model, input_tokens + cache_creation, cache_read, output_tokens)
            bucket.add_model(model, total)
            bucket.mark_latest(ts, model)
    return bucket


def latest_at_text(bucket: UsageBucket) -> str:
    if bucket.latest_at is None:
        return ""
    return bucket.latest_at.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")


def bucket_to_dict(name: str, bucket: UsageBucket, show_zero: bool = False) -> dict[str, Any]:
    result = {
        "name": name,
        "requests": bucket.requests,
        "tokens": bucket.total_tokens,
        "input_tokens": bucket.input_tokens,
        "cached_input_tokens": bucket.cached_input_tokens + bucket.cache_read_input_tokens,
        "cache_creation_input_tokens": bucket.cache_creation_input_tokens,
        "output_tokens": bucket.output_tokens,
        "cost": round(bucket.cost, 6),
        "models": dict(sorted(bucket.models.items(), key=lambda item: item[1], reverse=True)[:8]),
        "latest_at": latest_at_text(bucket),
        "latest_model": bucket.latest_model,
        "show_zero": show_zero,
    }
    if bucket.latest_app_speed:
        result.update(
            {
                "app_speed": bucket.latest_app_speed,
                "cost_multiplier": float(bucket.latest_cost_multiplier or 1.0),
                "speed_badge": bucket.latest_speed_badge,
            }
        )
    return result


def bucket_to_window_dict(bucket: UsageBucket, start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "requests": bucket.requests,
        "tokens": bucket.total_tokens,
        "cost": round(bucket.cost, 6),
        "start_at": start.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
        "end_at": end.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
    }


def build_codex_window_stats(
    home: Path,
    sessions_root: Path,
    now: datetime,
) -> dict[str, dict[str, dict[str, Any]]]:
    window_end = now + timedelta(seconds=1)
    window_5h_start = now - timedelta(hours=5)
    window_7d_start = now - timedelta(days=7)

    quota_by_account = cockpit_codex_quota_by_label(home)
    speed_by_account = cockpit_codex_speed_by_label(home)
    cost_multiplier_by_label = {
        label: float(meta.get("cost_multiplier") or 1.0)
        for label, meta in speed_by_account.items()
    }
    direct_7d = scan_cockpit_codex_accounts(home, window_7d_start, window_end)
    direct_total = UsageBucket()
    for bucket in direct_7d.values():
        add_bucket(direct_total, bucket)

    buckets_5h: dict[str, UsageBucket]
    buckets_7d: dict[str, UsageBucket]
    if direct_total.total_tokens > 0 or direct_total.requests > 0:
        buckets_7d = direct_7d
        buckets_5h = scan_cockpit_codex_accounts(home, window_5h_start, window_end)
    else:
        speed_markers = codex_speed_history(home, window_7d_start, window_end)
        events_7d = scan_all_codex_events(home, sessions_root, window_7d_start, window_end)
        apply_codex_speed_fallback(events_7d, speed_markers)
        markers_7d = scan_cockpit_codex_switch_markers(home, window_7d_start, window_end)
        markers_7d.extend(scan_cockpit_codex_account_markers(home, window_7d_start, window_end))
        buckets_7d = attribute_codex_events_to_account_markers(
            events_7d,
            markers_7d,
            cost_multiplier_by_label,
        )

        events_5h = scan_all_codex_events(home, sessions_root, window_5h_start, window_end)
        apply_codex_speed_fallback(events_5h, speed_markers)
        markers_5h = scan_cockpit_codex_switch_markers(home, window_5h_start, window_end)
        markers_5h.extend(scan_cockpit_codex_account_markers(home, window_5h_start, window_end))
        buckets_5h = attribute_codex_events_to_account_markers(
            events_5h,
            markers_5h,
            cost_multiplier_by_label,
        )

    (
        aligned_5h,
        aligned_7d,
        aligned_cycle,
        aligned_starts_5h,
        aligned_starts_7d,
        aligned_starts_cycle,
        direct_latest,
    ) = scan_cockpit_codex_quota_windows(
        home,
        quota_by_account,
        now,
        window_end,
    )
    aligned_starts = (
        list(aligned_starts_5h.values())
        + list(aligned_starts_7d.values())
        + list(aligned_starts_cycle.values())
    )
    if aligned_starts:
        aligned_scan_start = min(aligned_starts)
        aligned_events = scan_all_codex_events(home, sessions_root, aligned_scan_start, window_end)
        speed_markers = codex_speed_history(home, aligned_scan_start, window_end)
        apply_codex_speed_fallback(aligned_events, speed_markers)
        aligned_markers = scan_cockpit_codex_switch_markers(home, aligned_scan_start, window_end)
        aligned_markers.extend(scan_cockpit_codex_account_markers(home, aligned_scan_start, window_end))
        attributed_events = attribute_codex_events_by_account(aligned_events, aligned_markers)
        for label, account_events in attributed_events.items():
            direct_cutoff = direct_latest.get(label)
            if direct_cutoff is not None:
                direct_cutoff += timedelta(seconds=2)
            for event in account_events:
                if direct_cutoff is not None and event.when <= direct_cutoff:
                    continue
                multiplier = cost_multiplier_by_label.get(label, 1.0)
                if label in aligned_starts_5h and event.when >= aligned_starts_5h[label]:
                    add_codex_event_to_bucket(aligned_5h[label], event, multiplier)
                if label in aligned_starts_7d and event.when >= aligned_starts_7d[label]:
                    add_codex_event_to_bucket(aligned_7d[label], event, multiplier)
                if label in aligned_starts_cycle and event.when >= aligned_starts_cycle[label]:
                    add_codex_event_to_bucket(aligned_cycle[label], event, multiplier)
    buckets_5h.update(aligned_5h)
    buckets_7d.update(aligned_7d)
    buckets_cycle = aligned_cycle

    result: dict[str, dict[str, dict[str, Any]]] = {}
    labels = (
        set(buckets_5h)
        | set(buckets_7d)
        | set(buckets_cycle)
        | set(all_cockpit_codex_account_labels(home))
        | set(quota_by_account)
    )
    for label in labels:
        window_5h = bucket_to_window_dict(
            buckets_5h.get(label, UsageBucket()),
            aligned_starts_5h.get(label, window_5h_start),
            now,
        )
        window_7d = bucket_to_window_dict(
            buckets_7d.get(label, UsageBucket()),
            aligned_starts_7d.get(label, window_7d_start),
            now,
        )
        window_cycle = bucket_to_window_dict(
            buckets_cycle.get(label, UsageBucket()),
            aligned_starts_cycle.get(label, now),
            now,
        )
        quota = quota_by_account.get(label) or {}
        window_5h.update(quota.get("window_5h") or {})
        window_7d.update(quota.get("window_7d") or {})
        window_cycle.update(quota.get("window_cycle") or {})
        result[label] = {
            "window_5h": window_5h,
            "window_7d": window_7d,
            "window_cycle": window_cycle,
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Export local Claude/Codex client token usage for Sub2API monitor.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--date", default="")
    args = parser.parse_args()

    now = datetime.now()
    if args.date:
        day = datetime.fromisoformat(args.date).date()
    else:
        day = now.date()
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)

    home = Path(os.path.expanduser("~"))
    codex_sessions_root = home / ".codex" / "sessions"
    speed_by_account = cockpit_codex_speed_by_label(home)
    cost_multiplier_by_label = {
        label: float(meta.get("cost_multiplier") or 1.0)
        for label, meta in speed_by_account.items()
    }
    codex_events = scan_all_codex_events(home, codex_sessions_root, start, end)
    speed_markers = codex_speed_history(home, start, end)
    apply_codex_speed_fallback(codex_events, speed_markers)
    codex_jsonl = bucket_from_codex_events(codex_events)
    codex_accounts = scan_cockpit_codex_accounts(home, start, end)
    markers = scan_cockpit_codex_switch_markers(home, start, end)
    account_markers = scan_cockpit_codex_account_markers(home, start, end)
    markers.extend(account_markers)
    direct_latest = latest_marker_by_label(account_markers)
    attributed = attribute_codex_events_to_account_markers(
        codex_events,
        markers,
        cost_multiplier_by_label,
    )
    if codex_accounts:
        for label, bucket in attributed.items():
            cutoff = direct_latest.get(label)
            filtered = UsageBucket()
            for event in attribute_codex_events_by_account(codex_events, markers).get(label, []):
                if cutoff is not None and event.when <= cutoff + timedelta(seconds=2):
                    continue
                add_codex_event_to_bucket(
                    filtered,
                    event,
                    float(cost_multiplier_by_label.get(label) or 1.0),
                )
            if filtered.requests or filtered.total_tokens or filtered.cost:
                add_bucket(codex_accounts.setdefault(label, UsageBucket()), filtered)
    codex = UsageBucket()
    for bucket in codex_accounts.values():
        add_bucket(codex, bucket)
    if codex.total_tokens <= 0 and codex.requests <= 0:
        codex = UsageBucket()
        for bucket in attributed.values():
            add_bucket(codex, bucket)
        codex_provider_buckets = sorted(
            attributed.items(),
            key=lambda item: (-item[1].total_tokens, -item[1].requests, item[0]),
        )
    else:
        codex_provider_buckets = sorted(
            codex_accounts.items(),
            key=lambda item: (-item[1].total_tokens, -item[1].requests, item[0]),
        )
    codex_provider_map = {name: bucket for name, bucket in codex_provider_buckets}
    for label in all_cockpit_codex_account_labels(home):
        codex_provider_map.setdefault(label, UsageBucket())
    codex_provider_buckets = sorted(
        codex_provider_map.items(),
        key=lambda item: (-item[1].total_tokens, -item[1].requests, item[0]),
    )

    claude = scan_claude(home / ".claude" / "projects", start, end)
    window_stats_by_account: dict[str, dict[str, dict[str, Any]]] = {}
    if day == now.date():
        window_stats_by_account = build_codex_window_stats(home, codex_sessions_root, now)

    codex_providers = []
    for name, bucket in codex_provider_buckets:
        provider = bucket_to_dict(name, bucket, show_zero=True)
        for key, value in speed_by_account.get(name, {}).items():
            if key not in provider or provider.get(key) in {"", None}:
                provider[key] = value
        if "@" in name:
            provider.update(window_stats_by_account.get(name, {}))
        codex_providers.append(provider)
    providers = codex_providers + [bucket_to_dict("Claude local", claude)]
    total = UsageBucket()
    for bucket in (codex, claude):
        total.requests += bucket.requests
        total.input_tokens += bucket.input_tokens
        total.cached_input_tokens += bucket.cached_input_tokens
        total.cache_creation_input_tokens += bucket.cache_creation_input_tokens
        total.cache_read_input_tokens += bucket.cache_read_input_tokens
        total.output_tokens += bucket.output_tokens
        total.cost += bucket.cost
        total.mark_latest(bucket.latest_at, bucket.latest_model)

    latest_provider = ""
    latest_model = ""
    latest_at = ""
    latest_dt: datetime | None = None
    latest_candidates = list(codex_provider_buckets) + [("Claude local", claude)]
    for provider_name, bucket in latest_candidates:
        if bucket.latest_at is None:
            continue
        if latest_dt is None or bucket.latest_at > latest_dt:
            latest_dt = bucket.latest_at
            latest_provider = provider_name
            latest_model = bucket.latest_model
            latest_at = latest_at_text(bucket)

    output = {
        "schema": 1,
        "source": "client-jsonl",
        "updated_at": now.isoformat(timespec="seconds"),
        "date": day.isoformat(),
        "today": bucket_to_dict("Client local", total),
        "providers": providers,
        "latest_request": {
            "provider": latest_provider,
            "model": latest_model,
            "created_at": latest_at,
            "kind": "success" if latest_at else "",
        },
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output["today"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
