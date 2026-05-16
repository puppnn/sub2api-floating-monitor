from __future__ import annotations

import json
import math
import os
import re
import subprocess
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
CLIENT_USAGE_EXPORT = Path(os.environ.get("CLIENT_USAGE_EXPORT") or APP_DIR / "client_usage_export.py")
CLIENT_USAGE_JSON = Path(os.environ.get("CLIENT_USAGE_JSON") or APP_DIR / "client_usage_today.json")
CN_TZ = timezone(timedelta(hours=8), "CST")
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


def account_health_badge(account: dict[str, Any]) -> str:
    """Return a short account health label for the ranking list."""
    status = str(account.get("status") or "").strip().lower()
    error_message = str(account.get("error_message") or account.get("last_error") or "").strip().lower()
    schedulable = account.get("schedulable")
    temp_until = _parse_time(account.get("temp_unschedulable_until") or account.get("cooldown_until"))

    if any(word in error_message for word in ("quota", "exceed", "insufficient", "limit")):
        return "\u9650\u989d"
    if any(word in error_message for word in ("429", "rate")):
        return "\u9650\u6d41"
    if account.get("quota_exceeded") is True or status in {"quota_exceeded", "quota-exceeded", "quota"}:
        return "\u9650\u989d"
    if temp_until and temp_until > datetime.now(timezone.utc):
        return "\u51b7\u5374"
    if schedulable is False:
        return "\u4e0d\u53ef\u7528"
    if status in {"disabled", "inactive", "suspended", "banned", "unavailable"}:
        return "\u505c\u7528"
    if status in {"error", "failed"} or error_message:
        return "\u9519\u8bef"
    return ""


