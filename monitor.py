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
CLIENT_USAGE_EXPORT_TIMEOUT_SECONDS = int(os.environ.get("SUB2API_CLIENT_USAGE_EXPORT_TIMEOUT_SECONDS", "90"))
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
if Path(CLIENT_USAGE_PYTHON).name.lower() == "pythonw.exe":
    console_python = Path(CLIENT_USAGE_PYTHON).with_name("python.exe")
    if console_python.exists():
        CLIENT_USAGE_PYTHON = str(console_python)
USAGE_HISTORY_JSON = Path(os.environ.get("SUB2API_USAGE_HISTORY_JSON") or APP_DIR / "usage_history.json")
CLIENT_USAGE_ROUTE_LABELS_JSON = Path(
    os.environ.get("CLIENT_USAGE_ROUTE_LABELS_JSON") or APP_DIR / "client_usage_route_labels.json"
)
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


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def token_mix_from_client_usage(client_usage: dict[str, Any] | None) -> dict[str, int]:
    mix = {
        "input": 0,
        "cached": 0,
        "cache_create": 0,
        "output": 0,
    }
    if not isinstance(client_usage, dict):
        return mix
    providers = client_usage.get("providers")
    rows = providers if isinstance(providers, list) else [client_usage]
    for row in rows:
        if not isinstance(row, dict):
            continue
        mix["input"] += int(row.get("input_tokens") or 0)
        mix["cached"] += int(row.get("cached_input_tokens") or 0)
        mix["cache_create"] += int(row.get("cache_creation_input_tokens") or 0)
        mix["output"] += int(row.get("output_tokens") or 0)
    if isinstance(providers, list):
        mix["input"] = max(mix["input"], int(client_usage.get("input_tokens") or 0))
        mix["cached"] = max(mix["cached"], int(client_usage.get("cached_input_tokens") or 0))
        mix["cache_create"] = max(mix["cache_create"], int(client_usage.get("cache_creation_input_tokens") or 0))
        mix["output"] = max(mix["output"], int(client_usage.get("output_tokens") or 0))
    return mix


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
    mix = token_mix_from_client_usage(state.client_usage if isinstance(state.client_usage, dict) else None)

    # Same-day client usage is reconstructed from local logs and account
    # markers. Account switches can briefly make attribution smaller than the
    # previous snapshot, so keep a high-water total for the current day.
    use_local_high_water = state.usage_source in {"local", "client", "local-codex"}
    if use_local_high_water and existing_source_date in {"", source_date, key}:
        if existing_tokens > new_tokens and existing_tokens >= max(1, int(new_tokens * 1.05)):
            new_tokens = existing_tokens
            new_requests = max(new_requests, existing_requests)
            new_cost = max(new_cost, existing_cost)
            mix["input"] = max(mix["input"], int(existing.get("input_tokens") or 0))
            mix["cached"] = max(mix["cached"], int(existing.get("cached_input_tokens") or 0))
            mix["cache_create"] = max(mix["cache_create"], int(existing.get("cache_creation_input_tokens") or 0))
            mix["output"] = max(mix["output"], int(existing.get("output_tokens") or 0))

    days[key] = {
        "date": key,
        "source": state.usage_source,
        "requests": new_requests,
        "tokens": new_tokens,
        "input_tokens": mix["input"],
        "cached_input_tokens": mix["cached"],
        "cache_creation_input_tokens": mix["cache_create"],
        "output_tokens": mix["output"],
        "cost": round(new_cost, 6),
        "updated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "source_date": source_date,
    }
    try:
        write_json_atomic(USAGE_HISTORY_JSON, history)
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
    providers = client_usage.get("providers")
    providers = providers if isinstance(providers, list) else []
    active: list[dict[str, Any]] = []
    for index, provider in enumerate(providers):
        if not isinstance(provider, dict):
            continue
        latest_at = str(provider.get("latest_at") or "")
        if not is_recent_activity(latest_at):
            continue
        provider_name = str(provider.get("name") or "Local client")
        recent_sessions = max(1, int(provider.get("recent_sessions") or 0))
        active.append(
            {
                "id": f"local-{index}",
                "name": f"LOCAL - {local_provider_display_name(provider_name)}",
                "current": recent_sessions,
                "max": recent_sessions,
                "model": provider.get("latest_model") or "-",
                "source": "LOCAL",
                "speed_badge": provider.get("speed_badge") or "",
                "latest_at": latest_at,
            }
        )
    if active:
        active.sort(key=lambda row: _parse_time(str(row.get("latest_at") or "")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return active

    client_latest = client_usage.get("latest_request")
    if not isinstance(client_latest, dict) or not client_latest.get("created_at"):
        return []
    if not is_recent_activity(str(client_latest.get("created_at") or "")):
        return []
    provider_name = str(client_latest.get("provider") or "Local client")
    latest_provider = next(
        (provider for provider in providers if isinstance(provider, dict) and str(provider.get("name") or "") == provider_name),
        {},
    )
    return [
        {
            "id": "local-latest",
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
            "dashboard": data.get("dashboard") if isinstance(data.get("dashboard"), dict) else {},
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
        "dashboard": data.get("dashboard") if isinstance(data.get("dashboard"), dict) else {},
        "updated_at": data.get("updated_at") or "",
        "date": data_date,
        "stale": False,
    }


def latest_request_from_client_providers(providers: list[dict[str, Any]]) -> dict[str, Any]:
    latest_provider = ""
    latest_model = ""
    latest_at = ""
    latest_dt: datetime | None = None
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_latest_at = str(provider.get("latest_at") or "")
        provider_dt = _parse_time(provider_latest_at)
        if provider_dt is None:
            continue
        if latest_dt is None or provider_dt > latest_dt:
            latest_dt = provider_dt
            latest_provider = str(provider.get("name") or "Local client")
            latest_model = str(provider.get("latest_model") or "-")
            latest_at = provider_latest_at
    if not latest_at:
        return {}
    return {
        "provider": latest_provider,
        "model": latest_model,
        "created_at": latest_at,
        "kind": "success",
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
    latest = client_usage.get("latest_request")
    if isinstance(latest, dict) and str(latest.get("provider") or "") == provider_name:
        result["latest_request"] = latest_request_from_client_providers(kept)
    return result


def subtract_sub2api_routed_client_usage(client_usage: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(client_usage, dict):
        return client_usage
    providers = client_usage.get("providers")
    if not isinstance(providers, list):
        return client_usage

    result = client_usage
    routed_names = [
        str(provider.get("name") or "")
        for provider in providers
        if isinstance(provider, dict)
        and (
            provider.get("routed_to_sub2api") is True
            or str(provider.get("name") or "").strip().lower() == "codex via sub2api"
        )
    ]
    for name in routed_names:
        result = subtract_provider_from_client_usage(result, name)
    return result


def is_local_api_key_provider_name(name: str) -> bool:
    return name.strip().lower().startswith("codex local - api-key-")


def load_client_route_labels() -> dict[str, set[str]]:
    empty = {"sub2api_mirrored": set(), "direct": set()}
    try:
        data = json.loads(CLIENT_USAGE_ROUTE_LABELS_JSON.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty
    result: dict[str, set[str]] = {}
    for key in empty:
        values = data.get(key)
        if isinstance(values, list):
            result[key] = {str(value) for value in values if str(value).strip()}
        else:
            result[key] = set()
    return result


def write_client_route_labels(labels: dict[str, set[str]]) -> None:
    payload = {
        "schema": 1,
        "updated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "sub2api_mirrored": sorted(labels.get("sub2api_mirrored", set())),
        "direct": sorted(labels.get("direct", set())),
    }
    try:
        CLIENT_USAGE_ROUTE_LABELS_JSON.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def update_client_route_label(provider_name: str, points_to_sub2api: bool | None) -> dict[str, set[str]]:
    labels = load_client_route_labels()
    if not is_local_api_key_provider_name(provider_name):
        return labels
    if points_to_sub2api is True:
        if provider_name not in labels["direct"]:
            labels["sub2api_mirrored"].add(provider_name)
            write_client_route_labels(labels)
    elif points_to_sub2api is False:
        labels["direct"].add(provider_name)
        labels["sub2api_mirrored"].discard(provider_name)
        write_client_route_labels(labels)
    return labels


def backfill_sub2api_mirrored_api_key_labels(
    client_usage: dict[str, Any] | None,
    server_tokens: int,
    current_provider_name: str,
    points_to_sub2api: bool | None,
    labels: dict[str, set[str]],
) -> dict[str, set[str]]:
    if not isinstance(client_usage, dict) or server_tokens <= 0:
        return labels
    providers = client_usage.get("providers")
    if not isinstance(providers, list):
        return labels

    changed = False
    token_ceiling = max(1, int(server_tokens * 1.25))
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        name = str(provider.get("name") or "")
        tokens = int(provider.get("tokens") or 0)
        if not is_local_api_key_provider_name(name) or tokens <= 0:
            continue
        if name in labels["direct"] or name in labels["sub2api_mirrored"]:
            continue
        if points_to_sub2api is False and name == current_provider_name:
            continue
        if tokens <= token_ceiling:
            labels["sub2api_mirrored"].add(name)
            changed = True
    if changed:
        write_client_route_labels(labels)
    return labels


def subtract_sub2api_mirrored_api_key_usage(
    client_usage: dict[str, Any] | None,
    server_tokens: int,
    route_labels: dict[str, set[str]] | None = None,
) -> dict[str, Any] | None:
    """Remove local API-key rows that mirror already-counted Sub2API traffic."""
    if not isinstance(client_usage, dict) or server_tokens <= 0:
        return client_usage
    providers = client_usage.get("providers")
    if not isinstance(providers, list):
        return client_usage

    result = client_usage
    mirrored = (route_labels or {}).get("sub2api_mirrored", set())
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        name = str(provider.get("name") or "")
        tokens = int(provider.get("tokens") or 0)
        if is_local_api_key_provider_name(name) and name in mirrored and tokens > 0:
            result = subtract_provider_from_client_usage(result, name)
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
    source_label: str = "MONITOR"
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
    client_latest = client_usage.get("latest_request") if isinstance(client_usage, dict) else {}
    latest_provider_name = str(client_latest.get("provider") or "") if isinstance(client_latest, dict) else ""
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
                    "active_now": False,
                    "is_latest": str(provider.get("name") or "") == latest_provider_name,
                }
            )
    top_accounts.sort(key=lambda row: (-row["tokens"], -row["requests"], row["name"]))

    updated_at = client_usage.get("updated_at") if isinstance(client_usage, dict) else ""
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
        source_label="MONITOR",
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

    def clear_runtime_caches(self) -> None:
        self.clear_client_usage_cache()
        self._account_window_cache.clear()

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
            current = int(item.get("current_in_use") or item.get("current_concurrency") or item.get("current") or item.get("in_use") or 0)
            if current <= 0:
                continue
            account_id = int(item.get("account_id") or 0)
            account_info = account_map.get(account_id, {})
            active_accounts.append(
                {
                    "id": account_id,
                    "name": account_info.get("name") or item.get("account_name") or f"账号 #{account_id}",
                    "current": current,
                    "max": int(item.get("max_capacity") or item.get("concurrency") or account_info.get("concurrency") or current),
                }
            )
        active_by_id = {int(row.get("id") or 0): row for row in active_accounts if row.get("id") is not None}
        for account in accounts:
            account_id = int(account.get("id") or 0)
            current = int(account.get("current_concurrency") or account.get("current_in_use") or 0)
            if current <= 0:
                continue
            existing = active_by_id.get(account_id)
            if existing:
                existing["current"] = max(int(existing.get("current") or 0), current)
                existing["max"] = max(int(existing.get("max") or 0), int(account.get("concurrency") or current))
                continue
            row = {
                "id": account_id,
                "name": account.get("name") or f"账号 #{account_id}",
                "current": current,
                "max": int(account.get("concurrency") or current),
            }
            active_accounts.append(row)
            active_by_id[account_id] = row
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
                    "active_now": any(int(row.get("id") or 0) == account_id for row in active_accounts),
                    "is_latest": bool(latest and int(latest.get("account_id") or 0) == account_id),
                }
            )
        top_accounts.sort(key=lambda row: (-row["tokens"], -row["requests"], row["name"]))
        include_client_usage = self._should_include_client_usage(resolved_source)
        points_to_sub2api, _codex_urls = self._codex_points_to_sub2api()
        show_local_activity = include_client_usage and points_to_sub2api is not True
        raw_client_usage = self._load_client_usage_cached() if include_client_usage else None
        client_usage = subtract_sub2api_routed_client_usage(raw_client_usage)
        route_labels = load_client_route_labels()
        raw_latest = raw_client_usage.get("latest_request") if isinstance(raw_client_usage, dict) else {}
        latest_provider_name = (
            str(raw_latest.get("provider") or "")
            if isinstance(raw_latest, dict)
            else ""
        )
        if latest_provider_name:
            route_labels = update_client_route_label(latest_provider_name, points_to_sub2api)
        route_labels = backfill_sub2api_mirrored_api_key_labels(
            client_usage,
            realtime_today_tokens,
            latest_provider_name,
            points_to_sub2api,
            route_labels,
        )
        client_usage = subtract_sub2api_mirrored_api_key_usage(
            client_usage,
            realtime_today_tokens,
            route_labels,
        )
        if (
            points_to_sub2api is True
            and isinstance(raw_client_usage, dict)
            and client_usage is raw_client_usage
        ):
            routed_provider = (
                str(raw_latest.get("provider") or "")
                if isinstance(raw_latest, dict)
                else ""
            )
            if routed_provider and routed_provider.lower().startswith("codex local - api-key-"):
                client_usage = subtract_provider_from_client_usage(client_usage, routed_provider)
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
                        "active_now": False,
                        "is_latest": str(provider.get("name") or "") == latest_provider_name,
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
            active_accounts=active_accounts,
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
    """Neutral charcoal palette with restrained periwinkle-blue data accents."""
    # ── base surfaces ──
    bg_dark = "#18181B"
    bg_card = "#202023"
    bg_section = "#26262B"
    bg_lift = "#2B2B31"
    bg_hover = "#39393F"

    # ── primary blue ramp ──
    amber_dim = "#405F98"
    amber = "#6E9DF8"
    amber_bright = "#F4F4F6"
    amber_glow = "#97B8F8"

    # ── secondary accents ──
    cyan = "#8DB2F8"
    cyan_dim = "#44444B"
    violet = "#B5C8F5"
    blue = "#4E7FE4"

    # ── text ──
    text_primary = "#F5F5F7"
    text_secondary = "#B8B8BF"
    text_muted = "#888891"

    # ── semantic ──
    accent_cyan = "#8DB2F8"
    accent_red = "#F06A6A"
    accent_green = "#68C995"
    quota_red_bg = "#412B30"
    quota_amber_bg = "#413829"
    quota_green_bg = "#263A34"
    ag_bg = "#29292E"
    ag_surface = "#27272C"
    ag_surface_hover = "#39393F"
    ag_border = "#35353B"
    ag_divider = "#303035"
    ag_accent = "#6E9DF8"
    ag_bar = "#87ACF5"
    ag_success = "#68C995"
    ag_warn = "#E2B45B"
    ag_crit = "#F06A6A"
    ag_muted = "#92929B"
    ag_input = "#91B4F8"
    ag_cache = "#6E9DF8"
    ag_output = "#4E7FE4"
    ag_reason = "#BBCDF8"

    # ── misc ──
    border = "#34343A"
    shadow = "#0E0E10"
    transparent = "#010203"

    # ── fonts (family, size, weight) ──
    font_title = ("Segoe UI", 18, "bold")
    font_section = ("Segoe UI", 11, "bold")
    font_label = ("Segoe UI", 10, "normal")
    font_label_bold = ("Segoe UI", 10, "bold")
    font_value = ("Segoe UI", 18, "bold")
    font_value_sm = ("Segoe UI", 14, "bold")
    font_tiny = ("Segoe UI", 9, "normal")
    font_micro = ("Segoe UI", 8, "normal")
    font_icon = ("Segoe UI", 13, "normal")


class FloatingMonitorApp:
    """Borderless always-on-top floating monitor built entirely on tk.Canvas."""

    WIDTH = 390
    HEIGHT = 760
    MIN_WIDTH = 360
    MIN_HEIGHT = 640
    WINDOW_ALPHA = 0.99

    def __init__(self) -> None:
        self.WIDTH = int(type(self).WIDTH)
        self.HEIGHT = int(type(self).HEIGHT)
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
        self._tooltip_rects: list[tuple[int, int, int, int, str]] = []
        self._tooltip_text = ""
        self._tooltip_pos = (0, 0)
        self._main_tab = "accounts"
        self._scroll_offsets = {"accounts": 0, "budget": 0, "stats": 0}
        self._scroll_limits = {"accounts": 0, "budget": 0, "stats": 0}
        self._usage_range = "24h"
        self._account_range = "today"
        self._account_range_user_selected = False
        self._account_range_auto_selected = False
        self._topmost_repair_scheduled = False
        self._ignore_configure = False
        self._current_day_key = today_key()

        # ── root window ──
        self.root = tk.Tk()
        self.root.title("Token Monitor")
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
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", self._on_mousewheel)
        self.canvas.bind("<Button-5>", self._on_mousewheel)
        self.root.bind("<ButtonPress-1>", self._on_press)
        self.root.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<MouseWheel>", self._on_mousewheel)
        self.root.bind_all("<ButtonPress-1>", self._on_press)

        # ── initial draw & data ──
        self._draw()
        self._fade_in()
        self.refresh_async()
        self._schedule_auto_refresh()
        self._schedule_midnight_refresh()

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

    def _apply_window_size(self, width: int, height: int) -> None:
        width = int(max(self.MIN_WIDTH, width))
        height = int(max(self.MIN_HEIGHT, height))
        self.WIDTH = width
        self.HEIGHT = height
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        self._ignore_configure = True
        try:
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            self.canvas.configure(width=width, height=height)
        finally:
            self.root.after_idle(self._clear_ignore_configure)

    def _clear_ignore_configure(self) -> None:
        self._ignore_configure = False

    def _text_width(self, text: str, font_key: str) -> int:
        return self._fonts[font_key].measure(text)

    def _add_tooltip(self, x1: int, y1: int, x2: int, y2: int, text: str) -> None:
        if text:
            self._tooltip_rects.append((int(x1), int(y1), int(x2), int(y2), text))

    def _hit_tooltip(self, x: int, y: int) -> str:
        for x1, y1, x2, y2, text in reversed(self._tooltip_rects):
            if x1 <= x <= x2 and y1 <= y <= y2:
                return text
        return ""

    def _draw_tooltip(self, W: int, H: int) -> None:
        if not self._tooltip_text:
            return
        lines = self._tooltip_text.split("\n")[:3]
        width = max(self._text_width(line, "font_micro") for line in lines) + 18
        height = 18 * len(lines) + 8
        x = min(max(8, self._tooltip_pos[0] + 12), max(8, W - width - 8))
        y = min(max(8, self._tooltip_pos[1] + 14), max(8, H - height - 8))
        self._draw_rounded_rect(x, y, x + width, y + height, r=6,
                                fill=Theme.bg_lift, outline=Theme.ag_accent, width=1)
        for index, line in enumerate(lines):
            self.canvas.create_text(x + 9, y + 7 + index * 18, anchor="nw",
                                    text=line, font=self._fonts["font_micro"], fill=Theme.text_primary)

    def _ensure_topmost(self, force: bool = False, raise_window: bool = False) -> None:
        if not self._pinned and not force:
            return
        try:
            self.root.attributes("-topmost", True)
            if raise_window:
                self.root.deiconify()
                self.root.lift()
        except tk.TclError:
            pass

    def _schedule_topmost_repair(self) -> None:
        # Do not periodically lift/reassert topmost. Screenshot overlays are
        # often topmost windows too; repeated reassertion can jump above them.
        return

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

    def _draw_footer(self, W: int, H: int) -> None:
        now_str = datetime.now(CN_TZ).strftime("%H:%M:%S UTC+8")
        self.canvas.create_text(W // 2, H - 10, anchor="s", text=now_str,
                                font=self._fonts["font_tiny"], fill=Theme.text_muted)
        self.canvas.create_line(W - 18, H - 7, W - 7, H - 18, fill=Theme.border, width=1)
        self.canvas.create_line(W - 13, H - 7, W - 7, H - 13, fill=Theme.text_muted, width=1)

    def _draw_main_tabs(self, col_l: int, col_r: int, y: int) -> int:
        tabs = [
            ("main_accounts", "\u8d26\u53f7", "accounts"),
            ("main_budget", "Token \u9884\u7b97", "budget"),
            ("main_stats", "\u7528\u91cf\u7edf\u8ba1", "stats"),
        ]
        gap = 5
        total_gap = gap * (len(tabs) - 1)
        tab_w = max(62, (col_r - col_l - total_gap) // len(tabs))
        tab_h = 25
        for index, (button_name, label, value) in enumerate(tabs):
            x1 = col_l + index * (tab_w + gap)
            x2 = col_r if index == len(tabs) - 1 else x1 + tab_w
            self._btn_rects[button_name] = (x1, y, x2, y + tab_h)
            selected = self._main_tab == value
            hovered = self._hover_btn == button_name
            fill = Theme.bg_hover if selected else (Theme.ag_surface_hover if hovered else Theme.ag_bg)
            outline = Theme.bg_hover if selected else Theme.ag_border
            text_color = Theme.text_primary if selected else (Theme.text_primary if hovered else Theme.ag_muted)
            self._draw_rounded_rect(x1, y, x2, y + tab_h, r=7, fill=fill, outline=outline, width=1)
            self.canvas.create_text((x1 + x2) // 2, y + 12, anchor="center", text=label,
                                    font=self._fonts["font_label_bold"], fill=text_color)
        return y + tab_h + 10

    def _draw_ag_section(self, col_l: int, col_r: int, y: int, title: str, badge: str = "") -> int:
        self.canvas.create_text(col_l, y, anchor="nw", text=title,
                                font=self._fonts["font_section"], fill=Theme.text_primary)
        if badge:
            bw = self._text_width(badge, "font_micro") + 14
            self._draw_rounded_rect(col_r - bw, y - 1, col_r, y + 18, r=6,
                                    fill=Theme.ag_surface, outline=Theme.ag_border)
            self.canvas.create_text(col_r - bw // 2, y + 8, anchor="center", text=badge,
                                    font=self._fonts["font_micro"], fill=Theme.ag_muted)
        return y + 24

    def _draw_donut(self, x: int, y: int, size: int, pct: float, color: str, label: str) -> None:
        pct = max(0.0, min(100.0, float(pct or 0)))
        pad = 5
        self.canvas.create_oval(x + pad, y + pad, x + size - pad, y + size - pad,
                                outline=Theme.ag_border, width=5)
        if pct > 0:
            self.canvas.create_arc(
                x + pad,
                y + pad,
                x + size - pad,
                y + size - pad,
                start=90,
                extent=-360 * pct / 100,
                style="arc",
                outline=color,
                width=5,
            )
        self.canvas.create_text(x + size // 2, y + size // 2, anchor="center",
                                text=label, font=self._fonts["font_label_bold"], fill=color)

    def _draw_ag_chip(self, x: int, y: int, text: str, dot: str | None = None) -> int:
        width = self._text_width(text, "font_micro") + (24 if dot else 14)
        self._draw_rounded_rect(x, y, x + width, y + 20, r=8, fill=Theme.ag_surface, outline=Theme.ag_border)
        tx = x + 7
        if dot:
            self.canvas.create_oval(x + 7, y + 7, x + 13, y + 13, fill=dot, outline="")
            tx += 12
        self.canvas.create_text(tx, y + 4, anchor="nw", text=text,
                                font=self._fonts["font_micro"], fill=Theme.text_secondary)
        return width

    @staticmethod
    def _ag_quota_color(utilization: float | int | None) -> str:
        try:
            value = float(utilization or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value >= 90:
            return Theme.ag_crit
        if value >= 60:
            return Theme.ag_warn
        return Theme.ag_success

    @staticmethod
    def _activity_color(intensity: float) -> str:
        if intensity <= 0:
            return "#2D2D32"
        if intensity < 0.18:
            return "#3D4C6B"
        if intensity < 0.38:
            return "#5276B7"
        if intensity < 0.68:
            return "#6E9DF8"
        if intensity < 0.9:
            return "#91B4F8"
        return "#3975E5"

    def _trend_token_color(self, intensity: float, is_today: bool = False) -> str:
        if is_today:
            return Theme.ag_accent
        return self._activity_color(intensity)

    def _client_providers(self) -> list[dict[str, Any]]:
        if not self.state or not isinstance(self.state.client_usage, dict):
            return []
        providers = self.state.client_usage.get("providers")
        if not isinstance(providers, list):
            return []
        return [provider for provider in providers if isinstance(provider, dict)]

    def _token_mix(self) -> dict[str, int]:
        return token_mix_from_client_usage(self.state.client_usage if self.state else None)

    def _summary_token_mix(self, summary: dict[str, Any]) -> dict[str, int]:
        mix = {
            "input": int(summary.get("input_tokens") or 0),
            "cached": int(summary.get("cached_input_tokens") or 0),
            "cache_create": int(summary.get("cache_creation_input_tokens") or 0),
            "output": int(summary.get("output_tokens") or 0),
        }
        known = sum(mix.values())
        mix["unknown"] = max(0, int(summary.get("tokens") or 0) - known)
        return mix

    def _top_models(self) -> list[tuple[str, int]]:
        totals: dict[str, int] = {}
        for provider in self._client_providers():
            models = provider.get("models")
            if not isinstance(models, dict):
                continue
            for model, tokens in models.items():
                try:
                    amount = int(tokens or 0)
                except (TypeError, ValueError):
                    amount = 0
                if amount > 0:
                    name = str(model or "unknown")
                    totals[name] = totals.get(name, 0) + amount
        return sorted(totals.items(), key=lambda item: item[1], reverse=True)[:6]

    def _usage_range_summary(self, range_key: str) -> dict[str, Any]:
        if range_key == "24h":
            hourly: list[dict[str, Any]] = []
            if self.state and isinstance(self.state.client_usage, dict):
                dashboard = self.state.client_usage.get("dashboard")
                if isinstance(dashboard, dict):
                    raw_hourly = dashboard.get("hourly_today")
                    if isinstance(raw_hourly, list):
                        hourly = [row for row in raw_hourly if isinstance(row, dict)]
            if not hourly:
                hourly = [
                    {
                        "hour": hour,
                        "requests": 0,
                        "tokens": 0,
                        "cost": 0.0,
                    }
                    for hour in range(24)
                ]
            mix = self._token_mix()
            return {
                "label": "24h",
                "requests": int(self.state.today_requests if self.state else 0),
                "tokens": int(self.state.today_tokens if self.state else 0),
                "input_tokens": mix["input"],
                "cached_input_tokens": mix["cached"],
                "cache_creation_input_tokens": mix["cache_create"],
                "output_tokens": mix["output"],
                "cost": float(self.state.today_account_cost if self.state else 0),
                "series": hourly,
            }
        history = load_usage_history()
        days = history.get("days") if isinstance(history, dict) else {}
        if not isinstance(days, dict):
            days = {}
        series: list[dict[str, Any]] = []
        if range_key == "all":
            parsed_dates = []
            for key, row in days.items():
                if not isinstance(row, dict):
                    continue
                try:
                    parsed_dates.append(datetime.fromisoformat(str(key)).date())
                except ValueError:
                    continue
            if parsed_dates:
                start_date = min(parsed_dates)
                end_date = datetime.now(CN_TZ).date()
                day_count = max(1, (end_date - start_date).days + 1)
                keys = [(start_date + timedelta(days=offset)).isoformat() for offset in range(day_count)]
            else:
                keys = []
        else:
            days_count = 7 if range_key == "7d" else 30
            keys = [date_key(offset) for offset in range(days_count - 1, -1, -1)]
        for key in keys:
            row = days.get(key) if isinstance(days.get(key), dict) else {}
            series.append(
                {
                    "date": key,
                    "requests": int(row.get("requests") or 0),
                    "tokens": int(row.get("tokens") or 0),
                    "input_tokens": int(row.get("input_tokens") or 0),
                    "cached_input_tokens": int(row.get("cached_input_tokens") or 0),
                    "cache_creation_input_tokens": int(row.get("cache_creation_input_tokens") or 0),
                    "output_tokens": int(row.get("output_tokens") or 0),
                    "cost": float(row.get("cost") or 0),
                }
            )
        return {
            "label": range_key,
            "requests": sum(int(item.get("requests") or 0) for item in series),
            "tokens": sum(int(item.get("tokens") or 0) for item in series),
            "input_tokens": sum(int(item.get("input_tokens") or 0) for item in series),
            "cached_input_tokens": sum(int(item.get("cached_input_tokens") or 0) for item in series),
            "cache_creation_input_tokens": sum(int(item.get("cache_creation_input_tokens") or 0) for item in series),
            "output_tokens": sum(int(item.get("output_tokens") or 0) for item in series),
            "cost": sum(float(item.get("cost") or 0) for item in series),
            "series": series,
        }

    def _budget_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for account in list(self.state.top_accounts or []) if self.state else []:
            windows = []
            account_tokens = int(account.get("tokens") or 0)
            account_requests = int(account.get("requests") or 0)
            pressure_active = bool(account.get("active_now") or account.get("is_latest") or account_tokens > 0 or account_requests > 0)
            for key, label in (("window_5h", "5h"), ("window_7d", "7d"), ("window_cycle", "\u5468\u671f")):
                window = account.get(key)
                if not isinstance(window, dict) or not window:
                    continue
                quota_available = bool(window.get("quota_available", window.get("utilization") is not None))
                try:
                    utilization = float(window.get("utilization") or 0)
                except (TypeError, ValueError):
                    utilization = 0.0
                remaining = window.get("remaining_percent")
                if remaining is None and quota_available:
                    remaining = max(0.0, min(100.0, 100.0 - utilization))
                windows.append(
                    {
                        "label": label,
                        "quota_available": quota_available,
                        "quota_stale": bool(window.get("quota_stale")),
                        "utilization": utilization,
                        "remaining": remaining,
                        "resets_at": str(window.get("resets_at") or ""),
                        "tokens": int(window.get("tokens") or 0),
                        "cost": float(window.get("cost") or 0),
                        "pressure_active": pressure_active,
                    }
                )
            if not windows:
                continue
            quota_windows = [item for item in windows if item["quota_available"]]
            min_remaining = min(
                [float(item["remaining"]) for item in quota_windows if item["remaining"] is not None],
                default=999.0,
            )
            rows.append(
                {
                    "name": str(account.get("name") or "-"),
                    "source_badge": str(account.get("source_badge") or ""),
                    "health_badge": str(account.get("health_badge") or ""),
                    "windows": windows,
                    "has_quota": bool(quota_windows),
                    "min_remaining": min_remaining,
                    "pressure_active": pressure_active,
                    "tokens": account_tokens,
                    "requests": account_requests,
                }
            )
        rows.sort(
            key=lambda row: (
                0 if row.get("pressure_active") else 1,
                0 if row["has_quota"] else 1,
                row["min_remaining"],
                row["name"],
            )
        )
        return rows

    def _draw_activity_heatmap(self, col_l: int, col_r: int, y: int, summary: dict[str, Any], series: list[dict[str, Any]]) -> int:
        c = self.canvas
        c.create_text(col_l, y, anchor="nw", text="Activity",
                      font=self._fonts["font_section"], fill=Theme.text_primary)
        chip_x = col_l + self._text_width("Activity", "font_section") + 10
        self._draw_rounded_rect(chip_x, y - 1, chip_x + 78, y + 18, r=6,
                                fill=Theme.ag_surface, outline=Theme.ag_border)
        c.create_text(chip_x + 39, y + 8, anchor="center", text="Contribution",
                      font=self._fonts["font_micro"], fill=Theme.ag_muted)
        badge = f"{summary.get('label', '-') } heatmap"
        bw = self._text_width(badge, "font_micro") + 14
        self._draw_rounded_rect(col_r - bw, y - 1, col_r, y + 18, r=6,
                                fill=Theme.ag_surface, outline=Theme.ag_border)
        c.create_text(col_r - bw // 2, y + 8, anchor="center", text=badge,
                      font=self._fonts["font_micro"], fill=Theme.ag_muted)
        y += 28
        if not series:
            c.create_text(col_l + 4, y, anchor="nw", text="\u6682\u65e0\u8d8b\u52bf\u6570\u636e",
                          font=self._fonts["font_label"], fill=Theme.ag_muted)
            return y + 30

        if self._usage_range == "24h":
            visible = series[-24:]
        elif self._usage_range == "7d":
            visible = series[-7:]
        elif self._usage_range == "30d":
            visible = series[-30:]
        else:
            visible = series
        max_tokens = max([float(item.get("tokens") or 0) for item in visible], default=0.0) or 1.0
        peak = max(visible, key=lambda item: float(item.get("tokens") or 0))
        if self._usage_range == "24h":
            cols = 24
            rows_count = 1
            cell_gap = 3
            cell = max(7, min(13, int((col_r - col_l - cell_gap * (cols - 1)) / cols)))
            for label, col_index in (("00", 0), ("06", 6), ("12", 12), ("18", 18)):
                c.create_text(col_l + col_index * (cell + cell_gap), y, anchor="nw", text=label,
                              font=self._fonts["font_micro"], fill=Theme.ag_muted)
            grid_x = col_l
            grid_y = y + 15
            for index, item in enumerate(visible):
                row = index // cols
                col = index % cols
                tokens = float(item.get("tokens") or 0)
                intensity = min(1.0, tokens / max_tokens) if tokens > 0 else 0.0
                x1 = grid_x + col * (cell + cell_gap)
                y1 = grid_y + row * (cell + cell_gap)
                self._draw_rounded_rect(x1, y1, x1 + cell, y1 + cell, r=3,
                                        fill=self._activity_color(intensity), outline=Theme.ag_border)
                hour = int(item.get("hour") if item.get("hour") is not None else index)
                self._add_tooltip(
                    x1, y1, x1 + cell, y1 + cell,
                    f"{hour:02d}:00-{(hour + 1) % 24:02d}:00\n{compact_number(int(tokens))} token\n{compact_number(item.get('requests', 0))} calls \u00b7 {money(item.get('cost', 0))}",
                )
            legend_y = grid_y + rows_count * (cell + cell_gap) + 8
        elif self._usage_range == "7d":
            cols = 7
            cell_gap = 5
            cell_w = max(28, int((col_r - col_l - cell_gap * (cols - 1)) / cols))
            cell_h = 22
            grid_x = col_l
            grid_y = y + 15
            for index, item in enumerate(visible):
                day_text = str(item.get("date") or "")[-2:] or "-"
                x1 = grid_x + index * (cell_w + cell_gap)
                c.create_text(x1 + cell_w // 2, y, anchor="n", text=day_text,
                              font=self._fonts["font_micro"], fill=Theme.ag_muted)
                tokens = float(item.get("tokens") or 0)
                intensity = min(1.0, tokens / max_tokens) if tokens > 0 else 0.0
                self._draw_rounded_rect(x1, grid_y, x1 + cell_w, grid_y + cell_h, r=5,
                                        fill=self._activity_color(intensity), outline=Theme.ag_border)
                if tokens > 0:
                    shine_w = max(3, int(cell_w * min(1.0, intensity) * 0.18))
                    self._draw_rounded_rect(x1 + 3, grid_y + 3, x1 + 3 + shine_w, grid_y + 6,
                                            r=2, fill=Theme.amber_glow, outline="")
                self._add_tooltip(
                    x1,
                    grid_y,
                    x1 + cell_w,
                    grid_y + cell_h,
                    f"{item.get('date', '-')}\n{compact_number(int(tokens))} token\n{compact_number(item.get('requests', 0))} calls \u00b7 {money(item.get('cost', 0))}",
                )
            legend_y = grid_y + cell_h + 9
        elif self._usage_range == "30d":
            cols = 30
            rows_count = 1
            cell_gap = 3
            cell = max(7, min(12, int((col_r - col_l - cell_gap * (cols - 1)) / cols)))
            grid_w = cols * cell + (cols - 1) * cell_gap
            grid_x = col_l + max(0, (col_r - col_l - grid_w) // 2)
            grid_y = y + 8
            padded = visible[-30:]
            while len(padded) < 30:
                padded.insert(0, {"date": "-", "requests": 0, "tokens": 0, "cost": 0.0})
            for index, item in enumerate(padded):
                row = 0
                col = index
                tokens = float(item.get("tokens") or 0)
                intensity = min(1.0, tokens / max_tokens) if tokens > 0 else 0.0
                x1 = grid_x + col * (cell + cell_gap)
                y1 = grid_y + row * (cell + cell_gap)
                self._draw_rounded_rect(x1, y1, x1 + cell, y1 + cell, r=4,
                                        fill=self._activity_color(intensity), outline=Theme.ag_border)
                self._add_tooltip(
                    x1,
                    y1,
                    x1 + cell,
                    y1 + cell,
                    f"{item.get('date', '-')}\n{compact_number(int(tokens))} token\n{compact_number(item.get('requests', 0))} calls \u00b7 {money(item.get('cost', 0))}",
                )
            axis_y = grid_y + cell + 3
            c.create_text(grid_x, axis_y, anchor="nw", text="30d ago",
                          font=self._fonts["font_micro"], fill=Theme.ag_muted)
            c.create_text(grid_x + grid_w, axis_y, anchor="ne", text="today",
                          font=self._fonts["font_micro"], fill=Theme.ag_muted)
            legend_y = axis_y + 14
        else:
            label_w = 24
            cell_gap = 4
            rows_count = 7
            dates = []
            for item in visible:
                try:
                    dates.append(datetime.fromisoformat(str(item.get("date") or "")).date())
                except ValueError:
                    dates.append(None)
            first_date = next((value for value in dates if value is not None), None)
            last_date = next((value for value in reversed(dates) if value is not None), None)
            if first_date and last_date:
                grid_start = first_date - timedelta(days=first_date.weekday())
                grid_end = last_date + timedelta(days=6 - last_date.weekday())
            else:
                grid_start = datetime.now(CN_TZ).date()
                grid_end = grid_start
            span_days = (grid_end - grid_start).days
            cols = max(1, math.ceil((span_days + 1) / 7))
            min_cell = 5 if self._usage_range == "all" else 9
            max_cell = 10 if self._usage_range == "all" else 14
            cell = max(min_cell, min(max_cell, int((col_r - col_l - label_w - cell_gap * max(0, cols - 1)) / max(1, cols))))
            grid_x = col_l + label_w
            month_seen: set[tuple[int, int]] = set()
            data_by_date = {
                item_date: item
                for item, item_date in zip(visible, dates)
                if item_date is not None
            }
            for offset in range(span_days + 1):
                item_date = grid_start + timedelta(days=offset)
                col = offset // 7
                month_key = (item_date.year, item_date.month)
                if month_key in month_seen:
                    continue
                month_seen.add(month_key)
                c.create_text(grid_x + col * (cell + cell_gap), y, anchor="nw",
                              text=item_date.strftime("%b"), font=self._fonts["font_micro"], fill=Theme.ag_muted)
            grid_y = y + 15
            for label, row in (("Mon", 0), ("Wed", 2), ("Fri", 4)):
                c.create_text(col_l, grid_y + row * (cell + cell_gap), anchor="nw",
                              text=label, font=self._fonts["font_micro"], fill=Theme.ag_muted)
            for offset in range(span_days + 1):
                item_date = grid_start + timedelta(days=offset)
                item = data_by_date.get(item_date, {"date": item_date.isoformat(), "requests": 0, "tokens": 0, "cost": 0.0})
                col = offset // rows_count
                row = item_date.weekday()
                tokens = float(item.get("tokens") or 0)
                intensity = min(1.0, tokens / max_tokens) if tokens > 0 else 0.0
                x1 = grid_x + col * (cell + cell_gap)
                y1 = grid_y + row * (cell + cell_gap)
                self._draw_rounded_rect(x1, y1, x1 + cell, y1 + cell, r=3,
                                        fill=self._activity_color(intensity), outline=Theme.ag_border)
                self._add_tooltip(
                    x1, y1, x1 + cell, y1 + cell,
                    f"{item.get('date', '-')}\n{compact_number(int(tokens))} token\n{compact_number(item.get('requests', 0))} calls \u00b7 {money(item.get('cost', 0))}",
                )
            legend_y = grid_y + rows_count * (cell + cell_gap) + 8

        c.create_text(col_l, legend_y + 1, anchor="nw", text="Less",
                      font=self._fonts["font_micro"], fill=Theme.ag_muted)
        legend_x = col_l + 30
        for idx, color in enumerate(["#2D2D32", "#3D4C6B", "#5276B7", "#6E9DF8", "#3975E5"]):
            self._draw_rounded_rect(legend_x + idx * 14, legend_y, legend_x + idx * 14 + 10, legend_y + 10,
                                    r=2, fill=color, outline=Theme.ag_border)
        c.create_text(legend_x + 74, legend_y + 1, anchor="nw", text="More",
                      font=self._fonts["font_micro"], fill=Theme.ag_muted)
        peak_tokens = int(float(peak.get("tokens") or 0))
        peak_label = f"{int(peak.get('hour')):02d}:00" if self._usage_range == "24h" and peak.get("hour") is not None else str(peak.get("date") or "-")
        c.create_text(col_r, legend_y + 1, anchor="ne",
                      text=f"Peak: {peak_label} ({compact_number(peak_tokens)})",
                      font=self._fonts["font_micro"], fill=Theme.ag_muted)
        return legend_y + 23

    def _draw_token_budget_page(self, col_l: int, col_r: int, y: int, H: int) -> None:
        c = self.canvas
        rows = self._budget_rows()
        quota_rows = [row for row in rows if row["has_quota"]]
        stale_count = sum(
            1
            for row in rows
            for window in row["windows"]
            if window.get("quota_stale")
        )
        low_count = sum(1 for row in quota_rows if float(row.get("min_remaining") or 999) <= 20)
        effective_windows = [
            dict(window, account=row.get("name"))
            for row in rows
            for window in row["windows"]
            if window.get("quota_available") and window.get("pressure_active") and not window.get("quota_stale")
        ]
        inactive_low_count = sum(
            1
            for row in quota_rows
            if not row.get("pressure_active") and float(row.get("min_remaining") or 999) <= 20
        )
        pressure_window: dict[str, Any] | None = None
        for window in effective_windows:
            if pressure_window is None or float(window.get("utilization") or 0) > float(pressure_window.get("utilization") or 0):
                pressure_window = window
        worst_used = float(pressure_window.get("utilization") or 0) if pressure_window else 0.0
        try:
            worst_remaining = float(pressure_window.get("remaining")) if pressure_window else None
        except (TypeError, ValueError):
            worst_remaining = None
        pressure_label = str(pressure_window.get("label") or "") if pressure_window else ""
        donut_color = Theme.ag_crit if worst_used >= 80 else (Theme.ag_warn if worst_used >= 50 else Theme.ag_success)

        self._draw_rounded_rect(col_l, y, col_r, y + 92, r=8, fill=Theme.ag_surface, outline=Theme.ag_border)
        self._draw_donut(col_l + 10, y + 14, 64, worst_used, donut_color, f"{worst_used:.0f}%")
        c.create_text(col_l + 88, y + 14, anchor="nw", text="\u989d\u5ea6\u538b\u529b",
                      font=self._fonts["font_label_bold"], fill=Theme.text_primary)
        remaining_label = f"\u6700\u4f4e\u5269\u4f59 {worst_remaining:.0f}%" if worst_remaining is not None else "\u6682\u65e0\u5269\u4f59\u6570\u636e"
        detail = f"{remaining_label}  \u00b7  {pressure_label or '-'}  \u00b7  {len(effective_windows)} \u4e2a\u6d3b\u8dc3\u7a97\u53e3"
        c.create_text(col_l + 88, y + 36, anchor="nw", text=detail,
                      font=self._fonts["font_label"], fill=Theme.text_secondary)
        warning = "\u6d3b\u8dc3\u8d26\u53f7\u53ef\u80fd\u5373\u5c06\u9650\u989d" if pressure_window and worst_used >= 80 else "\u6d3b\u8dc3\u989d\u5ea6\u72b6\u6001\u6b63\u5e38"
        if not effective_windows:
            warning = "\u6682\u65e0\u6d3b\u8dc3\u989d\u5ea6\u538b\u529b"
        if stale_count:
            warning = f"{stale_count} \u4e2a\u7a97\u53e3\u5f85\u5237\u65b0"
        c.create_text(col_l + 88, y + 57, anchor="nw", text=warning,
                      font=self._fonts["font_tiny"], fill=Theme.ag_warn if stale_count or worst_used >= 80 else Theme.ag_success)
        x = col_l + 88
        y_chip = y + 70
        low_text = f"\u4f4e\u4f59\u989d {low_count}" if worst_remaining is None else f"\u6700\u4f4e {worst_remaining:.0f}%"
        x += self._draw_ag_chip(x, y_chip, low_text, Theme.ag_crit if worst_used >= 80 else Theme.ag_success) + 5
        x += self._draw_ag_chip(x, y_chip, f"\u975e\u6d3b\u8dc3\u4f4e\u989d {inactive_low_count}", Theme.ag_muted) + 5
        self._draw_ag_chip(x, y_chip, f"\u5f85\u5237\u65b0 {stale_count}", Theme.ag_warn)
        y += 106

        cats: list[dict[str, Any]] = []
        for key, label in (("5h", "5h"), ("7d", "7d"), ("cycle", "\u5468\u671f")):
            all_windows = [
                window
                for row in rows
                for window in row["windows"]
                if window.get("label") == label
            ]
            quota_windows = [window for window in all_windows if window.get("quota_available")]
            pressure_windows = [
                window
                for window in quota_windows
                if window.get("pressure_active") and not window.get("quota_stale")
            ]
            avg_used = (
                sum(float(window.get("utilization") or 0) for window in pressure_windows) / len(pressure_windows)
                if pressure_windows
                else 0.0
            )
            cats.append(
                {
                    "name": label,
                    "count": len(all_windows),
                    "quota_count": len(quota_windows),
                    "active_count": len(pressure_windows),
                    "tokens": sum(int(window.get("tokens") or 0) for window in all_windows),
                    "cost": sum(float(window.get("cost") or 0) for window in all_windows),
                    "used": avg_used,
                    "stale": sum(1 for window in all_windows if window.get("quota_stale")),
                }
            )
        cats.sort(key=lambda item: item["used"], reverse=True)
        y = self._draw_ag_section(col_l, col_r, y, "Category Breakdown", "\u771f\u5b9e\u989d\u5ea6")
        for cat in cats:
            color = Theme.ag_crit if cat["used"] >= 80 else (Theme.ag_warn if cat["used"] >= 50 else Theme.ag_success)
            self._draw_rounded_rect(col_l, y, col_r, y + 38, r=6, fill=Theme.ag_surface, outline=Theme.ag_border)
            c.create_text(col_l + 10, y + 10, anchor="nw", text=str(cat["name"]),
                          font=self._fonts["font_label_bold"], fill=Theme.text_primary)
            c.create_text(col_l + 52, y + 10, anchor="nw", text=f"{compact_number(cat['tokens'])} tok",
                          font=self._fonts["font_micro"], fill=Theme.text_secondary)
            if cat["active_count"] <= 0 and cat["quota_count"] > 0:
                c.create_text(col_l + 52, y + 23, anchor="nw", text="\u6682\u65e0\u6d3b\u8dc3\u538b\u529b",
                              font=self._fonts["font_micro"], fill=Theme.ag_muted)
            c.create_text(col_r - 62, y + 10, anchor="ne", text=money(cat["cost"]),
                          font=self._fonts["font_micro"], fill=Theme.ag_muted)
            pct_text = f"{cat['used']:.0f}%"
            self._draw_rounded_rect(col_r - 54, y + 8, col_r - 10, y + 27, r=6,
                                    fill=Theme.ag_bg, outline=color)
            c.create_text(col_r - 32, y + 17, anchor="center", text=pct_text,
                          font=self._fonts["font_micro"], fill=color)
            y += 44

        y = self._draw_ag_section(col_l, col_r, y + 4, "\u989d\u5ea6\u7a97\u53e3", "\u5269\u4f59\u4ece\u4f4e\u5230\u9ad8")

        list_top = y
        list_bottom = H - 38
        row_h = 84
        max_scroll = max(0, len(rows) * row_h - max(1, list_bottom - list_top))
        self._scroll_limits["budget"] = max_scroll
        self._scroll_offsets["budget"] = max(0, min(self._scroll_offsets.get("budget", 0), max_scroll))
        offset = self._scroll_offsets.get("budget", 0)
        if not rows:
            c.create_text(col_l + 8, y, anchor="nw", text="\u6682\u65e0\u989d\u5ea6\u7a97\u53e3\u6570\u636e",
                          font=self._fonts["font_label"], fill=Theme.text_muted)
            return
        for index, row in enumerate(rows):
            row_y = list_top + index * row_h - offset
            if row_y < list_top or row_y > list_bottom:
                continue
            name = self._truncate(ranking_account_display_name(row["name"]), "font_label_bold", col_r - col_l - 88)
            self._draw_rounded_rect(col_l, row_y, col_r, row_y + row_h - 8, r=10,
                                    fill=Theme.ag_surface, outline=Theme.ag_border)
            c.create_text(col_l + 10, row_y + 8, anchor="nw", text=name,
                          font=self._fonts["font_label_bold"], fill=Theme.text_primary)
            badge = "\u672c\u5730" if row["source_badge"] == "LOCAL" else ("SUB2" if row["source_badge"] == "SUB" else row["source_badge"])
            if badge:
                badge_w = self._text_width(badge, "font_micro") + 14
                self._draw_rounded_rect(col_r - badge_w - 10, row_y + 7, col_r - 10, row_y + 25, r=6,
                                        fill=Theme.ag_bg, outline=Theme.ag_border)
                c.create_text(col_r - badge_w - 3, row_y + 10, anchor="nw", text=badge,
                              font=self._fonts["font_micro"], fill=Theme.ag_muted)
            for win_index, window in enumerate(row["windows"][:3]):
                x1 = col_l + 10 + win_index * ((col_r - col_l - 28) // 3)
                x2 = col_l + 10 + (win_index + 1) * ((col_r - col_l - 28) // 3) - 5
                wy = row_y + 35
                label = str(window["label"])
                if window["quota_available"]:
                    utilization = float(window.get("utilization") or 0)
                    color = self._ag_quota_color(utilization)
                    remaining = window.get("remaining")
                    try:
                        detail = f"\u5269\u4f59 {float(remaining):.0f}%"
                    except (TypeError, ValueError):
                        detail = "\u5269\u4f59 --"
                    reset = quota_reset_text(window.get("resets_at")) or "\u91cd\u7f6e -"
                    if window.get("quota_stale"):
                        detail = "\u5f85\u5237\u65b0"
                        color = Theme.ag_warn
                else:
                    utilization = 0.0
                    color = Theme.ag_muted
                    detail = "\u672a\u914d\u7f6e"
                    reset = "\u65e0\u989d\u5ea6"
                c.create_text(x1, wy, anchor="nw", text=label,
                              font=self._fonts["font_micro"], fill=Theme.ag_muted)
                c.create_text(x1 + 24, wy, anchor="nw", text=self._truncate(detail, "font_micro", max(30, x2 - x1 - 24)),
                              font=self._fonts["font_micro"], fill=color)
                bar_y = wy + 20
                self._draw_rounded_rect(x1, bar_y, x2, bar_y + 5, r=2, fill=Theme.ag_bg, outline="")
                if window["quota_available"]:
                    fill_w = int((x2 - x1) * max(0.02, min(1.0, utilization / 100.0)))
                    self._draw_rounded_rect(x1, bar_y, x1 + fill_w, bar_y + 5, r=2, fill=color, outline="")
                c.create_text(x1, bar_y + 9, anchor="nw", text=self._truncate(reset, "font_micro", max(40, x2 - x1)),
                              font=self._fonts["font_micro"], fill=Theme.ag_muted)

    def _draw_usage_stats_page(self, col_l: int, col_r: int, y: int, H: int) -> None:
        c = self.canvas
        summary = self._usage_range_summary(self._usage_range)
        c.create_text(col_l, y, anchor="nw", text="Usage Dashboard",
                      font=self._fonts["font_section"], fill=Theme.text_primary)
        range_buttons = [("24h", "24h"), ("7d", "7d"), ("30d", "30d"), ("All", "all")]
        btn_w = 39
        gap = 4
        x = col_r - (btn_w * len(range_buttons) + gap * (len(range_buttons) - 1))
        for label, value in range_buttons:
            name = f"usage_range_{value}"
            selected = self._usage_range == value
            self._btn_rects[name] = (x - 3, y - 4, x + btn_w + 3, y + 24)
            fill = Theme.bg_hover if selected else Theme.ag_surface
            outline = Theme.bg_hover if selected else Theme.ag_border
            text_color = Theme.text_primary if selected else Theme.ag_muted
            self._draw_rounded_rect(x, y - 1, x + btn_w, y + 21, r=6, fill=fill, outline=outline)
            c.create_text(x + btn_w // 2, y + 10, anchor="center", text=label,
                          font=self._fonts["font_micro"], fill=text_color)
            x += btn_w + gap
        y += 28

        hero_h = 78
        self._draw_rounded_rect(col_l, y, col_r, y + hero_h, r=8, fill=Theme.ag_surface, outline=Theme.ag_border)
        c.create_text(col_l + 12, y + 10, anchor="nw", text="Total Tokens",
                      font=self._fonts["font_micro"], fill=Theme.ag_muted)
        c.create_text(col_l + 12, y + 29, anchor="nw", text=compact_number(summary["tokens"]),
                      font=self._fonts["font_value"], fill=Theme.ag_accent)
        c.create_text(col_l + 12, y + 56, anchor="nw", text=f"{compact_number(summary['requests'])} calls",
                      font=self._fonts["font_micro"], fill=Theme.text_secondary)
        mid = col_l + (col_r - col_l) // 2
        c.create_line(mid, y + 12, mid, y + hero_h - 12, fill=Theme.ag_divider, width=1)
        c.create_text(mid + 14, y + 10, anchor="nw", text="Estimated Cost",
                      font=self._fonts["font_micro"], fill=Theme.ag_muted)
        c.create_text(mid + 14, y + 29, anchor="nw", text=money(summary["cost"]),
                      font=self._fonts["font_value"], fill=Theme.ag_success)
        c.create_text(mid + 14, y + 56, anchor="nw", text=f"{summary['label']} window",
                      font=self._fonts["font_micro"], fill=Theme.text_secondary)
        y += hero_h + 12

        mix = self._summary_token_mix(summary)
        token_items = [
            ("Input", mix["input"], Theme.ag_input),
            ("Cache Read", mix["cached"], Theme.ag_cache),
            ("Cache Write", mix["cache_create"], Theme.ag_reason),
            ("Output", mix["output"], Theme.ag_output),
        ]
        cache_base = mix["input"] + mix["cached"] + mix["cache_create"]
        cache_hit_text = f"{mix['cached'] * 100 / cache_base:.1f}%" if cache_base > 0 else "-"
        chip_items = [
            ("Input", compact_number(mix["input"]), Theme.ag_input),
            ("Cache Read", compact_number(mix["cached"]), Theme.ag_cache),
            ("Cache Hit", cache_hit_text, Theme.ag_success),
            ("Cache Write", compact_number(mix["cache_create"]), Theme.ag_reason),
            ("Output", compact_number(mix["output"]), Theme.ag_output),
        ]
        if mix.get("unknown", 0) > 0:
            token_items.append(("Untracked", mix["unknown"], Theme.ag_muted))
            chip_items.append(("Untracked", compact_number(mix["unknown"]), Theme.ag_muted))
        mix_total = sum(value for _label, value, _color in token_items)
        chip_badge = f"{summary['label']} mix"
        if mix.get("unknown", 0) > 0:
            chip_badge = f"{summary['label']} partial"
        y = self._draw_ag_section(col_l, col_r, y, "Token Chips", chip_badge)
        chip_w = (col_r - col_l - 8) // 2
        for index, (label, value_text, color) in enumerate(chip_items):
            cx = col_l + (index % 2) * (chip_w + 8)
            cy = y + (index // 2) * 34
            self._draw_rounded_rect(cx, cy, cx + chip_w, cy + 27, r=6, fill=Theme.ag_surface, outline=Theme.ag_border)
            c.create_oval(cx + 8, cy + 10, cx + 15, cy + 17, fill=color, outline="")
            c.create_text(cx + 22, cy + 6, anchor="nw", text=label,
                          font=self._fonts["font_micro"], fill=Theme.text_secondary)
            c.create_text(cx + chip_w - 8, cy + 6, anchor="ne", text=value_text,
                          font=self._fonts["font_micro"], fill=Theme.text_primary)
        y += max(72, math.ceil(len(chip_items) / 2) * 34 + 4)
        bar_x = col_l
        bar_w = col_r - col_l
        self._draw_rounded_rect(bar_x, y, bar_x + bar_w, y + 8, r=3, fill=Theme.ag_bg, outline="")
        cursor = bar_x
        for _label, value, color in token_items:
            if mix_total <= 0 or value <= 0:
                continue
            seg_w = int(bar_w * value / mix_total)
            c.create_rectangle(cursor, y, min(bar_x + bar_w, cursor + seg_w), y + 8, fill=color, outline="")
            cursor += seg_w
        y += 22

        series = summary.get("series") if isinstance(summary, dict) else []
        series = [item for item in series if isinstance(item, dict)]
        y = self._draw_activity_heatmap(col_l, col_r, y, summary, series)
        if False and series:
            if self._usage_range == "24h":
                visible = series[-24:]
            else:
                visible = series[-30:] if self._usage_range == "30d" else series[-7:]
            max_tokens = max([float(item.get("tokens") or 0) for item in visible], default=0.0) or 1.0
            cols = 12 if self._usage_range == "24h" else (15 if self._usage_range == "30d" else 7)
            cell_gap = 4
            cell = max(8, min(18, int((col_r - col_l - cell_gap * (cols - 1)) / cols)))
            for index, item in enumerate(visible):
                row = index // cols
                col = index % cols
                tokens = float(item.get("tokens") or 0)
                intensity = min(1.0, tokens / max_tokens) if tokens > 0 else 0.0
                color = Theme.ag_bg
                if intensity > 0.66:
                    color = Theme.ag_accent
                elif intensity > 0.33:
                    color = Theme.ag_bar
                elif intensity > 0:
                    color = Theme.amber_glow
                x1 = col_l + col * (cell + cell_gap)
                y1 = y + row * (cell + cell_gap)
                self._draw_rounded_rect(x1, y1, x1 + cell, y1 + cell, r=3, fill=color, outline=Theme.ag_border)
                if self._usage_range == "24h":
                    hour = int(item.get("hour") if item.get("hour") is not None else index)
                    next_hour = (hour + 1) % 24
                    tip_title = f"{hour:02d}:00-{next_hour:02d}:00"
                else:
                    tip_title = str(item.get("date") or "-")
                self._add_tooltip(
                    x1,
                    y1,
                    x1 + cell,
                    y1 + cell,
                    f"{tip_title}\n{compact_number(int(tokens))} token\n{compact_number(item.get('requests', 0))} calls · {money(item.get('cost', 0))}",
                )
            heatmap_rows = max(1, math.ceil(len(visible) / cols))
            if self._usage_range == "24h":
                label_y = y + heatmap_rows * (cell + cell_gap) + 1
                for label, col_index in (("00", 0), ("06", 3), ("12", 6), ("18", 9)):
                    lx = col_l + col_index * (cell + cell_gap)
                    c.create_text(lx, label_y, anchor="nw", text=label,
                                  font=self._fonts["font_micro"], fill=Theme.ag_muted)
                y += 12
            y += heatmap_rows * (cell + cell_gap) + 10
        elif False:
            c.create_text(col_l + 4, y, anchor="nw", text="\u6682\u65e0\u8d8b\u52bf\u6570\u636e",
                          font=self._fonts["font_label"], fill=Theme.ag_muted)
            y += 30

        models = self._top_models()
        y = self._draw_ag_section(col_l, col_r, y, "Top Models", f"{len(models)} models")
        if not models:
            c.create_text(col_l + 4, y, anchor="nw", text="\u6682\u65e0\u6a21\u578b\u7edf\u8ba1",
                          font=self._fonts["font_label"], fill=Theme.ag_muted)
            y += 28
        else:
            max_model_tokens = max(tokens for _model, tokens in models) or 1
            for model, tokens in models[:5]:
                self._draw_rounded_rect(col_l, y, col_r, y + 29, r=6, fill=Theme.ag_surface, outline=Theme.ag_border)
                c.create_text(col_l + 9, y + 7, anchor="nw",
                              text=self._truncate(model, "font_micro", max(90, col_r - col_l - 124)),
                              font=self._fonts["font_micro"], fill=Theme.text_primary)
                pct = int(tokens * 100 / max_model_tokens)
                c.create_text(col_r - 9, y + 7, anchor="ne", text=f"{compact_number(tokens)} tok",
                              font=self._fonts["font_micro"], fill=Theme.text_secondary)
                self._draw_rounded_rect(col_l + 9, y + 22, col_r - 9, y + 25, r=1, fill=Theme.ag_bg, outline="")
                fill_w = int((col_r - col_l - 18) * pct / 100)
                self._draw_rounded_rect(col_l + 9, y + 22, col_l + 9 + fill_w, y + 25, r=1,
                                        fill=Theme.ag_accent, outline="")
                y += 34

        providers = sorted(
            self._client_providers(),
            key=lambda row: (-float(row.get("cost") or 0), -int(row.get("tokens") or 0), str(row.get("name") or "")),
        )
        y = self._draw_ag_section(col_l, col_r, y, "Provider Cost Breakdown", f"{len(providers)} providers")
        list_top = y
        list_bottom = H - 38
        row_h = 48
        max_scroll = max(0, len(providers) * row_h - max(1, list_bottom - list_top))
        self._scroll_limits["stats"] = max_scroll
        self._scroll_offsets["stats"] = max(0, min(self._scroll_offsets.get("stats", 0), max_scroll))
        offset = self._scroll_offsets.get("stats", 0)
        if not providers:
            c.create_text(col_l + 8, y, anchor="nw", text="\u6682\u65e0 provider \u6570\u636e",
                          font=self._fonts["font_label"], fill=Theme.ag_muted)
            return
        max_provider_cost = max([float(row.get("cost") or 0) for row in providers], default=0.0) or 1.0
        for index, provider in enumerate(providers):
            row_y = list_top + index * row_h - offset
            if row_y < list_top or row_y > list_bottom:
                continue
            self._draw_rounded_rect(col_l, row_y, col_r, row_y + row_h - 7, r=6,
                                    fill=Theme.ag_surface, outline=Theme.ag_border)
            name = self._truncate(local_provider_display_name(str(provider.get("name") or "-")),
                                  "font_label", max(90, col_r - col_l - 132))
            tokens = compact_number(provider.get("tokens", 0))
            cost_value = float(provider.get("cost") or 0)
            requests_count = compact_number(provider.get("requests", 0))
            c.create_text(col_l + 9, row_y + 6, anchor="nw", text=name,
                          font=self._fonts["font_label"], fill=Theme.text_primary)
            c.create_text(col_r - 9, row_y + 6, anchor="ne", text=money(cost_value),
                          font=self._fonts["font_label_bold"], fill=Theme.ag_success)
            c.create_text(col_l + 9, row_y + 24, anchor="nw", text=f"{tokens} tok  \u00b7  {requests_count} calls",
                          font=self._fonts["font_micro"], fill=Theme.ag_muted)
            bar_w = int((col_r - col_l - 18) * min(1.0, cost_value / max_provider_cost))
            if bar_w > 0:
                c.create_rectangle(col_l + 9, row_y + 38, col_l + 9 + bar_w, row_y + 40,
                                   fill=Theme.ag_bar, outline="")

    def _draw(self) -> None:
        if self.closed:
            return
        c = self.canvas
        c.delete("all")
        self._tooltip_rects = []
        W, H = self.WIDTH, self.HEIGHT
        actual_w = self.root.winfo_width()
        actual_h = self.root.winfo_height()
        if actual_w > 50 and actual_h > 50 and (actual_w != W or actual_h != H):
            self._apply_window_size(W, H)
        PAD = 14
        COL_L = PAD
        COL_R = W - PAD

        # ── outer card background ──
        self._draw_rounded_rect(4, 7, W - 2, H - 2, r=18, fill=Theme.shadow, outline="")
        self._draw_rounded_rect(0, 0, W, H - 5, r=18, fill=Theme.bg_card, outline=Theme.border, width=1)

        # ── subtle top accent lines ──
        c.create_line(20, 2, W // 2 - 8, 2, fill=Theme.amber, width=2)
        c.create_line(W // 2 + 8, 2, W - 20, 2, fill=Theme.cyan_dim, width=2)

        # ════════════════════════════════════════════════════════
        #  HEADER  (row y=10..48)
        # ════════════════════════════════════════════════════════
        y = 16
        title_text = "Token Pulse"
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
                      font=self._fonts["font_micro"], fill=Theme.amber)

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
        y += 10
        y = self._draw_main_tabs(COL_L, COL_R, y)
        if self._main_tab == "budget":
            self._draw_token_budget_page(COL_L, COL_R, y, H)
            self._draw_footer(W, H)
            self._draw_tooltip(W, H)
            return
        if self._main_tab == "stats":
            self._draw_usage_stats_page(COL_L, COL_R, y, H)
            self._draw_footer(W, H)
            self._draw_tooltip(W, H)
            return

        # ════════════════════════════════════════════════════════
        #  CURRENT CHANNEL HERO
        # ════════════════════════════════════════════════════════
        y += 12
        self._draw_rounded_rect(COL_L, y, COL_R, y + 72, r=13, fill=Theme.bg_section, outline=Theme.border)
        all_active_accounts = list(self.state.active_accounts or []) if self.state else []
        accounts = all_active_accounts[:5]
        latest_name = self.state.latest_account_name if self.state else ""
        if accounts:
            hero_name = accounts[0].get("name", latest_name or "-")
            total_current = sum(int(account.get("current") or 0) for account in all_active_accounts)
            hero_sub = f"{len(all_active_accounts)} \u4e2a\u8d26\u53f7\u6d3b\u8dc3 / \u603b\u5e76\u53d1 {total_current}"
            hero_color = Theme.accent_green
        else:
            status, _model, ago, color = self._latest_status()
            hero_name = latest_name or ("\u6b63\u5728\u8bfb\u53d6\u6570\u636e" if self._loading or not self.state else "\u6682\u65e0\u6570\u636e")
            hero_sub = f"\u6700\u8fd1 {status} / {ago}" if status != "-" else ("\u521d\u59cb\u5316\u4e2d" if self._loading or not self.state else "\u6682\u65e0\u6d3b\u8dc3\u8bf7\u6c42")
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
            c.create_text(COL_L + 8, y, anchor="nw", text=("\u6b63\u5728\u8bfb\u53d6\u8d26\u53f7\u72b6\u6001" if self._loading or not self.state else "\u6682\u65e0\u6d3b\u8dc3"),
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
            c.create_text(COL_L + 8, y, anchor="nw", text=("\u6b63\u5728\u8bfb\u53d6\u6700\u8fd1\u8bf7\u6c42" if self._loading or not self.state else "\u6682\u65e0\u8bf7\u6c42\u8bb0\u5f55"),
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
        self._draw_rounded_rect(COL_L, y - 5, COL_R, y + 43, r=8,
                                fill=Theme.ag_surface, outline=Theme.ag_border)
        for i, (lbl, val, color) in enumerate(stats):
            cx = COL_L + col_w * i + col_w // 2
            if i:
                c.create_line(COL_L + col_w * i, y + 2, COL_L + col_w * i, y + 36,
                              fill=Theme.ag_divider, width=1)
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
            ("\u4eca\u65e5", f"{compact_number(history.get('today_tokens', 0))} tok", Theme.ag_accent),
            ("\u6628\u65e5", f"{compact_number(history.get('yesterday_tokens', 0))} tok", Theme.ag_success),
            ("\u65e5\u5747", f"{compact_number(float(history.get('seven_day_tokens') or 0) / 7)} tok", Theme.amber_glow),
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
                intensity = min(1.0, cost / max_cost) if cost > 0 else 0.0
                x1 = COL_L + index * (bar_w + gap)
                x2 = min(COL_R, x1 + bar_w)
                fill_h = max(2, int(bar_h * min(1.0, cost / max_cost))) if cost > 0 else 2
                self._draw_rounded_rect(x1, bar_y, x2, bar_y + bar_h, r=3, fill=Theme.ag_bg, outline="")
                color = self._trend_token_color(intensity, index == 6)
                self._draw_rounded_rect(x1, bar_y + bar_h - fill_h, x2, bar_y + bar_h, r=3, fill=color, outline="")
                self._add_tooltip(
                    x1,
                    bar_y,
                    x2,
                    bar_y + bar_h,
                    f"{item.get('date', '-')}\n{compact_number(int(cost))} token\n{compact_number(item.get('requests', 0))} calls · {money(item.get('cost', 0))}",
                )
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
            fill = Theme.bg_hover if selected else (Theme.ag_surface_hover if hovered else Theme.ag_surface)
            outline = Theme.bg_hover if selected else Theme.ag_border
            text_color = Theme.text_primary if selected else (Theme.text_primary if hovered else Theme.ag_muted)
            self._draw_rounded_rect(x1, y - 1, x2, y - 1 + tab_h, r=6, fill=fill, outline=outline, width=1)
            c.create_text((x1 + x2) // 2, y + 9, anchor="center", text=label,
                          font=self._fonts["font_micro"], fill=text_color)
        y += 27

        if not top:
            if self._loading or not self.state:
                empty_text = "\u6b63\u5728\u8bfb\u53d6\u7528\u91cf\u6570\u636e"
            else:
                empty_text = "\u8be5\u65f6\u95f4\u8303\u56f4\u6682\u65e0\u8d26\u53f7\u8bb0\u5f55" if range_key else "\u6682\u65e0\u7528\u91cf"
            c.create_text(COL_L + 8, y, anchor="nw", text=empty_text,
                          font=self._fonts["font_label"], fill=Theme.text_muted)
        window_mode = bool(range_key)
        row_h = 64 if window_mode else 39
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

            self._draw_rounded_rect(COL_L, y - 3, COL_R, y + row_h - 5, r=6,
                                    fill=Theme.ag_surface, outline=Theme.ag_border)
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

        self._draw_footer(W, H)
        self._draw_tooltip(W, H)

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
            if name.startswith("main_") and x1 - 6 <= x <= x2 + 6 and y1 - 8 <= y <= y2 + 8:
                return name
            if x1 <= x <= x2 and y1 <= y <= y2:
                return name
        return None

    def _hit_resize_handle(self, x: int, y: int) -> bool:
        return x >= self.WIDTH - 24 and y >= self.HEIGHT - 24

    def _on_press(self, event: tk.Event) -> None:
        btn = self._hit_button(event.x, event.y)
        if btn == "btn_close":
            self._resizing = False
            self.close_app()
            return
        if btn == "btn_pin":
            self._resizing = False
            self._pinned = not self._pinned
            self.root.attributes("-topmost", self._pinned)
            self._draw()
            return
        if btn == "btn_refresh":
            self._resizing = False
            self.client.clear_client_usage_cache()
            self.refresh_async()
            return
        if btn in {"main_accounts", "main_budget", "main_stats"}:
            self._resizing = False
            self._main_tab = {
                "main_accounts": "accounts",
                "main_budget": "budget",
                "main_stats": "stats",
            }[btn]
            self._scroll_offsets[self._main_tab] = 0
            self._draw()
            return
        if 56 <= event.y <= 96 and 14 <= event.x <= self.WIDTH - 14:
            self._resizing = False
            tab_width = max(1, (self.WIDTH - 28) / 3)
            tab_index = int(max(0, min(2, (event.x - 14) // tab_width)))
            tab_value = ("accounts", "budget", "stats")[tab_index]
            if self._main_tab != tab_value:
                self._main_tab = tab_value
                self._scroll_offsets[tab_value] = 0
                self._draw()
            return
        if btn in {"usage_range_24h", "usage_range_7d", "usage_range_30d", "usage_range_all"}:
            self._resizing = False
            self._usage_range = btn.replace("usage_range_", "")
            self._scroll_offsets["stats"] = 0
            self._draw()
            return
        if btn in {"rank_today", "rank_5h", "rank_7d", "rank_cycle"}:
            self._resizing = False
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

    def _on_release(self, _event: tk.Event) -> None:
        self._resizing = False

    def _on_drag(self, event: tk.Event) -> None:
        if self._resizing:
            new_w = max(self.MIN_WIDTH, self._resize_data["w"] + event.x_root - self._resize_data["x"])
            new_h = max(self.MIN_HEIGHT, self._resize_data["h"] + event.y_root - self._resize_data["y"])
            self._apply_window_size(int(new_w), int(new_h))
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
        tooltip = self._hit_tooltip(event.x, event.y)
        tooltip_pos = (int(event.x), int(event.y))
        if btn != self._hover_btn or tooltip != self._tooltip_text or (tooltip and tooltip_pos != self._tooltip_pos):
            self._hover_btn = btn
            self._tooltip_text = tooltip
            self._tooltip_pos = tooltip_pos
            self._draw()

    def _on_leave(self, _event: tk.Event) -> None:
        self.canvas.configure(cursor="")
        if self._hover_btn is not None or self._tooltip_text:
            self._hover_btn = None
            self._tooltip_text = ""
            self._draw()

    def _on_configure(self, event: tk.Event) -> None:
        if self._ignore_configure:
            return
        width = int(getattr(event, "width", self.WIDTH) or self.WIDTH)
        height = int(getattr(event, "height", self.HEIGHT) or self.HEIGHT)
        if width <= 50 or height <= 50:
            return
        if self._resizing:
            self.WIDTH = max(self.MIN_WIDTH, width)
            self.HEIGHT = max(self.MIN_HEIGHT, height)
            return
        if width != self.WIDTH or height != self.HEIGHT:
            self._apply_window_size(self.WIDTH, self.HEIGHT)

    def _on_mousewheel(self, event: tk.Event) -> None:
        tab = self._main_tab
        limit = int(self._scroll_limits.get(tab, 0) or 0)
        if limit <= 0:
            return
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            wheel_delta = int(getattr(event, "delta", 0) or 0)
            delta = -1 if wheel_delta > 0 else 1
        current = int(self._scroll_offsets.get(tab, 0) or 0)
        self._scroll_offsets[tab] = max(0, min(limit, current + delta * 42))
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
        self._draw()

    def _handle_day_rollover(self, force: bool = False) -> bool:
        current_day = today_key()
        if not force and current_day == self._current_day_key:
            return False
        self._current_day_key = current_day
        self.client.clear_runtime_caches()
        self._account_range_auto_selected = False
        if not self._account_range_user_selected:
            self._account_range = "today"
        return True

    def _schedule_auto_refresh(self) -> None:
        if self.closed:
            return
        self._handle_day_rollover()
        self.refresh_async()
        self.root.after(REFRESH_SECONDS * 1000, self._schedule_auto_refresh)

    def _schedule_midnight_refresh(self) -> None:
        if self.closed:
            return
        now = datetime.now(CN_TZ)
        next_day = now.date() + timedelta(days=1)
        next_midnight = datetime.combine(next_day, datetime.min.time(), tzinfo=CN_TZ)
        delay_ms = max(1000, int((next_midnight - now).total_seconds() * 1000) + 5000)
        self.root.after(delay_ms, self._on_midnight_refresh)

    def _on_midnight_refresh(self) -> None:
        if self.closed:
            return
        self._handle_day_rollover(force=True)
        self.refresh_async()
        self._schedule_midnight_refresh()

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
