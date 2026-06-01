from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = APP_DIR / "client_usage_today.json"
CODEX_DEFAULT_MODEL = os.environ.get("CLIENT_USAGE_CODEX_DEFAULT_MODEL", "gpt-5.5")
MAX_SINGLE_EVENT_TOKENS = int(os.environ.get("CLIENT_USAGE_MAX_SINGLE_EVENT_TOKENS", "2000000"))


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


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
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
    paths: list[Path] = []
    for path in root.rglob("*.jsonl"):
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
                    add_codex_usage(bucket, model, input_tokens, cached_tokens, output_tokens)
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
                add_codex_usage(bucket, model, delta_input, delta_cached, delta_output)
            last_total = current
    return bucket


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
    return bucket


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
    codex = scan_codex(home / ".codex" / "sessions", start, end)
    claude = scan_claude(home / ".claude" / "projects", start, end)
    providers = [bucket_to_dict("Codex local", codex), bucket_to_dict("Claude local", claude)]
    total = UsageBucket()
    for bucket in (codex, claude):
        total.requests += bucket.requests
        total.input_tokens += bucket.input_tokens
        total.cached_input_tokens += bucket.cached_input_tokens
        total.cache_creation_input_tokens += bucket.cache_creation_input_tokens
        total.cache_read_input_tokens += bucket.cache_read_input_tokens
        total.output_tokens += bucket.output_tokens
        total.cost += bucket.cost

    output = {
        "schema": 1,
        "source": "client-jsonl",
        "updated_at": now.isoformat(timespec="seconds"),
        "date": day.isoformat(),
        "today": bucket_to_dict("Client local", total),
        "providers": providers,
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output["today"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