def load_client_usage() -> dict[str, Any] | None:
    if CLIENT_USAGE_EXPORT.exists():
        try:
            subprocess.run(
                ["python", str(CLIENT_USAGE_EXPORT), "--output", str(CLIENT_USAGE_JSON)],
                cwd=str(APP_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
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
    return {
        "requests": int(today.get("requests") or 0),
        "tokens": int(today.get("tokens") or 0),
        "cost": float(today.get("cost") or 0),
        "providers": data.get("providers") or [],
        "updated_at": data.get("updated_at") or "",
    }


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
    top_accounts: list[dict[str, Any]] | None = None
    client_usage: dict[str, Any] | None = None


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
                    "name": provider.get("name") or "Local client",
                    "tokens": int(provider.get("tokens") or 0),
                    "requests": int(provider.get("requests") or 0),
                    "cost": float(provider.get("cost") or 0),
                    "health_badge": "本地",
                }
            )
    top_accounts.sort(key=lambda row: (-row["tokens"], -row["requests"], row["name"]))

    updated_at = client_usage.get("updated_at") if isinstance(client_usage, dict) else ""
    latest_request = None
    if updated_at:
        latest_request = {
            "kind": "success",
            "model": "local-codex",
            "created_at": updated_at,
        }

    return MonitorState(
        loading=False,
        error=error_text,
        updated_at=time.time(),
        mode="local-codex",
        source_label="LOCAL-CODEX",
        usage_source="local",
        usage_note=usage_note,
        active_accounts=[],
        latest_request=latest_request,
        latest_account_name="Local client logs",
        today_requests=int(client_usage.get("requests") or 0),
        today_tokens=int(client_usage.get("tokens") or 0),
        today_account_cost=float(client_usage.get("cost") or 0),
        top_accounts=top_accounts,
        client_usage=client_usage,
    )


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
        return "sub2api", "Auto: 未确认 Codex endpoint"

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
        if resolved_source == "local":
            return build_local_monitor_state(usage_note=usage_note)
        try:
            return self.fetch_sub2api_state()
        except Exception as exc:
            if self.mode in {"", "auto"}:
                return build_local_monitor_state(str(exc))
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
                    "time_range": "30m",
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
                }
            )
        top_accounts.sort(key=lambda row: (-row["tokens"], -row["requests"], row["name"]))
        client_usage = load_client_usage() if resolved_source == "both" else None
        if client_usage and (client_usage["tokens"] or client_usage["requests"] or client_usage["cost"]):
            top_accounts.append(
                {
                    "name": "Client local / 本地客户端日志",
                    "tokens": client_usage["tokens"],
                    "requests": client_usage["requests"],
                    "cost": client_usage["cost"],
                    "health_badge": "本地",
                }
            )
            top_accounts.sort(key=lambda row: (-row["tokens"], -row["requests"], row["name"]))

        return MonitorState(
            loading=False,
            updated_at=time.time(),
            mode="sub2api",
            source_label="SUB2 监控",
            usage_source=resolved_source,
            usage_note=usage_note,
            active_accounts=active_accounts[:4],
            latest_request=latest,
            latest_account_name=latest_account_name,
            today_requests=realtime_today_requests + int((client_usage or {}).get("requests") or 0),
            today_tokens=realtime_today_tokens + int((client_usage or {}).get("tokens") or 0),
            today_account_cost=realtime_today_cost + float((client_usage or {}).get("cost") or 0),
            top_accounts=top_accounts,
            client_usage=client_usage,
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
    HEIGHT = 680
    MIN_WIDTH = 360
    MIN_HEIGHT = 560
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

        active_count = len(self.state.active_accounts or []) if self.state and self.state.mode == "sub2api" else 0
        updated = "\u8bfb\u53d6\u4e2d" if self._loading else "\u7b49\u5f85\u5237\u65b0"
        if self.state and self.state.updated_at:
            updated = relative_time(datetime.fromtimestamp(self.state.updated_at, timezone.utc).isoformat())
        subtitle = f"\u6d3b\u8dc3 {active_count}  /  {updated}"
        if self.state and self.state.mode == "local-codex":
            subtitle = f"\u672c\u5730\u6a21\u5f0f  /  {updated}"
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
        if self.state and self.state.mode == "local-codex":
            hero_name = "Local Codex / Claude"
            hero_sub = "\u4ec5\u7edf\u8ba1\u672c\u673a\u5ba2\u6237\u7aef\u65e5\u5fd7"
            hero_color = Theme.cyan
        elif accounts:
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
        self._draw_pill(COL_R - 94, y + 44, "\u672c\u5730", Theme.amber_bright, 76)
        y += 82

        # ════════════════════════════════════════════════════════
        #  ACTIVE ACCOUNTS
        # ════════════════════════════════════════════════════════
        c.create_text(COL_L, y, anchor="nw", text="\u5f53\u524d\u6d3b\u8dc3",
                       font=self._fonts["font_section"], fill=Theme.amber)

        y += 24
        if self.state and self.state.mode == "local-codex":
            c.create_text(COL_L + 8, y, anchor="nw", text="\u672c\u5730\u6a21\u5f0f\u4e0d\u663e\u793a Sub2API \u5e76\u53d1",
                           font=self._fonts["font_label"], fill=Theme.text_muted)
            y += 20
        elif not accounts:
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
                source_text = f"\u542b\u672c\u5730 {compact_number(client_tokens)} tok"
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

        # ════════════════════════════════════════════════════════
        #  TOP ACCOUNTS
        # ════════════════════════════════════════════════════════
        y += 10
        c.create_text(COL_L, y, anchor="nw", text="\u8d26\u53f7\u6392\u884c",
                       font=self._fonts["font_section"], fill=Theme.amber)
        y += 20

        top = self.state.top_accounts if self.state else []
        if not top:
            c.create_text(COL_L + 8, y, anchor="nw", text="\u6682\u65e0\u7528\u91cf",
                           font=self._fonts["font_label"], fill=Theme.text_muted)
        available_rank_rows = max(1, (H - 44 - y) // 30)
        max_rank_rows = min(len(top), max(3, available_rank_rows))
        max_tokens = max([int(acc.get("tokens") or 0) for acc in top], default=1) or 1
        for index, acc in enumerate(top[:max_rank_rows]):
            health_badge = str(acc.get("health_badge") or "")
            name_max_w = 112 if health_badge else 150
            name = self._truncate(acc.get("name", "-"), "font_label", name_max_w)
            tokens = compact_number(acc.get("tokens", 0))
            reqs = compact_number(acc.get("requests", 0))
            cost = money(acc.get("cost", 0))
            bar_color = Theme.amber if index == 0 else (Theme.cyan if index == 1 else (Theme.violet if index == 2 else Theme.blue))

            c.create_text(COL_L + 8, y, anchor="nw", text=name,
                           font=self._fonts["font_label"], fill=Theme.text_primary)
            if health_badge:
                badge_x = COL_L + 12 + self._text_width(name, "font_label")
                self._draw_health_badge(badge_x, y + 1, health_badge)

            emphasis = f"{tokens} tok  {cost}"
            req_text = f"{reqs} req"
            c.create_text(COL_R - 4, y, anchor="ne", text=emphasis,
                           font=self._fonts["font_label_bold"], fill=bar_color)
            emphasis_w = self._text_width(emphasis, "font_label_bold")
            c.create_text(COL_R - 12 - emphasis_w, y + 1, anchor="ne", text=req_text,
                           font=self._fonts["font_tiny"], fill=Theme.text_secondary)
            bar_y = y + 20
            bar_x1 = COL_L + 8
            bar_x2 = COL_R - 4
            self._draw_rounded_rect(bar_x1, bar_y, bar_x2, bar_y + 6, r=3, fill=Theme.bg_dark, outline="")
            tokens_raw = int(acc.get("tokens") or 0)
            fill_w = int((bar_x2 - bar_x1) * max(0.04, min(1.0, tokens_raw / max_tokens)))
            self._draw_rounded_rect(bar_x1, bar_y, bar_x1 + fill_w, bar_y + 6, r=3, fill=bar_color, outline="")
            y += 30

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
            self.refresh_async()
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

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  DATA REFRESH
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
            self.state = result
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
