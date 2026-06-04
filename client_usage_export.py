from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = APP_DIR / "client_usage_today.json"
CODEX_DEFAULT_MODEL = os.environ.get("CLIENT_USAGE_CODEX_DEFAULT_MODEL", "gpt-5.5")
MAX_SINGLE_EVENT_TOKENS = int(os.environ.get("CLIENT_USAGE_MAX_SINGLE_EVENT_TOKENS", "2000000"))
LOCAL_TZ = timezone(timedelta(hours=8))


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

    def mark_latest(self, when: datetime | None, model: str) -> None:
        if when is None:
            return
        if self.latest_at is None or when > self.latest_at:
            self.latest_at = when
            self.latest_model = (model or "unknown").strip() or "unknown"


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
    bucket.cost += estimate_cost(model, uncached_input, cached_input, output)
    bucket.add_model(model, total)
    bucket.mark_latest(when, model)


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
    target.mark_latest(source.latest_at, source.latest_model)


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
    day_dir = root / f"{start.year:04d}" / f"{start.month:02d}" / f"{start.day:02d}"
    if day_dir.exists():
        return list(day_dir.glob("*.jsonl"))
    paths: list[Path] = []
    for path in root.rglob("*.jsonl"):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if modified >= start - timedelta(hours=2):
            paths.append(path)
    return paths


def scan_codex(root: Path, start: datetime, end: datetime) -> UsageBucket:
    bucket = UsageBucket()
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
                    add_codex_usage(bucket, model, input_tokens, cached_tokens, output_tokens, ts)
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
                add_codex_usage(bucket, model, delta_input, delta_cached, delta_output, ts)
            last_total = current
    return bucket


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


def scan_cockpit_codex_accounts(root: Path, start: datetime, end: datetime) -> dict[str, UsageBucket]:
    db_path = root / ".antigravity_cockpit" / "codex_local_access_logs.sqlite"
    if not db_path.exists():
        return {}
    start_ms = local_epoch_ms(start)
    end_ms = local_epoch_ms(end)
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
        total_tokens = max(0, int(total_tokens or 0))
        input_tokens = max(0, int(input_tokens or 0))
        output_tokens = max(0, int(output_tokens or 0))
        cached_tokens = max(0, int(cached_tokens or 0))
        if total_tokens <= 0 and input_tokens <= 0 and output_tokens <= 0 and cached_tokens <= 0:
            continue
        model = codex_model_name(str(model or "codex"))
        label = cockpit_account_label(str(account_id or ""), str(email or ""), str(api_key_label or ""))
        bucket = buckets.setdefault(label, UsageBucket())
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
        bucket.cost += cost
        bucket.add_model(model, event_total)
        bucket.mark_latest(ms_to_local_datetime(timestamp), model)
    return buckets


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


def bucket_to_dict(name: str, bucket: UsageBucket) -> dict[str, Any]:
    return {
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
    }


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
    codex_jsonl = scan_codex(home / ".codex" / "sessions", start, end)
    codex_accounts = scan_cockpit_codex_accounts(home, start, end)
    codex = UsageBucket()
    for bucket in codex_accounts.values():
        add_bucket(codex, bucket)
    if codex.total_tokens <= 0 and codex.requests <= 0:
        codex = codex_jsonl
        codex_provider_buckets = [(current_codex_account_label(home), codex)]
    else:
        codex_provider_buckets = sorted(
            codex_accounts.items(),
            key=lambda item: (-item[1].total_tokens, -item[1].requests, item[0]),
        )
    claude = scan_claude(home / ".claude" / "projects", start, end)
    codex_providers = [bucket_to_dict(name, bucket) for name, bucket in codex_provider_buckets]
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
