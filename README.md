# Token Floating Monitor

一个轻量级 Windows Token 悬浮窗，用 Python/Tk 写成，不需要额外 Python 依赖。它可以在桌面上显示当前活跃账号、并发、今日请求量、Token、成本、账号额度窗口，以及更细的用量统计面板。

> 截图使用脱敏演示数据生成，仅用于展示界面结构和功能。

## 界面预览

### 账号

显示当前活跃账号、最近请求、今日统计、Token 趋势和账号用量排行。支持 `今日 / 近5小时 / 近7天 / 周期` 切换。

![账号页](assets/screenshots/accounts.png)

### Token 预算

展示 5h、7d、cycle 等额度窗口的剩余比例、已用比例、重置时间、待刷新状态和低余额压力。

![Token 预算页](assets/screenshots/budget.png)

### 用量统计

提供 `24h / 7d / 30d / All` 视图，包含 Token Chips、缓存命中率、Activity 热力图、Top Models 和 Provider 成本排行。

![用量统计页](assets/screenshots/usage-stats.png)

## 主要功能

- 桌面悬浮窗：支持置顶、拖动、缩放、刷新和关闭。
- 三个中文页签：`账号`、`Token 预算`、`用量统计`。
- 活跃账号与并发：显示当前正在使用的账号，以及总并发/账号并发。
- 账号排行：按今日、近 5 小时、近 7 天、周期窗口查看账号用量。
- 额度窗口：展示 5h、7d、cycle 的剩余百分比、已用比例、重置时间、无额度和 stale 状态。
- 用量统计：展示请求数、Token、成本、input/cache/output 构成、缓存命中率、Top Models 和 Provider 成本。
- Activity 热力图：支持 24h、7d、30d、All time，不同强度颜色表示用量高低，鼠标悬停可查看具体值。
- 本地历史：记录每日请求、Token 和成本快照，用于趋势和历史统计。
- 去重逻辑：保留 Codex fork replay 去重、Sub2API mirror 扣除、成本估算等原有统计规则。

## 运行要求

- Windows
- Python 3.10+
- Tkinter，Windows 官方 Python 通常自带

项目不需要安装额外 Python 包。

## 快速开始

自动模式会优先读取 Sub2API，失败时回退到本地客户端日志：

```powershell
.\start-monitor.ps1
```

只读取本地客户端日志：

```powershell
.\start-local-codex.ps1
```

也可以直接运行：

```powershell
python .\monitor.py
```

## Sub2API 配置

复制 `.env.example` 为 `.env`，填写你的本地 Sub2API 管理端配置：

```env
SUB2API_MONITOR_MODE=auto
SUB2API_BASE_URL=http://127.0.0.1:8080
SUB2API_ADMIN_EMAIL=admin@sub2api.local
SUB2API_ADMIN_PASSWORD=your-password
```

如果希望必须连接 Sub2API，不允许回退到本地日志：

```env
SUB2API_MONITOR_MODE=sub2api
```

如果你的 Sub2API 有多个本地访问地址，可以配置匹配地址：

```env
SUB2API_MATCH_BASE_URLS=http://127.0.0.1:8080,http://localhost:8080
```

## 本地客户端日志模式

本地模式会扫描本机客户端日志并生成 `client_usage_today.json` 作为当天统计缓存。默认扫描路径包括：

- `%USERPROFILE%\.codex\sessions`
- `%USERPROFILE%\.claude\projects`

常用配置：

```env
SUB2API_MONITOR_MODE=local-codex
CLIENT_USAGE_CODEX_DEFAULT_MODEL=gpt-5.5
CLIENT_USAGE_MAX_SINGLE_EVENT_TOKENS=2000000
SUB2API_INCLUDE_LOCAL_USAGE=false
SUB2API_MONITOR_USAGE_SOURCE=auto
```

`CLIENT_USAGE_MAX_SINGLE_EVENT_TOKENS` 用来过滤异常大的单次 token 事件。

## 统计来源

`SUB2API_MONITOR_USAGE_SOURCE` 支持：

- `auto`：自动检测当前 Codex endpoint。
- `sub2api`：只使用 Sub2API 服务端统计。
- `local`：只使用本地客户端日志。
- `both`：同时展示 Sub2API 服务端统计和本地日志，适合对账，但可能重复计算。

默认建议使用 `auto`。当 Codex 指向你的 Sub2API 地址时，主统计优先来自 Sub2API；因为客户端本身也会写本地 token 日志，默认不会把本地日志直接合并进总量，避免重复计算。

## 隐私说明

- `.env`、本地配置、当天统计缓存、历史统计缓存和归因 ledger 默认都在 `.gitignore` 中，不会提交到 Git。
- 本地模式只读取你电脑上的日志文件，不会主动上传到第三方。
- Sub2API 模式只请求你配置的 `SUB2API_BASE_URL`。
- 仓库中的截图使用脱敏演示数据，不包含真实账号或真实用量。

## 文件说明

- `monitor.py`：悬浮窗 UI、Sub2API 读取、本地统计整合和页面绘制。
- `client_usage_export.py`：本地客户端 JSONL 用量扫描器。
- `start-monitor.ps1`：自动模式启动脚本。
- `start-local-codex.ps1`：本地日志模式启动脚本。
- `run-monitor.cmd`：CMD 启动脚本。
- `run-client-usage-export.cmd`：单独导出本地用量 JSON。

## 验证

```powershell
python -m py_compile monitor.py client_usage_export.py
python client_usage_export.py --output client_usage_today.json
```
