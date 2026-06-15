from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib import error, parse, request


APP_DIR = Path(__file__).resolve().parent
ENV_FILES = [
    APP_DIR / ".env",
    APP_DIR / "deploy" / ".env",
    APP_DIR.parent.parent / "deploy" / ".env",
]
DEFAULT_BASE_URL = "http://127.0.0.1:8080"
REFRESH_SECONDS = 3
CLIENT_USAGE_CACHE_SECONDS = int(os.environ.get("SUB2API_CLIENT_USAGE_CACHE_SECONDS", "10"))
CLIENT_USAGE_EXPORT_TIMEOUT_SECONDS = int(os.environ.get("SUB2API_CLIENT_USAGE_EXPORT_TIMEOUT_SECONDS", "15"))
ACCOUNT_WINDOW_CACHE_SECONDS = int(os.environ.get("SUB2API_ACCOUNT_WINDOW_CACHE_SECONDS", "60"))
LOCAL_ACTIVE_WINDOW_SECONDS = int(os.environ.get("SUB2API_LOCAL_ACTIVE_WINDOW_SECONDS", "300"))
CLIENT_USAGE_EXPORT = Path(os.environ.get("CLIENT_USAGE_EXPORT") or APP_DIR / "client_usage_export.py")
if not CLIENT_USAGE_EXPORT.exists():
    fallback_export = APP_DIR.parent / "client-token-importer" / "client_usage_export.py"
    if fallback_export.exists():
        CLIENT_USAGE_EXPORT = fallback_export
CLIENT_USAGE_JSON = Path(os.environ.get("CLIENT_USAGE_JSON") or APP_DIR / "client_usage_today.json")
if not CLIENT_USAGE_JSON.exists():
    fallback_json = APP_DIR.parent / "client-token-importer" / "client_usage_today.json"
    if fallback_json.exists():
        CLIENT_USAGE_JSON = fallback_json
CLIENT_USAGE_PYTHON = os.environ.get("SUB2API_CLIENT_USAGE_PYTHON") or sys.executable
USAGE_HISTORY_JSON = Path(os.environ.get("SUB2API_USAGE_HISTORY_JSON") or APP_DIR / "usage_history.json")
CN_TZ = timezone(timedelta(hours=8), "CST")
DISPLAY_TIMEZONE = "Asia/Shanghai"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_env_files(paths: list[Path]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        values.update(read_env_file(path))
    return values


def env_bool(values: dict[str, str], key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        raw = values.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def normalize_url(value: str | None) -> str:
    url = (value or "").strip().strip('"').strip("'")
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    return url.rstrip("/")


def same_endpoint(left: str, right: str) -> bool:
    left = normalize_url(left)
    right = normalize_url(right)
    if not left or not right:
        return False
    try:
        left_url = parse.urlparse(left)
        right_url = parse.urlparse(right)
    except Exception:
        return False
    left_host = (left_url.hostname or "").lower()
    right_host = (right_url.hostname or "").lower()
    left_port = left_url.port or (443 if left_url.scheme == "https" else 80)
    right_port = right_url.port or (443 if right_url.scheme == "https" else 80)
    if left_port != right_port:
        return False
    if left_host == right_host:
        return True
    return left_host in LOCAL_HOSTS and right_host in LOCAL_HOSTS


def strip_url_path(url: str) -> str:
    normalized = normalize_url(url)
    if not normalized:
        return ""
    try:
        parts = parse.urlparse(normalized)
    except Exception:
        return normalized
    netloc = parts.netloc
    if not netloc:
        return normalized
    return parse.urlunparse((parts.scheme or "http", netloc, "", "", "", ""))


def extract_json_urls(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).lower()
            if isinstance(child, str) and any(part in key_text for part in ("base_url", "api_base", "api_base_url")):
                urls.append(child)
            urls.extend(extract_json_urls(child))
    elif isinstance(value, list):
        for child in value:
            urls.extend(extract_json_urls(child))
    return urls


def read_codex_toml_urls(path: Path) -> list[str]:
    if not path.exists():
        return []
    active_provider = ""
    current_section = ""
    root_urls: list[str] = []
    provider_urls: dict[str, list[str]] = {}
    key_value = re.compile(r"^([A-Za-z0-9_.-]+)\s*=\s*[\"']([^\"']+)[\"']")
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line.strip("[]").strip()
            continue
        match = key_value.match(line)
        if not match:
            continue
        key, value = match.groups()
        if key == "model_provider" and not current_section:
            active_provider = value
            continue
        if key != "base_url":
            continue
        if current_section.startswith("model_providers."):
            provider = current_section.split(".", 1)[1]
            provider_urls.setdefault(provider, []).append(value)
        elif not current_section:
            root_urls.append(value)
    urls: list[str] = []
    if active_provider:
        urls.extend(provider_urls.get(active_provider, []))
    urls.extend(root_urls)
    for provider, values in provider_urls.items():
        if provider != active_provider:
            urls.extend(values)
    return urls


def detect_codex_base_urls() -> list[str]:
    urls: list[str] = []
    for key in ("OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_API_BASE_URL", "CODEX_BASE_URL"):
        value = os.environ.get(key)
        if value:
            urls.append(value)
    codex_dir = Path(os.path.expanduser("~")) / ".codex"
    urls.extend(read_codex_toml_urls(codex_dir / "config.toml"))
    for name in ("auth.json", ".cockpit_codex_auth.json"):
        path = codex_dir / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        urls.extend(extract_json_urls(data))
    unique: list[str] = []
    for url in urls:
        normalized = normalize_url(url)
        if normalized and normalized not in unique:
            unique.append(normalized)
    return unique


def current_cockpit_account_label() -> str:
    codex_dir = Path(os.path.expanduser("~")) / ".codex"
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
            return email
        account_id = str(data.get("account_id") or "").strip()
        if account_id:
            return account_id
    return ""


def local_provider_display_name(provider_name: str) -> str:
    name = (provider_name or "Local client").strip() or "Local client"
    if name.lower().startswith("codex local - "):
        return name
    label = current_cockpit_account_label()
    if label and name.lower().startswith("codex"):
        return f"Codex local - {label}"
    return name


def ranking_account_display_name(account_name: str) -> str:
    name = (account_name or "-").strip() or "-"
    for prefix in ("Codex local - ", "Codex OAuth - ", "Relay - "):
        if name.lower().startswith(prefix.lower()):
            return name[len(prefix):].strip() or name
    return name


def compact_number(value: float | int | None) -> str:
    number = float(value or 0)
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number >= 1_000_000:
        return f"{sign}{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{sign}{number / 1_000:.1f}K"
    return f"{sign}{int(number):,}"


def money(value: float | int | None) -> str:
    number = float(value or 0)
    if 0 < number < 0.01:
        return f"${number:.6f}"
    return f"${number:.2f}"


def quota_color(utilization: float | int | None) -> str:
    try:
        value = float(utilization or 0)
    except (TypeError, ValueError):
        value = 0
    if value >= 90:
        return Theme.accent_red
    if value >= 60:
        return Theme.amber_bright
    return Theme.accent_green


def quota_reset_text(value: str | None) -> str:
    target = _parse_time(value)
    if target is None:
        return ""
    seconds = int((target - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return "\u5f85\u5237\u65b0"
    minutes = max(1, seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h \u540e\u91cd\u7f6e"
    if hours:
        return f"{hours}h {minutes}m \u540e\u91cd\u7f6e"
    return f"{minutes}m \u540e\u91cd\u7f6e"


def today_key() -> str:
    return datetime.now(CN_TZ).date().isoformat()


def date_key(days_ago: int) -> str:
    return (datetime.now(CN_TZ).date() - timedelta(days=days_ago)).isoformat()


def load_usage_history() -> dict[str, Any]:
    if not USAGE_HISTORY_JSON.exists():
        return {"schema": 1, "days": {}}
    try:
        data = json.loads(USAGE_HISTORY_JSON.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {"schema": 1, "days": {}}
    if not isinstance(data, dict):
        return {"schema": 1, "days": {}}
    days = data.get("days")
    if not isinstance(days, dict):
        data["days"] = {}
    data["schema"] = int(data.get("schema") or 1)
    return data


def summarize_usage_history(history: dict[str, Any]) -> dict[str, Any]:
    days = history.get("days") if isinstance(history, dict) else {}
    if not isinstance(days, dict):
        days = {}
    series: list[dict[str, Any]] = []
    for offset in range(6, -1, -1):
        key = date_key(offset)
        row = days.get(key) if isinstance(days.get(key), dict) else {}
        series.append(
            {
                "date": key,
                "cost": float(row.get("cost") or 0),
                "tokens": int(row.get("tokens") or 0),
                "requests": int(row.get("requests") or 0),
            }
        )
    today = series[-1]
    yesterday = series[-2] if len(series) >= 2 else {"cost": 0.0, "tokens": 0, "requests": 0}
    return {
        "today_cost": today["cost"],
        "today_tokens": today["tokens"],
        "today_requests": today["requests"],
        "yesterday_cost": yesterday["cost"],
        "yesterday_tokens": yesterday["tokens"],
        "yesterday_requests": yesterday["requests"],
        "seven_day_cost": sum(item["cost"] for item in series),
        "seven_day_tokens": sum(item["tokens"] for item in series),
        "seven_day_requests": sum(item["requests"] for item in series),
        "series": series,
    }


def summarize_trend_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_date: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("date") or "").strip()
        if key:
            by_date[key] = row

    series: list[dict[str, Any]] = []
    for offset in range(6, -1, -1):
        key = date_key(offset)
        row = by_date.get(key, {})
        series.append(
            {
                "date": key,
                "cost": float(row.get("actual_cost") or row.get("cost") or 0),
                "tokens": int(row.get("total_tokens") or row.get("tokens") or 0),
                "requests": int(row.get("requests") or 0),
            }
        )

    today = series[-1] if series else {"cost": 0.0, "tokens": 0, "requests": 0}
    yesterday = series[-2] if len(series) >= 2 else {"cost": 0.0, "tokens": 0, "requests": 0}
    return {
        "today_cost": today["cost"],
        "today_tokens": today["tokens"],
        "today_requests": today["requests"],
        "yesterday_cost": yesterday["cost"],
        "yesterday_tokens": yesterday["tokens"],
        "yesterday_requests": yesterday["requests"],
        "seven_day_cost": sum(item["cost"] for item in series),
        "seven_day_tokens": sum(item["tokens"] for item in series),
        "seven_day_requests": sum(item["requests"] for item in series),
        "series": series,
    }


def update_usage_history(state: "MonitorState") -> dict[str, Any]:
    history = load_usage_history()
    days = history.setdefault("days", {})
    if not isinstance(days, dict):
        days = {}
        history["days"] = days

    key = today_key()
    existing = days.get(key) if isinstance(days.get(key), dict) else {}
    new_cost = float(state.today_account_cost or 0)
    new_tokens = int(state.today_tokens or 0)
    new_requests = int(state.today_requests or 0)
    existing_cost = float(existing.get("cost") or 0)
    existing_tokens = int(existing.get("tokens") or 0)
    existing_requests = int(existing.get("requests") or 0)
    source_date = ""
    if isinstance(state.client_usage, dict):
        source_date = str(state.client_usage.get("date") or "").strip()
    existing_source_date = str(existing.get("source_date") or "").strip()

    # Same-day usage is cumulative. A sharp drop usually means a transient
    # exporter/API failure or a source switch, so keep the higher observed
    # counters instead of letting a partial refresh erase the day.
    if existing_source_date in {"", source_date, key}:
        if existing_tokens > new_tokens and existing_tokens >= max(1, int(new_tokens * 1.05)):
            new_tokens = existing_tokens
            new_requests = max(new_requests, existing_requests)
            new_cost = max(new_cost, existing_cost)

    days[key] = {
        "date": key,
        "source": state.usage_source,
        "requests": new_requests,
        "tokens": new_tokens,
        "cost": round(new_cost, 6),
        "updated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "source_date": source_date,
    }
    try:
        USAGE_HISTORY_JSON.parent.mkdir(parents=True, exist_ok=True)
        USAGE_HISTORY_JSON.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return summarize_usage_history(history)


def relative_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        seconds = max(0, int((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()))
    except Exception:
        return "-"
    if seconds < 60:
        return "刚刚"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}小时前"
    return f"{hours // 24}天前"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def is_recent_activity(value: str | None, window_seconds: int = LOCAL_ACTIVE_WINDOW_SECONDS) -> bool:
    dt = _parse_time(value)
    if dt is None:
        return False
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    return 0 <= seconds <= max(1, window_seconds)


def local_active_accounts_from_client_usage(
    client_usage: dict[str, Any] | None,
    *,
    include_when_routed_to_sub2api: bool = True,
) -> list[dict[str, Any]]:
    if not include_when_routed_to_sub2api:
        return []
    if not isinstance(client_usage, dict):
        return []
    client_latest = client_usage.get("latest_request")
    if not isinstance(client_latest, dict) or not client_latest.get("created_at"):
        return []
    if not is_recent_activity(str(client_latest.get("created_at") or "")):
        return []

    providers = client_usage.get("providers")
    providers = providers if isinstance(providers, list) else []
    provider_name = str(client_latest.get("provider") or "Local client")
    latest_provider = next(
        (provider for provider in providers if isinstance(provider, dict) and str(provider.get("name") or "") == provider_name),
        {},
    )
    return [
        {
            "id": "local",
            "name": f"LOCAL - {local_provider_display_name(provider_name)}",
            "current": 1,
            "max": 1,
            "model": client_latest.get("model") or "-",
            "source": "LOCAL",
            "speed_badge": latest_provider.get("speed_badge") or "",
        }
    ]


def account_health_badge(account: dict[str, Any]) -> str:
    """Return a short account health label for the ranking list."""
    status = str(account.get("status") or "").strip().lower()
    error_message = str(account.get("error_message") or account.get("last_error") or "").strip().lower()
    schedulable = account.get("schedulable")
    temp_until = _parse_time(account.get("temp_unschedulable_until") or account.get("cooldown_until"))

    if account.get("quota_exceeded") is True or status in {"quota_exceeded", "quota-exceeded", "quota"}:
        return "\u9650\u989d"
    if temp_until and temp_until > datetime.now(timezone.utc):
        return "\u51b7\u5374"
    if schedulable is False:
        return "\u4e0d\u53ef\u7528"
    if status in {"disabled", "inactive", "suspended", "banned", "unavailable"}:
        return "\u505c\u7528"
    if status in {"error", "failed"}:
        return "\u9519\u8bef"
    return ""


def account_has_email(account: dict[str, Any]) -> bool:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
    candidates = (
        account.get("name"),
        extra.get("email_address"),
        extra.get("email"),
        credentials.get("email"),
    )
    return any("@" in str(value or "") for value in candidates)


def normalize_usage_window(progress: Any) -> dict[str, Any]:
    if not isinstance(progress, dict):
        return {}
    stats = progress.get("window_stats")
    if not isinstance(stats, dict):
        return {}
    utilization = progress.get("utilization")
    result = {
        "requests": int(stats.get("requests") or 0),
        "tokens": int(stats.get("tokens") or 0),
        "cost": float(stats.get("cost") or 0),
        "resets_at": str(progress.get("resets_at") or ""),
        "quota_available": utilization is not None,
        "quota_stale": False,
    }
    if utilization is not None:
        try:
            used = float(utilization)
            result["utilization"] = used
            result["remaining_percent"] = max(0.0, min(100.0, 100.0 - used))
        except (TypeError, ValueError):
            result["quota_available"] = False
    return result


def load_client_usage() -> dict[str, Any] | None:
    if CLIENT_USAGE_EXPORT.exists():
        try:
            subprocess.run(
                [CLIENT_USAGE_PYTHON, str(CLIENT_USAGE_EXPORT), "--output", str(CLIENT_USAGE_JSON)],
                cwd=str(APP_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=CLIENT_USAGE_EXPORT_TIMEOUT_SECONDS,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
    if not CLIENT_USAGE_JSON.exists():
        return None
    try:
        data = json.loads(CLIENT_USAGE_JSON.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None
    today = data.get("today") if isinstance(data, dict) else None
    if not isinstance(today, dict):
        return None
    data_date = str(data.get("date") or "").strip()
    if data_date and data_date != today_key():
        return {
            "requests": 0,
            "tokens": 0,
            "cost": 0.0,
            "providers": [],
            "latest_request": {},
            "updated_at": data.get("updated_at") or "",
            "date": data_date,
            "stale": True,
        }
    return {
        "requests": int(today.get("requests") or 0),
        "tokens": int(today.get("tokens") or 0),
        "cost": float(today.get("cost") or 0),
        "providers": data.get("providers") or [],
        "latest_request": data.get("latest_request") or {},
        "updated_at": data.get("updated_at") or "",
        "date": data_date,
        "stale": False,
    }


def subtract_provider_from_client_usage(client_usage: dict[str, Any] | None, provider_name: str) -> dict[str, Any] | None:
    if not isinstance(client_usage, dict) or not provider_name:
        return client_usage
    providers = client_usage.get("providers")
    if not isinstance(providers, list):
        return client_usage

    kept: list[dict[str, Any]] = []
    removed_requests = 0
    removed_tokens = 0
    removed_cost = 0.0
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if str(provider.get("name") or "") == provider_name:
            removed_requests += int(provider.get("requests") or 0)
            removed_tokens += int(provider.get("tokens") or 0)
            removed_cost += float(provider.get("cost") or 0)
            continue
        kept.append(provider)

    if removed_requests <= 0 and removed_tokens <= 0 and removed_cost <= 0:
        return client_usage

    result = dict(client_usage)
    result["providers"] = kept
    result["requests"] = max(0, int(client_usage.get("requests") or 0) - removed_requests)
    result["tokens"] = max(0, int(client_usage.get("tokens") or 0) - removed_tokens)
    result["cost"] = max(0.0, float(client_usage.get("cost") or 0) - removed_cost)
    result["sub2api_routed_provider"] = provider_name
    return result


def local_usage_from_providers(client_usage: dict[str, Any] | None, prefixes: tuple[str, ...]) -> dict[str, Any] | None:
    if not client_usage:
        return None
    providers = client_usage.get("providers")
    if not isinstance(providers, list):
        return None

    selected: list[dict[str, Any]] = []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        name = str(provider.get("name") or "")
        if any(name.lower().startswith(prefix.lower()) for prefix in prefixes):
            selected.append(provider)
    if not selected:
        return None

    requests_count = sum(int(provider.get("requests") or 0) for provider in selected)
    tokens = sum(int(provider.get("tokens") or 0) for provider in selected)
    cost = sum(float(provider.get("cost") or 0) for provider in selected)
    if requests_count <= 0 and tokens <= 0 and cost <= 0:
        return None

    return {
        "requests": requests_count,
        "tokens": tokens,
        "cost": cost,
        "providers": selected,
        "updated_at": client_usage.get("updated_at") or "",
    }


def combine_client_usage(usages: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    selected = [usage for usage in usages if usage and (usage.get("requests") or usage.get("tokens") or usage.get("cost"))]
    if not selected:
        return None

    providers: list[dict[str, Any]] = []
    for usage in selected:
        usage_providers = usage.get("providers")
        if isinstance(usage_providers, list):
            providers.extend([provider for provider in usage_providers if isinstance(provider, dict)])

    return {
        "requests": sum(int(usage.get("requests") or 0) for usage in selected),
        "tokens": sum(int(usage.get("tokens") or 0) for usage in selected),
        "cost": sum(float(usage.get("cost") or 0) for usage in selected),
        "providers": providers,
        "updated_at": max([str(usage.get("updated_at") or "") for usage in selected], default=""),
    }


def residual_client_usage(
    client_usage: dict[str, Any] | None,
    server_requests: int,
    server_tokens: int,
    server_cost: float,
) -> dict[str, Any] | None:
    if not client_usage:
        return None

    raw_requests = int(client_usage.get("requests") or 0)
    raw_tokens = int(client_usage.get("tokens") or 0)
    raw_cost = float(client_usage.get("cost") or 0)
    if raw_requests <= 0 and raw_tokens <= 0 and raw_cost <= 0:
        return None

    local_requests = max(0, raw_requests - max(0, int(server_requests or 0)))
    local_tokens = max(0, raw_tokens - max(0, int(server_tokens or 0)))
    local_cost = max(0.0, raw_cost - max(0.0, float(server_cost or 0)))
    if local_tokens > 0 and local_requests == 0:
        local_requests = 1
    if local_tokens > 0 and local_cost == 0 and raw_tokens > 0:
        local_cost = raw_cost * (local_tokens / raw_tokens)

    if local_requests <= 0 and local_tokens <= 0 and local_cost <= 0:
        return None

    result = dict(client_usage)
    result["requests"] = local_requests
    result["tokens"] = local_tokens
    result["cost"] = local_cost
    result["raw_requests"] = raw_requests
    result["raw_tokens"] = raw_tokens
    result["raw_cost"] = raw_cost
    result["deducted_requests"] = max(0, int(server_requests or 0))
    result["deducted_tokens"] = max(0, int(server_tokens or 0))
    result["deducted_cost"] = max(0.0, float(server_cost or 0))
    return result


@dataclass
class MonitorState:
    loading: bool = True
    error: str | None = None
    updated_at: float | None = None
    mode: str = "sub2api"
    source_label: str = "SUB2 监控"
    usage_source: str = "sub2api"
    usage_note: str = ""
    active_accounts: list[dict[str, Any]] | None = None
    latest_request: dict[str, Any] | None = None
    latest_account_name: str = ""
    today_requests: int = 0
    today_tokens: int = 0
    today_account_cost: float = 0.0
    cost_history: dict[str, Any] | None = None
    top_accounts: list[dict[str, Any]] | None = None
    client_usage: dict[str, Any] | None = None
    client_usage_history: dict[str, Any] | None = None


def build_local_monitor_state(error_text: str | None = None, usage_note: str = "本地客户端日志") -> MonitorState:
    client_usage = load_client_usage() or {
        "requests": 0,
        "tokens": 0,
        "cost": 0.0,
        "providers": [],
        "updated_at": "",
    }
    providers = client_usage.get("providers") if isinstance(client_usage, dict) else []
    top_accounts: list[dict[str, Any]] = []
    if isinstance(providers, list):
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            top_accounts.append(
                {
                    "name": local_provider_display_name(str(provider.get("name") or "Local client")),
                    "tokens": int(provider.get("tokens") or 0),
                    "requests": int(provider.get("requests") or 0),
                    "cost": float(provider.get("cost") or 0),
                    "health_badge": "",
                    "source_badge": "LOCAL",
                    "app_speed": provider.get("app_speed") or "",
                    "cost_multiplier": provider.get("cost_multiplier") or 1,
                    "speed_badge": provider.get("speed_badge") or "",
                    "window_5h": provider.get("window_5h") or {},
                    "window_7d": provider.get("window_7d") or {},
                    "window_cycle": provider.get("window_cycle") or {},
                }
            )
    top_accounts.sort(key=lambda row: (-row["tokens"], -row["requests"], row["name"]))

    updated_at = client_usage.get("updated_at") if isinstance(client_usage, dict) else ""
    client_latest = client_usage.get("latest_request") if isinstance(client_usage, dict) else {}
    latest_request = None
    latest_account_name = "Local client logs"
    active_accounts: list[dict[str, Any]] = []
    if isinstance(client_latest, dict) and client_latest.get("created_at"):
        provider_name = str(client_latest.get("provider") or "Local client")
        latest_provider = next(
            (provider for provider in providers if isinstance(provider, dict) and str(provider.get("name") or "") == provider_name),
            {},
        )
        latest_request = {
            "kind": client_latest.get("kind") or "success",
            "model": client_latest.get("model") or "-",
            "created_at": client_latest.get("created_at"),
            "source": "LOCAL",
            "speed_badge": latest_provider.get("speed_badge") or "",
        }
        latest_account_name = f"LOCAL - {local_provider_display_name(provider_name)}"
        if is_recent_activity(str(client_latest.get("created_at") or "")):
            active_accounts.append(
                {
                    "id": "local",
                    "name": latest_account_name,
                    "current": 1,
                    "max": 1,
                    "model": client_latest.get("model") or "-",
                    "source": "LOCAL",
                    "speed_badge": latest_provider.get("speed_badge") or "",
                }
            )
    elif updated_at:
        latest_request = {
            "kind": "success",
            "model": "local-codex",
            "created_at": updated_at,
            "source": "LOCAL",
        }
        if is_recent_activity(str(updated_at)):
            active_accounts.append(
                {
                    "id": "local",
                    "name": latest_account_name,
                    "current": 1,
                    "max": 1,
                    "model": "local-codex",
                    "source": "LOCAL",
                }
            )

    active_accounts = local_active_accounts_from_client_usage(client_usage)
    return MonitorState(
        loading=False,
        error=error_text,
        updated_at=time.time(),
        mode="local-codex",
        source_label="LOCAL-CODEX",
        usage_source="local",
        usage_note=usage_note,
        active_accounts=active_accounts,
        latest_request=latest_request,
        latest_account_name=latest_account_name,
        today_requests=int(client_usage.get("requests") or 0),
        today_tokens=int(client_usage.get("tokens") or 0),
        today_account_cost=float(client_usage.get("cost") or 0),
        top_accounts=top_accounts,
        client_usage=client_usage,
        client_usage_history=summarize_usage_history(load_usage_history()),
    )


def build_sub2api_error_state(error_text: str, usage_note: str) -> MonitorState:
    return MonitorState(
        loading=False,
        error=error_text,
        updated_at=time.time(),
        mode="sub2api",
        source_label="SUB2 监控",
        usage_source="sub2api",
        usage_note=usage_note,
        active_accounts=[],
        latest_request=None,
        latest_account_name="",
        today_requests=0,
        today_tokens=0,
        today_account_cost=0.0,
        top_accounts=[],
        client_usage=None,
        client_usage_history=summarize_usage_history(load_usage_history()),
    )


def empty_client_usage() -> dict[str, Any]:
    return {
        "requests": 0,
        "tokens": 0,
        "cost": 0.0,
        "providers": [],
        "updated_at": "",
    }


class Sub2APIClient:
    def __init__(self) -> None:
        env = read_env_files(ENV_FILES)
        self.base_url = os.environ.get("SUB2API_BASE_URL") or env.get("SUB2API_BASE_URL") or DEFAULT_BASE_URL
        self.base_url = self.base_url.rstrip("/")
        self.email = os.environ.get("SUB2API_ADMIN_EMAIL") or env.get("ADMIN_EMAIL") or "admin@sub2api.local"
        self.password = os.environ.get("SUB2API_ADMIN_PASSWORD") or env.get("ADMIN_PASSWORD") or ""
        self.mode = (os.environ.get("SUB2API_MONITOR_MODE") or env.get("SUB2API_MONITOR_MODE") or "auto").strip().lower()
        usage_source = os.environ.get("SUB2API_MONITOR_USAGE_SOURCE") or env.get("SUB2API_MONITOR_USAGE_SOURCE") or ""
        self.usage_source = usage_source.strip().lower() or ("both" if env_bool(env, "SUB2API_INCLUDE_LOCAL_USAGE", False) else "auto")
        self.token: str | None = None
        self._client_usage_cache: dict[str, Any] | None = None
        self._client_usage_cache_at: float = 0.0
        self._account_window_cache: dict[int, tuple[float, dict[str, Any]]] = {}

    def _sub2api_match_urls(self) -> list[str]:
        env = read_env_files(ENV_FILES)
        urls = [self.base_url]
        extra = os.environ.get("SUB2API_MATCH_BASE_URLS") or env.get("SUB2API_MATCH_BASE_URLS") or ""
        for item in extra.split(","):
            item = item.strip()
            if item:
                urls.append(item)
        return [strip_url_path(url) for url in urls if strip_url_path(url)]

    def _codex_points_to_sub2api(self) -> tuple[bool | None, list[str]]:
        codex_urls = detect_codex_base_urls()
        if not codex_urls:
            return None, []
        sub2api_urls = self._sub2api_match_urls()
        active_url = codex_urls[0]
        if any(same_endpoint(active_url, sub2api_url) for sub2api_url in sub2api_urls):
            return True, codex_urls
        return False, codex_urls

    def _resolve_usage_source(self) -> tuple[str, str]:
        if self.usage_source in {"sub2api", "server"}:
            return "sub2api", "手动: Sub2API"
        if self.usage_source in {"local", "local-codex", "client"}:
            return "local", "手动: 本地日志"
        if self.usage_source in {"both", "merge", "all"}:
            return "both", "手动: 合并显示"
        points_to_sub2api, codex_urls = self._codex_points_to_sub2api()
        if points_to_sub2api is True:
            return "sub2api", "Auto: Codex -> Sub2API"
        if points_to_sub2api is False:
            first = codex_urls[0] if codex_urls else ""
            return "local", f"Auto: Codex -> {strip_url_path(first) or 'other API'}"
        return "local", "Auto: 未确认 Codex endpoint"

    def _should_include_client_usage(self, resolved_source: str) -> bool:
        if self.usage_source in {"sub2api", "server"}:
            return False
        if self.usage_source in {"both", "merge", "all", "local", "local-codex", "client"}:
            return True
        return resolved_source in {"sub2api", "local", "both"}

    def _load_client_usage_cached(self) -> dict[str, Any] | None:
        now = time.time()
        if self._client_usage_cache is not None and now - self._client_usage_cache_at < CLIENT_USAGE_CACHE_SECONDS:
            return self._client_usage_cache
        client_usage = load_client_usage()
        self._client_usage_cache = client_usage
        self._client_usage_cache_at = now
        return client_usage

    def clear_client_usage_cache(self) -> None:
        self._client_usage_cache = None
        self._client_usage_cache_at = 0.0

    def _load_account_windows_cached(self, account: dict[str, Any]) -> dict[str, Any]:
        account_id = int(account.get("id") or 0)
        if account_id <= 0 or not account_has_email(account) or str(account.get("type") or "").lower() != "oauth":
            return {}

        now = time.time()
        cached = self._account_window_cache.get(account_id)
        if cached and now - cached[0] < ACCOUNT_WINDOW_CACHE_SECONDS:
            return cached[1]

        try:
            usage = self._request("GET", f"/api/v1/admin/accounts/{account_id}/usage") or {}
            result = {
                "window_5h": normalize_usage_window(usage.get("five_hour")),
                "window_7d": normalize_usage_window(usage.get("seven_day")),
                "window_cycle": normalize_usage_window(usage.get("cycle") or usage.get("primary_window")),
            }
            self._account_window_cache[account_id] = (now, result)
            return result
        except Exception:
            return cached[1] if cached else {}

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> Any:
        query = ""
        if params:
            query = "?" + parse.urlencode({k: v for k, v in params.items() if v is not None})
        body = None
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = request.Request(f"{self.base_url}{path}{query}", data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            if exc.code == 401 and retry_auth:
                self.login()
                return self._request(method, path, payload, params, retry_auth=False)
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {exc.code}: {detail[:160]}")
        except error.URLError as exc:
            raise RuntimeError(f"无法连接 {self.base_url}: {exc.reason}")

        data = json.loads(raw) if raw else {}
        if isinstance(data, dict) and "code" in data:
            if data.get("code") == 0:
                return data.get("data")
            raise RuntimeError(str(data.get("message") or data.get("reason") or "接口返回错误"))
        return data

    def _fetch_dashboard_trend(self) -> dict[str, Any]:
        params = {
            "start_date": date_key(6),
            "end_date": today_key(),
            "granularity": "day",
            "timezone": DISPLAY_TIMEZONE,
        }
        data = self._request("GET", "/api/v1/admin/dashboard/trend", params=params) or {}
        trend = data.get("trend") if isinstance(data, dict) else []
        if not isinstance(trend, list):
            trend = []
        return summarize_trend_rows([row for row in trend if isinstance(row, dict)])

    def login(self) -> None:
        if not self.password:
            raise RuntimeError("没有找到管理员密码，请检查 deploy/.env 或 SUB2API_ADMIN_PASSWORD")
        data = self._request(
            "POST",
            "/api/v1/auth/login",
            {"email": self.email, "password": self.password},
            retry_auth=False,
        )
        if isinstance(data, dict) and data.get("requires_2fa"):
            raise RuntimeError("管理员账号开启了 2FA，桌面监控暂不支持自动登录")
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise RuntimeError("登录成功但没有返回 access_token")
        self.token = str(token)

    def fetch_state(self) -> MonitorState:
        if self.mode in {"local", "local-codex", "client", "client-local"}:
            return build_local_monitor_state()
        resolved_source, usage_note = self._resolve_usage_source()
        try:
            return self.fetch_sub2api_state()
        except Exception as exc:
            if self.mode in {"", "auto"}:
                return build_local_monitor_state(
                    str(exc),
                    f"{usage_note} / Sub2API 不可用，已切到本地日志",
                )
            raise

    def fetch_sub2api_state(self) -> MonitorState:
        resolved_source, usage_note = self._resolve_usage_source()
        if not self.token:
            self.login()

        stats = self._request("GET", "/api/v1/admin/dashboard/stats") or {}
        accounts_resp = self._request(
            "GET",
            "/api/v1/admin/accounts",
            params={"page": 1, "page_size": 1000, "platform": "openai", "sort_by": "priority", "sort_order": "asc"},
        ) or {}
        accounts = accounts_resp.get("items") or []
        account_map = {int(item.get("id")): item for item in accounts if item.get("id") is not None}

        try:
            concurrency_resp = self._request("GET", "/api/v1/admin/ops/concurrency", params={"platform": "openai"}) or {}
            concurrency = concurrency_resp.get("account") or {}
        except Exception:
            concurrency = {}

        try:
            requests_resp = self._request(
                "GET",
                "/api/v1/admin/ops/requests",
                params={
                    "time_range": "30d",
                    "kind": "all",
                    "platform": "openai",
                    "page": 1,
                    "page_size": 1,
                    "sort": "created_at_desc",
                },
            ) or {}
            latest = (requests_resp.get("items") or [None])[0]
        except Exception:
            latest = None

        try:
            trend_history = self._fetch_dashboard_trend()
        except Exception:
            trend_history = summarize_usage_history(load_usage_history())

        account_ids = [int(item["id"]) for item in accounts if item.get("id") is not None]
        today_by_account: dict[str, Any] = {}
        if account_ids:
            try:
                batch = self._request(
                    "POST",
                    "/api/v1/admin/accounts/today-stats/batch",
                    {"account_ids": account_ids},
                ) or {}
                today_by_account = batch.get("stats") or {}
            except Exception:
                today_by_account = {}

        active_accounts = []
        for item in concurrency.values():
            if int(item.get("current_in_use") or 0) <= 0:
                continue
            account_id = int(item.get("account_id") or 0)
            active_accounts.append(
                {
                    "id": account_id,
                    "name": account_map.get(account_id, {}).get("name") or item.get("account_name") or f"账号 #{account_id}",
                    "current": int(item.get("current_in_use") or 0),
                    "max": int(item.get("max_capacity") or 0),
                }
            )
        active_accounts.sort(key=lambda row: (-row["current"], row["id"]))

        latest_account_name = ""
        if latest and latest.get("account_id"):
            latest_id = int(latest["account_id"])
            latest_account_name = account_map.get(latest_id, {}).get("name") or f"账号 #{latest_id}"

        top_accounts = []
        realtime_today_requests = 0
        realtime_today_tokens = 0
        realtime_today_cost = 0.0
        for account in accounts:
            account_id = int(account.get("id") or 0)
            account_stats = today_by_account.get(str(account_id)) or {}
            account_windows = self._load_account_windows_cached(account)
            tokens = int(account_stats.get("tokens") or 0)
            requests_count = int(account_stats.get("requests") or 0)
            cost = float(account_stats.get("cost") or 0)
            realtime_today_requests += requests_count
            realtime_today_tokens += tokens
            realtime_today_cost += cost
            top_accounts.append(
                {
                    "name": account.get("name") or f"账号 #{account_id}",
                    "tokens": tokens,
                    "requests": requests_count,
                    "cost": cost,
                    "health_badge": account_health_badge(account),
                    "source_badge": "SUB",
                    "window_5h": account_windows.get("window_5h") or {},
                    "window_7d": account_windows.get("window_7d") or {},
                    "window_cycle": account_windows.get("window_cycle") or {},
                }
            )
        top_accounts.sort(key=lambda row: (-row["tokens"], -row["requests"], row["name"]))
        include_client_usage = self._should_include_client_usage(resolved_source)
        points_to_sub2api, _codex_urls = self._codex_points_to_sub2api()
        show_local_activity = include_client_usage and points_to_sub2api is not True
        raw_client_usage = self._load_client_usage_cached() if include_client_usage else None
        client_usage = raw_client_usage
        if points_to_sub2api is True and isinstance(raw_client_usage, dict):
            raw_latest = raw_client_usage.get("latest_request")
            routed_provider = (
                str(raw_latest.get("provider") or "")
                if isinstance(raw_latest, dict)
                else ""
            )
            if routed_provider:
                client_usage = subtract_provider_from_client_usage(raw_client_usage, routed_provider)
        if client_usage and (client_usage["tokens"] or client_usage["requests"] or client_usage["cost"]):
            today_requests = realtime_today_requests + int(client_usage.get("requests") or 0)
            today_tokens = realtime_today_tokens + int(client_usage.get("tokens") or 0)
            today_account_cost = realtime_today_cost + float(client_usage.get("cost") or 0)
            ledger_source = "both"
            ledger_note = f"{usage_note} / Sub2API + 本地日志"
        else:
            today_requests = int(stats.get("today_requests") or realtime_today_requests)
            today_tokens = int(stats.get("today_tokens") or realtime_today_tokens)
            today_account_cost = float(stats.get("today_actual_cost") or realtime_today_cost)
            ledger_source = resolved_source
            ledger_note = usage_note

        providers = client_usage.get("providers") if isinstance(client_usage, dict) else []
        if isinstance(providers, list):
            for provider in providers:
                if not isinstance(provider, dict):
                    continue
                provider_tokens = int(provider.get("tokens") or 0)
                provider_requests = int(provider.get("requests") or 0)
                provider_cost = float(provider.get("cost") or 0)
                if (
                    provider_tokens <= 0
                    and provider_requests <= 0
                    and provider_cost <= 0
                    and not provider.get("show_zero")
                ):
                    continue
                top_accounts.append(
                    {
                        "name": local_provider_display_name(str(provider.get("name") or "Local client")),
                        "tokens": provider_tokens,
                        "requests": provider_requests,
                        "cost": provider_cost,
                        "health_badge": "",
                        "source_badge": "LOCAL",
                        "app_speed": provider.get("app_speed") or "",
                        "cost_multiplier": provider.get("cost_multiplier") or 1,
                        "speed_badge": provider.get("speed_badge") or "",
                        "window_5h": provider.get("window_5h") or {},
                        "window_7d": provider.get("window_7d") or {},
                        "window_cycle": provider.get("window_cycle") or {},
                    }
                )
            top_accounts.sort(key=lambda row: (-row["tokens"], -row["requests"], row["name"]))

        display_latest = latest
        display_latest_account_name = latest_account_name
        client_latest = client_usage.get("latest_request") if isinstance(client_usage, dict) else {}
        if show_local_activity and isinstance(client_latest, dict) and client_latest.get("created_at"):
            provider_name = str(client_latest.get("provider") or "Local client")
            latest_provider = next(
                (provider for provider in providers if isinstance(provider, dict) and str(provider.get("name") or "") == provider_name),
                {},
            )
            local_latest = {
                "kind": client_latest.get("kind") or "success",
                "model": client_latest.get("model") or "-",
                "created_at": client_latest.get("created_at"),
                "source": "LOCAL",
                "speed_badge": latest_provider.get("speed_badge") or "",
            }
            local_dt = _parse_time(str(client_latest.get("created_at") or ""))
            sub_dt = _parse_time(str(latest.get("created_at") or "")) if isinstance(latest, dict) else None
            if sub_dt is None or (local_dt is not None and local_dt >= sub_dt):
                display_latest = local_latest
                display_latest_account_name = f"LOCAL - {local_provider_display_name(provider_name)}"

        for local_active in local_active_accounts_from_client_usage(
            client_usage,
            include_when_routed_to_sub2api=show_local_activity,
        ):
            local_name = str(local_active.get("name") or "")
            if not any(str(account.get("name") or "") == local_name for account in active_accounts):
                active_accounts.append(local_active)
        active_accounts.sort(
            key=lambda row: (-int(row.get("current") or 0), str(row.get("id") or ""), str(row.get("name") or ""))
        )

        return MonitorState(
            loading=False,
            updated_at=time.time(),
            mode="sub2api",
            source_label="MONITOR",
            usage_source=ledger_source,
            usage_note=ledger_note,
            active_accounts=active_accounts[:4],
            latest_request=display_latest,
            latest_account_name=display_latest_account_name,
            today_requests=today_requests,
            today_tokens=today_tokens,
            today_account_cost=today_account_cost,
            cost_history=trend_history,
            top_accounts=top_accounts,
            client_usage=client_usage,
            client_usage_history=summarize_usage_history(load_usage_history()),
        )


class Theme:
    """Cockpit-inspired dark amber floating card palette."""
    # ── base surfaces ──
    bg_dark = "#0B0D12"
    bg_card = "#151820"
    bg_section = "#1D222C"
    bg_lift = "#232A36"
    bg_hover = "#2A3140"

    # ── amber accent ramp ──
    amber_dim = "#8A6522"
    amber = "#E0A84B"
    amber_bright = "#FFD37A"
    amber_glow = "#FFD980"

    # ── secondary accents ──
    cyan = "#6DD6E8"
    cyan_dim = "#285A66"
    violet = "#A78BFA"
    blue = "#7DB7FF"

    # ── text ──
    text_primary = "#F2EBDD"
    text_secondary = "#C9BBA3"
    text_muted = "#7B7161"

    # ── semantic ──
    accent_cyan = "#66D9E8"
    accent_red = "#F07178"
    accent_green = "#8BD17C"
    quota_red_bg = "#34131D"
    quota_amber_bg = "#33240E"
    quota_green_bg = "#10251A"

    # ── misc ──
    border = "#2A2D35"
    shadow = "#000000"
    transparent = "#010203"

    # ── fonts (family, size, weight) ──
    font_title = ("Segoe UI", 15, "bold")
    font_section = ("Segoe UI", 11, "bold")
    font_label = ("Segoe UI", 10, "normal")
    font_label_bold = ("Segoe UI", 10, "bold")
    font_value = ("Consolas", 17, "bold")
    font_value_sm = ("Consolas", 13, "bold")
    font_tiny = ("Segoe UI", 9, "normal")
    font_micro = ("Segoe UI", 8, "normal")
    font_icon = ("Segoe UI", 13, "normal")


class FloatingMonitorApp:
    """Borderless always-on-top floating monitor built entirely on tk.Canvas."""

    WIDTH = 390
    HEIGHT = 760
    MIN_WIDTH = 360
    MIN_HEIGHT = 640
    WINDOW_ALPHA = 0.92

    def __init__(self) -> None:
        self.client = Sub2APIClient()
        self.state: MonitorState | None = None
        self.error: str | None = None
        self.closed = False
        self._pinned = True
        self._loading = False
        self._refresh_lock = threading.Lock()
        self._pulse_phase = 0.0
        self._fade_alpha = 0.0
        self._drag_data = {"x": 0, "y": 0}
        self._resize_data = {"x": 0, "y": 0, "w": self.WIDTH, "h": self.HEIGHT}
        self._resizing = False
        self._hover_btn: str | None = None
        self._btn_rects: dict[str, tuple[int, int, int, int]] = {}
        self._account_range = "today"
        self._account_range_user_selected = False
        self._account_range_auto_selected = False
        self._topmost_repair_scheduled = False

        # ── root window ──
        self.root = tk.Tk()
        self.root.title("Sub2 Monitor")
        self.root.overrideredirect(True)
        self.root.geometry(f"{self.WIDTH}x{self.HEIGHT}+1120+70")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.configure(bg=Theme.transparent)
        try:
            self.root.attributes("-transparentcolor", Theme.transparent)
        except tk.TclError:
            self.root.configure(bg=Theme.bg_dark)

        # ── canvas ──
        self.canvas = tk.Canvas(
            self.root,
            width=self.WIDTH,
            height=self.HEIGHT,
            bg=Theme.transparent,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        # ── fonts (resolved) ──
        self._fonts: dict[str, tkfont.Font] = {}
        for attr in dir(Theme):
            if attr.startswith("font_"):
                family, size, weight = getattr(Theme, attr)
                self._fonts[attr] = tkfont.Font(
                    family=family, size=size, weight=weight
                )

        # ── bindings ──
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)

        # ── initial draw & data ──
        self._draw()
        self._fade_in()
        self.refresh_async()
        self._schedule_auto_refresh()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  GEOMETRY HELPERS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @staticmethod
    def _rounded_rect_points(
        x1: int, y1: int, x2: int, y2: int, r: int
    ) -> list[int]:
        """Return point list for a rounded rectangle (for create_polygon smooth)."""
        r = min(r, (x2 - x1) // 2, (y2 - y1) // 2)
        pts = []
        for a in range(180, 270 + 1, 10):
            rad = math.radians(a)
            pts += [x1 + r + r * math.cos(rad), y1 + r + r * math.sin(rad)]
        for a in range(270, 360 + 1, 10):
            rad = math.radians(a)
            pts += [x2 - r + r * math.cos(rad), y1 + r + r * math.sin(rad)]
        for a in range(0, 90 + 1, 10):
            rad = math.radians(a)
            pts += [x2 - r + r * math.cos(rad), y2 - r + r * math.sin(rad)]
        for a in range(90, 180 + 1, 10):
            rad = math.radians(a)
            pts += [x1 + r + r * math.cos(rad), y2 - r + r * math.sin(rad)]
        return pts

    def _draw_rounded_rect(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        r: int = 10,
        **kw: Any,
    ) -> int:
        pts = self._rounded_rect_points(x1, y1, x2, y2, r)
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _text_width(self, text: str, font_key: str) -> int:
        return self._fonts[font_key].measure(text)

    def _ensure_topmost(self, force: bool = False) -> None:
        if not self._pinned and not force:
            return
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after_idle(lambda: self.root.attributes("-topmost", True))
            self.root.after(250, lambda: self.root.attributes("-topmost", True))
        except tk.TclError:
            pass

    def _schedule_topmost_repair(self) -> None:
        if self.closed or self._topmost_repair_scheduled:
            return
        self._topmost_repair_scheduled = True

        def _repair() -> None:
            self._topmost_repair_scheduled = False
            if self.closed or not self._pinned:
                return
            self._ensure_topmost()
            self._schedule_topmost_repair()

        self.root.after(4000, _repair)

    def _truncate(self, text: str, font_key: str, max_w: int) -> str:
        f = self._fonts[font_key]
        if f.measure(text) <= max_w:
            return text
        while text and f.measure(text + "...") > max_w:
            text = text[:-1]
        return text + "..."

    def _latest_status(self) -> tuple[str, str, str, str]:
        if not self.state or not self.state.latest_request:
            return "-", "-", "-", Theme.text_muted
        req = self.state.latest_request
        kind = req.get("kind", "-")
        model = req.get("model", "-")
        created = req.get("created_at", "")
        status = "\u9519\u8bef" if kind == "error" else ("\u6210\u529f" if kind else "-")
        color = Theme.accent_red if kind == "error" else Theme.accent_green
        return status, model, relative_time(created) if created else "-", color

    def _draw_pill(self, x: int, y: int, text: str, color: str, max_w: int) -> None:
        label = self._truncate(text, "font_tiny", max_w - 14)
        width = min(max_w, self._text_width(label, "font_tiny") + 14)
        self._draw_rounded_rect(x, y, x + width, y + 22, r=8, fill=Theme.bg_dark, outline=Theme.border)
        self.canvas.create_text(x + 7, y + 4, anchor="nw", text=label, font=self._fonts["font_tiny"], fill=color)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  DRAWING
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _health_color(self, label: str) -> str:
        if label in {"LOCAL", "\u672c\u5730"}:
            return Theme.accent_green
        if label in {"SUB", "SUB2"}:
            return Theme.cyan
        if label.upper().startswith("FAST"):
            return Theme.amber_bright
        if label in {"\u9650\u989d", "\u9650\u6d41", "\u51b7\u5374"}:
            return Theme.amber_bright
        if label in {"\u4e0d\u53ef\u7528", "\u505c\u7528", "\u9519\u8bef"}:
            return Theme.accent_red
        return Theme.text_muted

    def _draw_health_badge(self, x: int, y: int, label: str) -> int:
        if not label:
            return 0
        color = self._health_color(label)
        width = self._text_width(label, "font_micro") + 14
        self._draw_rounded_rect(x, y, x + width, y + 17, r=7, fill=Theme.bg_dark, outline=color)
        self.canvas.create_text(x + 7, y + 2, anchor="nw", text=label, font=self._fonts["font_micro"], fill=color)
        return width

    def _draw(self) -> None:
        if self.closed:
            return
        c = self.canvas
        c.delete("all")
        W, H = self.WIDTH, self.HEIGHT
        PAD = 14
        COL_L = PAD
        COL_R = W - PAD

        # ── outer card background ──
        self._draw_rounded_rect(4, 7, W - 2, H - 2, r=18, fill="#050608", outline="")
        self._draw_rounded_rect(0, 0, W, H - 5, r=18, fill=Theme.bg_card, outline=Theme.border, width=1)

        # ── subtle top accent lines ──
        c.create_line(20, 2, W // 2 - 8, 2, fill=Theme.amber, width=2)
        c.create_line(W // 2 + 8, 2, W - 20, 2, fill=Theme.cyan_dim, width=2)

        # ════════════════════════════════════════════════════════
        #  HEADER  (row y=10..48)
        # ════════════════════════════════════════════════════════
        y = 16
        title_text = self.state.source_label if self.state else "SUB2 \u76d1\u63a7"
        c.create_text(COL_L, y, anchor="nw", text=title_text,
                       font=self._fonts["font_title"], fill=Theme.amber_bright)

        if self._loading:
            brightness = int(128 + 127 * math.sin(self._pulse_phase))
            dot_color = f"#{brightness // 2:02x}{brightness:02x}{brightness:02x}"
            c.create_oval(COL_R - 78, y + 5, COL_R - 68, y + 15, fill=dot_color, outline="")
        elif self.state:
            c.create_oval(COL_R - 78, y + 5, COL_R - 68, y + 15, fill=Theme.accent_green, outline="")

        active_count = len(self.state.active_accounts or []) if self.state else 0
        updated = "\u8bfb\u53d6\u4e2d" if self._loading else "\u7b49\u5f85\u5237\u65b0"
        if self.state and self.state.updated_at:
            updated = relative_time(datetime.fromtimestamp(self.state.updated_at, timezone.utc).isoformat())
        subtitle = f"\u6d3b\u8dc3 {active_count}  /  {updated}"
        if self.state and self.state.mode == "local-codex":
            subtitle = f"\u6d3b\u8dc3 {active_count}  /  {updated}"
        c.create_text(COL_L, y + 24, anchor="nw", text=subtitle,
                      font=self._fonts["font_micro"], fill=Theme.text_muted)

        btn_y = y - 2
        btn_specs = [
            ("btn_close", "\u00d7", COL_R - 14),
            ("btn_pin", "\u7f6e" if self._pinned else "\u9876", COL_R - 36),
            ("btn_refresh", "\u21bb", COL_R - 58),
        ]
        self._btn_rects.clear()
        for name, glyph, bx in btn_specs:
            bx1, by1, bx2, by2 = bx - 9, btn_y - 2, bx + 9, btn_y + 16
            self._btn_rects[name] = (bx1, by1, bx2, by2)
            is_hover = self._hover_btn == name
            bg = Theme.bg_hover if is_hover else ""
            if bg:
                self._draw_rounded_rect(bx1, by1, bx2, by2, r=4, fill=bg, outline="")
            fg = Theme.amber_bright if is_hover else Theme.text_secondary
            if name == "btn_close":
                fg = Theme.accent_red if is_hover else Theme.text_secondary
            c.create_text(bx, btn_y + 7, text=glyph, font=self._fonts["font_icon"],
                           fill=fg, anchor="center")

        y = 52
        c.create_line(COL_L, y, COL_R, y, fill=Theme.border, width=1)

        # ════════════════════════════════════════════════════════
        #  CURRENT CHANNEL HERO
        # ════════════════════════════════════════════════════════
        y += 12
        self._draw_rounded_rect(COL_L, y, COL_R, y + 72, r=13, fill=Theme.bg_section, outline=Theme.border)
        accounts = (self.state.active_accounts if self.state else [])[:5]
        latest_name = self.state.latest_account_name if self.state else ""
        if accounts:
            hero_name = accounts[0].get("name", latest_name or "-")
            total_current = sum(int(account.get("current") or 0) for account in accounts)
            hero_sub = f"{len(accounts)} \u4e2a\u8d26\u53f7\u6d3b\u8dc3 / \u603b\u5e76\u53d1 {total_current}"
            hero_color = Theme.accent_green
        else:
            status, _model, ago, color = self._latest_status()
            hero_name = latest_name or "\u6682\u65e0\u8bf7\u6c42"
            hero_sub = f"\u6700\u8fd1 {status} / {ago}" if status != "-" else "\u6682\u65e0\u6d3b\u8dc3\u8bf7\u6c42"
            hero_color = color if status != "-" else Theme.cyan
        c.create_rectangle(COL_L + 12, y + 13, COL_L + 58, y + 15, fill=Theme.cyan, outline="")
        c.create_text(COL_L + 12, y + 22, anchor="nw", text=self._truncate(hero_name, "font_label_bold", COL_R - COL_L - 32),
                      font=self._fonts["font_label_bold"], fill=Theme.text_primary)
        self._draw_pill(COL_L + 12, y + 44, hero_sub, hero_color, 170)
        if self.state and self.state.client_usage:
            self._draw_pill(COL_R - 94, y + 44, "\u672c\u5730", Theme.amber_bright, 76)
        y += 82

        # ════════════════════════════════════════════════════════
        #  ACTIVE ACCOUNTS
        # ════════════════════════════════════════════════════════
        c.create_text(COL_L, y, anchor="nw", text="\u5f53\u524d\u6d3b\u8dc3",
                       font=self._fonts["font_section"], fill=Theme.amber)

        y += 24
        if not accounts:
            c.create_text(COL_L + 8, y, anchor="nw", text="\u6682\u65e0\u6d3b\u8dc3\u8bf7\u6c42",
                           font=self._fonts["font_label"], fill=Theme.text_muted)
            y += 20
        for acc in accounts[:3]:
            name = self._truncate(acc.get("name", "-"), "font_label", 230)
            cur = acc.get("current", 0)
            mx = acc.get("max", 1)

            c.create_text(COL_L + 8, y, anchor="nw", text=name,
                           font=self._fonts["font_label"], fill=Theme.text_primary)
            frac_text = f"{compact_number(cur)}/{compact_number(mx)}"
            pill_w = 54
            self._draw_rounded_rect(COL_R - pill_w, y - 2, COL_R - 4, y + 21, r=8,
                                    fill=Theme.bg_section, outline=Theme.border)
            c.create_text(COL_R - 4 - pill_w / 2, y + 9, anchor="center", text=frac_text,
                           font=self._fonts["font_label_bold"], fill=Theme.accent_green)
            y += 26

        y += 4
        c.create_line(COL_L, y, COL_R, y, fill=Theme.border, width=1)

        # ════════════════════════════════════════════════════════
        #  LATEST REQUEST
        # ════════════════════════════════════════════════════════
        y += 10
        c.create_text(COL_L, y, anchor="nw", text="\u6700\u8fd1\u8bf7\u6c42",
                       font=self._fonts["font_section"], fill=Theme.amber)
        y += 22

        if self.state and self.state.latest_request:
            req = self.state.latest_request
            kind = req.get("kind", "-")
            model = req.get("model", "-")
            speed_badge = str(req.get("speed_badge") or "")
            if speed_badge:
                model = f"{model} / {speed_badge}"
            created = req.get("created_at", "")
            acct = self.state.latest_account_name or "-"
            status_text = "\u9519\u8bef" if kind == "error" else ("\u6210\u529f" if kind else "-")
            status_color = Theme.accent_red if kind == "error" else Theme.accent_green

            label_pairs = [
                ("\u8d26\u53f7", self._truncate(acct, "font_label", 200)),
                ("\u72b6\u6001", status_text),
                ("\u6a21\u578b", model),
                ("\u65f6\u95f4", relative_time(created) if created else "-"),
            ]
            for lbl, val in label_pairs:
                c.create_text(COL_L + 8, y, anchor="nw", text=lbl,
                               font=self._fonts["font_tiny"], fill=Theme.text_muted)
                value_color = status_color if lbl == "\u72b6\u6001" else (Theme.cyan if lbl == "\u6a21\u578b" else Theme.text_primary)
                c.create_text(COL_L + 64, y, anchor="nw",
                               text=self._truncate(val, "font_label", COL_R - COL_L - 72),
                               font=self._fonts["font_label"], fill=value_color)
                y += 19
        else:
            c.create_text(COL_L + 8, y, anchor="nw", text="\u6682\u65e0\u8bf7\u6c42\u8bb0\u5f55",
                           font=self._fonts["font_label"], fill=Theme.text_muted)
            y += 22

        y += 4
        c.create_line(COL_L, y, COL_R, y, fill=Theme.border, width=1)

        # ════════════════════════════════════════════════════════
        #  TODAY STATS
        # ════════════════════════════════════════════════════════
        y += 10
        c.create_text(COL_L, y, anchor="nw", text="\u4eca\u65e5\u7edf\u8ba1",
                       font=self._fonts["font_section"], fill=Theme.amber)
        if self.state and self.state.client_usage:
            client_tokens = int(self.state.client_usage.get("tokens") or 0)
            client_requests = int(self.state.client_usage.get("requests") or 0)
            if client_tokens or client_requests:
                source_text = f"\u672c\u5730\u603b\u91cf {compact_number(client_tokens)} tok"
                c.create_text(COL_R, y + 1, anchor="ne", text=source_text,
                               font=self._fonts["font_tiny"], fill=Theme.text_secondary)
        elif self.state and self.state.usage_note:
            c.create_text(COL_R, y + 1, anchor="ne",
                           text=self._truncate(self.state.usage_note, "font_tiny", 180),
                           font=self._fonts["font_tiny"], fill=Theme.text_secondary)
        y += 24

        stats = [
            ("\u8bf7\u6c42", compact_number(self.state.today_requests) if self.state else "0", Theme.amber_bright),
            ("Token", compact_number(self.state.today_tokens) if self.state else "0", Theme.cyan),
            ("\u6210\u672c", money(self.state.today_account_cost) if self.state else "$0", Theme.violet),
        ]
        col_w = (COL_R - COL_L) // 3
        for i, (lbl, val, color) in enumerate(stats):
            cx = COL_L + col_w * i + col_w // 2
            c.create_text(cx, y, anchor="n", text=val,
                           font=self._fonts["font_value"], fill=color)
            c.create_text(cx, y + 26, anchor="n", text=lbl,
                           font=self._fonts["font_tiny"], fill=Theme.text_secondary)

        y += 50
        c.create_line(COL_L, y, COL_R, y, fill=Theme.border, width=1)

        y += 10
        c.create_text(COL_L, y, anchor="nw", text="Token \u8d8b\u52bf",
                       font=self._fonts["font_section"], fill=Theme.amber)
        history = (self.state.cost_history if self.state else None) or summarize_trend_rows([])
        c.create_text(COL_R, y + 1, anchor="ne",
                       text=f"7\u65e5 {compact_number(history.get('seven_day_tokens', 0))} tok",
                       font=self._fonts["font_tiny"], fill=Theme.text_secondary)
        y += 22
        cost_stats = [
            ("\u4eca\u65e5", f"{compact_number(history.get('today_tokens', 0))} tok", Theme.amber_bright),
            ("\u6628\u65e5", f"{compact_number(history.get('yesterday_tokens', 0))} tok", Theme.cyan),
            ("\u65e5\u5747", f"{compact_number(float(history.get('seven_day_tokens') or 0) / 7)} tok", Theme.violet),
        ]
        for i, (lbl, val, color) in enumerate(cost_stats):
            cx = COL_L + col_w * i + col_w // 2
            c.create_text(cx, y, anchor="n", text=val,
                           font=self._fonts["font_value_sm"], fill=color)
            c.create_text(cx, y + 21, anchor="n", text=lbl,
                           font=self._fonts["font_micro"], fill=Theme.text_secondary)
        series = history.get("series") if isinstance(history, dict) else []
        if isinstance(series, list) and series:
            bar_y = y + 42
            bar_h = 22
            gap = 5
            bar_w = max(8, int((COL_R - COL_L - gap * 6) / 7))
            max_cost = max([float(item.get("tokens") or 0) for item in series if isinstance(item, dict)], default=0) or 1
            for index, item in enumerate(series[:7]):
                cost = float(item.get("tokens") or 0) if isinstance(item, dict) else 0
                x1 = COL_L + index * (bar_w + gap)
                x2 = min(COL_R, x1 + bar_w)
                fill_h = max(2, int(bar_h * min(1.0, cost / max_cost))) if cost > 0 else 2
                self._draw_rounded_rect(x1, bar_y, x2, bar_y + bar_h, r=3, fill=Theme.bg_dark, outline="")
                color = Theme.amber if index == 6 else (Theme.cyan if cost >= max_cost else Theme.blue)
                self._draw_rounded_rect(x1, bar_y + bar_h - fill_h, x2, bar_y + bar_h, r=3, fill=color, outline="")
            y += 72
        else:
            y += 46
        c.create_line(COL_L, y, COL_R, y, fill=Theme.border, width=1)

        # ════════════════════════════════════════════════════════
        #  TOP ACCOUNTS
        # ════════════════════════════════════════════════════════
        raw_top = list(self.state.top_accounts or []) if self.state else []
        range_key = {"5h": "window_5h", "7d": "window_7d", "cycle": "window_cycle"}.get(self._account_range)
        range_label = {
            "today": "\u4eca\u65e5",
            "5h": "\u6700\u8fd1 5 \u5c0f\u65f6",
            "7d": "\u6700\u8fd1 7 \u5929",
            "cycle": "\u5468\u671f",
        }.get(self._account_range, "\u4eca\u65e5")
        if range_key:
            top = []
            for account in raw_top:
                window = account.get(range_key)
                if not isinstance(window, dict) or not window:
                    continue
                window_tokens = int(window.get("tokens") or 0)
                window_requests = int(window.get("requests") or 0)
                window_cost = float(window.get("cost") or 0)
                has_quota = bool(window.get("quota_available", window.get("utilization") is not None))
                if window_tokens <= 0 and window_requests <= 0 and window_cost <= 0 and not has_quota:
                    continue
                item = dict(account)
                item["tokens"] = window_tokens
                item["requests"] = window_requests
                item["cost"] = window_cost
                item["utilization"] = window.get("utilization")
                item["remaining_percent"] = window.get("remaining_percent")
                item["resets_at"] = str(window.get("resets_at") or "")
                item["quota_available"] = has_quota
                item["quota_stale"] = bool(window.get("quota_stale"))
                top.append(item)
            top.sort(key=lambda row: (-row["tokens"], -row["requests"], row["name"]))
        else:
            top = raw_top

        y += 9
        c.create_text(COL_L, y + 2, anchor="nw", text="\u8d26\u53f7\u7528\u91cf",
                       font=self._fonts["font_section"], fill=Theme.amber)
        c.create_text(COL_L + 67, y + 5, anchor="nw", text=f"{len(top)} \u4e2a\u8d26\u53f7",
                      font=self._fonts["font_micro"], fill=Theme.text_muted)

        tab_specs = [
            ("rank_today", "\u4eca\u65e5", "today"),
            ("rank_5h", "\u8fd1 5 \u5c0f\u65f6", "5h"),
            ("rank_7d", "\u8fd1 7 \u5929", "7d"),
            ("rank_cycle", "\u5468\u671f", "cycle"),
        ]
        tab_w = 52
        tab_gap = 3
        tab_h = 21
        tabs_x = COL_R - (tab_w * len(tab_specs) + tab_gap * (len(tab_specs) - 1))
        for tab_index, (button_name, label, value) in enumerate(tab_specs):
            x1 = tabs_x + tab_index * (tab_w + tab_gap)
            x2 = x1 + tab_w
            self._btn_rects[button_name] = (x1, y - 1, x2, y - 1 + tab_h)
            selected = self._account_range == value
            hovered = self._hover_btn == button_name
            fill = Theme.bg_lift if selected else (Theme.bg_hover if hovered else Theme.bg_dark)
            outline = Theme.amber_dim if selected else Theme.border
            text_color = Theme.amber_bright if selected else (Theme.text_primary if hovered else Theme.text_secondary)
            self._draw_rounded_rect(x1, y - 1, x2, y - 1 + tab_h, r=6, fill=fill, outline=outline, width=1)
            c.create_text((x1 + x2) // 2, y + 9, anchor="center", text=label,
                          font=self._fonts["font_micro"], fill=text_color)
        y += 27

        if not top:
            empty_text = "\u8be5\u65f6\u95f4\u8303\u56f4\u6682\u65e0\u8d26\u53f7\u8bb0\u5f55" if range_key else "\u6682\u65e0\u7528\u91cf"
            c.create_text(COL_L + 8, y, anchor="nw", text=empty_text,
                          font=self._fonts["font_label"], fill=Theme.text_muted)
        window_mode = bool(range_key)
        row_h = 50 if window_mode else 29
        available_rank_rows = max(1, (H - 44 - y) // row_h)
        max_rank_rows = min(len(top), available_rank_rows)
        display_top = list(top[:max_rank_rows])
        for index, acc in enumerate(display_top):
            health_badge = str(acc.get("health_badge") or "")
            source_badge = str(acc.get("source_badge") or "")
            speed_badge = str(acc.get("speed_badge") or "")
            source_label = "\u672c\u5730" if source_badge == "LOCAL" else ("SUB2" if source_badge == "SUB" else source_badge)
            source_w = self._text_width(source_label, "font_micro") + 14 if source_label else 0
            speed_w = self._text_width(speed_badge, "font_micro") + 14 if speed_badge else 0
            badges_w = (source_w + 7 if source_label else 0) + (speed_w + 7 if speed_badge else 0)
            name_x = COL_L + 8 + badges_w
            metric_start_x = COL_R - (76 if window_mode else 150)
            name_max_w = max(60, metric_start_x - name_x - 6)
            name = self._truncate(
                ranking_account_display_name(str(acc.get("name") or "-")),
                "font_label",
                name_max_w,
            )
            tokens = compact_number(acc.get("tokens", 0))
            reqs = compact_number(acc.get("requests", 0))
            cost = money(acc.get("cost", 0))
            utilization = acc.get("utilization")
            remaining_percent = acc.get("remaining_percent")
            if remaining_percent is None and utilization is not None:
                try:
                    remaining_percent = max(0.0, min(100.0, 100.0 - float(utilization)))
                except (TypeError, ValueError):
                    remaining_percent = None
            quota_available = bool(acc.get("quota_available")) if window_mode else False
            quota_stale = bool(acc.get("quota_stale")) if window_mode else False
            cycle_window = acc.get("window_cycle") if isinstance(acc.get("window_cycle"), dict) else {}
            has_cycle_quota = bool(cycle_window.get("quota_available"))
            if window_mode and quota_available:
                bar_color = quota_color(utilization)
            else:
                bar_color = Theme.amber if index == 0 else (Theme.cyan if index == 1 else (Theme.violet if index == 2 else Theme.blue))

            marker_bottom = y + (42 if window_mode else 23)
            c.create_rectangle(COL_L, y + 2, COL_L + 3, marker_bottom, fill=bar_color, outline="")
            if source_label:
                self._draw_health_badge(COL_L + 8, y + 1, source_label)
            if speed_badge:
                speed_x = COL_L + 8 + (source_w + 4 if source_label else 0)
                self._draw_health_badge(speed_x, y + 1, speed_badge)
            c.create_text(name_x, y, anchor="nw", text=name,
                          font=self._fonts["font_label"], fill=Theme.text_primary)

            if window_mode:
                if quota_available:
                    try:
                        percent_value = float(utilization)
                        percentage_text = f"\u5df2\u7528 {percent_value:.0f}%"
                    except (TypeError, ValueError):
                        percentage_text = "--%"
                elif has_cycle_quota and self._account_range in {"5h", "7d"}:
                    percentage_text = "\u5468\u671f\u8d26\u53f7"
                else:
                    percentage_text = "\u6682\u65e0\u989d\u5ea6"
                percentage_color = Theme.text_muted if quota_stale or not quota_available else bar_color
                if has_cycle_quota and not quota_available and self._account_range in {"5h", "7d"}:
                    percentage_color = Theme.amber_bright
                if quota_stale or not quota_available:
                    percentage_fill = Theme.bg_lift
                    percentage_outline = Theme.border
                elif percentage_color == Theme.accent_red:
                    percentage_fill = Theme.quota_red_bg
                    percentage_outline = Theme.accent_red
                elif percentage_color == Theme.amber_bright:
                    percentage_fill = Theme.quota_amber_bg
                    percentage_outline = Theme.amber_bright
                else:
                    percentage_fill = Theme.quota_green_bg
                    percentage_outline = Theme.accent_green
                pill_w = self._text_width(percentage_text, "font_label_bold") + 15
                pill_x1 = COL_R - max(56, pill_w)
                pill_x2 = COL_R - 2
                self._draw_rounded_rect(pill_x1, y - 2, pill_x2, y + 18,
                                        r=6, fill=percentage_fill, outline=percentage_outline, width=1)
                c.create_text((pill_x1 + pill_x2) // 2, y + 8, anchor="center", text=percentage_text,
                              font=self._fonts["font_label_bold"], fill=percentage_color)

                metric_text = f"{tokens} Token  \u00b7  {cost}"
                c.create_text(name_x, y + 20, anchor="nw", text=metric_text,
                              font=self._fonts["font_label_bold"], fill=Theme.cyan)
                if health_badge:
                    right_detail = health_badge
                    right_color = self._health_color(health_badge)
                elif quota_stale:
                    right_detail = "\u989d\u5ea6\u5f85\u5237\u65b0"
                    right_color = Theme.amber_bright
                elif has_cycle_quota and not quota_available and self._account_range in {"5h", "7d"}:
                    right_detail = "\u770b\u5468\u671f\u9875"
                    right_color = Theme.amber_bright
                elif not quota_available:
                    right_detail = "\u65e0\u8be5\u7a97\u53e3\u989d\u5ea6"
                    right_color = Theme.text_muted
                else:
                    try:
                        remaining_text = f"{float(remaining_percent):.0f}%"
                    except (TypeError, ValueError):
                        remaining_text = "--%"
                    right_detail = f"\u5269\u4f59 {remaining_text} \u00b7 {reqs} \u6b21"
                    right_color = Theme.text_muted
                c.create_text(COL_R - 4, y + 20, anchor="ne", text=right_detail,
                              font=self._fonts["font_micro"], fill=right_color)

                if quota_available:
                    reset_text = quota_reset_text(str(acc.get("resets_at") or ""))
                    if self._account_range == "7d" and reset_text:
                        reset_text = f"\u5468\u9650\u989d \u00b7 {reset_text}"
                    elif self._account_range == "cycle" and reset_text:
                        reset_text = f"\u5468\u671f \u00b7 {reset_text}"
                    elif self._account_range == "5h" and reset_text:
                        reset_text = f"5h \u9650\u989d \u00b7 {reset_text}"
                elif self._account_range == "7d":
                    reset_text = "\u8be5\u8d26\u53f7\u4f7f\u7528\u5468\u671f\u989d\u5ea6" if has_cycle_quota else "\u672a\u63d0\u4f9b\u5468\u989d\u5ea6"
                elif self._account_range == "cycle":
                    reset_text = "\u672a\u63d0\u4f9b\u5468\u671f\u989d\u5ea6"
                else:
                    reset_text = "\u6682\u65e0\u989d\u5ea6\u6570\u636e"
                reset_w = self._text_width(reset_text, "font_micro") if reset_text else 0
                progress_x1 = name_x
                progress_x2 = max(progress_x1 + 42, COL_R - reset_w - 14)
                progress_y = y + 41
                self._draw_rounded_rect(progress_x1, progress_y, progress_x2, progress_y + 4,
                                        r=2, fill=Theme.bg_lift, outline="")
                if quota_available:
                    try:
                        ratio = max(0.0, min(1.0, float(utilization) / 100.0))
                    except (TypeError, ValueError):
                        ratio = 0.0
                    if ratio > 0:
                        fill_x2 = progress_x1 + max(3, int((progress_x2 - progress_x1) * ratio))
                        self._draw_rounded_rect(progress_x1, progress_y, fill_x2, progress_y + 4,
                                                r=2, fill=bar_color, outline="")
                if reset_text:
                    c.create_text(COL_R - 4, y + 33, anchor="ne", text=reset_text,
                                  font=self._fonts["font_micro"], fill=Theme.text_muted)
                c.create_line(COL_L + 8, y + 48, COL_R - 4, y + 48, fill=Theme.border, width=1)
            else:
                cost_w = self._text_width(cost, "font_label_bold")
                c.create_text(COL_R - 4, y, anchor="ne", text=cost,
                              font=self._fonts["font_label_bold"], fill=Theme.amber_bright)
                c.create_text(COL_R - 12 - cost_w, y, anchor="ne", text=f"{tokens} Token",
                              font=self._fonts["font_label_bold"], fill=bar_color)

                detail_text = f"{range_label}  \u00b7  {reqs} \u6b21\u8bf7\u6c42"
                c.create_text(name_x, y + 15, anchor="nw", text=detail_text,
                              font=self._fonts["font_micro"], fill=Theme.text_muted)
                if health_badge:
                    c.create_text(COL_R - 4, y + 15, anchor="ne", text=health_badge,
                                  font=self._fonts["font_micro"], fill=self._health_color(health_badge))
                c.create_line(COL_L + 8, y + 27, COL_R - 4, y + 27, fill=Theme.border, width=1)
            y += row_h

        # ── footer timestamp ──
        now_str = datetime.now(CN_TZ).strftime("%H:%M:%S UTC+8")
        c.create_text(W // 2, H - 10, anchor="s", text=now_str,
                       font=self._fonts["font_tiny"], fill=Theme.text_muted)
        c.create_line(W - 18, H - 7, W - 7, H - 18, fill=Theme.border, width=1)
        c.create_line(W - 13, H - 7, W - 7, H - 13, fill=Theme.text_muted, width=1)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  ANIMATION
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _fade_in(self) -> None:
        if self.closed:
            return
        if self._fade_alpha < self.WINDOW_ALPHA:
            self._fade_alpha = min(self._fade_alpha + 0.06, self.WINDOW_ALPHA)
            self.root.attributes("-alpha", self._fade_alpha)
            self.root.after(16, self._fade_in)
        else:
            self.root.attributes("-alpha", self.WINDOW_ALPHA)

    def _pulse_tick(self) -> None:
        if self.closed or not self._loading:
            return
        self._pulse_phase += 0.25
        self._draw()
        self.root.after(60, self._pulse_tick)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  DRAG
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _hit_button(self, x: int, y: int) -> str | None:
        for name, (x1, y1, x2, y2) in self._btn_rects.items():
            if x1 <= x <= x2 and y1 <= y <= y2:
                return name
        return None

    def _hit_resize_handle(self, x: int, y: int) -> bool:
        return x >= self.WIDTH - 24 and y >= self.HEIGHT - 24

    def _on_press(self, event: tk.Event) -> None:
        btn = self._hit_button(event.x, event.y)
        if btn == "btn_close":
            self.close_app()
            return
        if btn == "btn_pin":
            self._pinned = not self._pinned
            self.root.attributes("-topmost", self._pinned)
            self._draw()
            return
        if btn == "btn_refresh":
            self.client.clear_client_usage_cache()
            self.refresh_async()
            return
        if btn in {"rank_today", "rank_5h", "rank_7d", "rank_cycle"}:
            self._account_range = {
                "rank_today": "today",
                "rank_5h": "5h",
                "rank_7d": "7d",
                "rank_cycle": "cycle",
            }[btn]
            self._account_range_user_selected = True
            self._draw()
            return
        if self._hit_resize_handle(event.x, event.y):
            self._resizing = True
            self._resize_data = {"x": event.x_root, "y": event.y_root, "w": self.WIDTH, "h": self.HEIGHT}
            return
        self._resizing = False
        self._drag_data["x"] = event.x
        self._drag_data["y"] = event.y

    def _on_drag(self, event: tk.Event) -> None:
        if self._resizing:
            new_w = max(self.MIN_WIDTH, self._resize_data["w"] + event.x_root - self._resize_data["x"])
            new_h = max(self.MIN_HEIGHT, self._resize_data["h"] + event.y_root - self._resize_data["y"])
            self.WIDTH = int(new_w)
            self.HEIGHT = int(new_h)
            self.root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{self.root.winfo_x()}+{self.root.winfo_y()}")
            self.canvas.configure(width=self.WIDTH, height=self.HEIGHT)
            self._draw()
            return
        dx = event.x - self._drag_data["x"]
        dy = event.y - self._drag_data["y"]
        x = self.root.winfo_x() + dx
        y = self.root.winfo_y() + dy
        self.root.geometry(f"+{x}+{y}")

    def _on_motion(self, event: tk.Event) -> None:
        if self._hit_resize_handle(event.x, event.y):
            self.canvas.configure(cursor="size_nw_se")
        else:
            self.canvas.configure(cursor="")
        btn = self._hit_button(event.x, event.y)
        if btn != self._hover_btn:
            self._hover_btn = btn
            self._draw()

    def _on_leave(self, _event: tk.Event) -> None:
        self.canvas.configure(cursor="")
        if self._hover_btn is not None:
            self._hover_btn = None
            self._draw()

    def _on_focus_in(self, _event: tk.Event) -> None:
        self._ensure_topmost()

    def _on_visibility(self, _event: tk.Event) -> None:
        self._ensure_topmost()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  DATA REFRESH
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _maybe_select_cycle_range(self, state: MonitorState) -> None:
        if self._account_range_user_selected or self._account_range_auto_selected:
            return
        if self._account_range != "today":
            return
        latest = str(state.latest_account_name or "").replace("LOCAL - ", "")
        for account in state.top_accounts or []:
            window = account.get("window_cycle") if isinstance(account, dict) else None
            if not isinstance(window, dict) or not window.get("quota_available"):
                continue
            name = str(account.get("name") or "")
            if latest and (name in latest or latest in name):
                self._account_range = "cycle"
                self._account_range_auto_selected = True
                return

    def refresh_async(self) -> None:
        if not self._refresh_lock.acquire(blocking=False):
            return
        self._loading = True
        self._draw()
        self._pulse_tick()

        def _worker() -> None:
            err = None
            try:
                result = self.client.fetch_state()
            except Exception as exc:
                result = None
                err = f"\u8bf7\u6c42\u5931\u8d25: {exc}"
            self.root.after(0, lambda: self._apply_state(result, err))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _apply_state(self, result: MonitorState | None, error: str | None = None) -> None:
        self._loading = False
        try:
            self._refresh_lock.release()
        except RuntimeError:
            pass
        self.error = error
        if result is not None:
            try:
                result.cost_history = update_usage_history(result)
            except Exception:
                result.cost_history = summarize_usage_history(load_usage_history())
            self.state = result
            self._maybe_select_cycle_range(result)
        self._ensure_topmost()
        self._schedule_topmost_repair()
        self._draw()

    def _schedule_auto_refresh(self) -> None:
        if self.closed:
            return
        self.refresh_async()
        self.root.after(REFRESH_SECONDS * 1000, self._schedule_auto_refresh)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  LIFECYCLE
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def close_app(self) -> None:
        self.closed = True
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
if __name__ == "__main__":
    FloatingMonitorApp().run()
