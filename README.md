# AI Intel Station · AI 情报站

A zero-dependency, self-hosted intelligence dashboard for indie developers:
track your GitHub repos, discover trending AI tools, aggregate news (Chinese RSS + Hacker News), and let an LLM pre-review incoming pull requests.

一个零依赖、自托管的个人情报面板：追踪你的 GitHub 仓库、发现近期高分 AI 工具、聚合新闻（中文 RSS + Hacker News），并用 LLM 自动初审 PR。

English | [中文说明](README.zh.md)

## Why / 为什么

Indie devs scatter work across repos and miss signals: a new fork, a hot PR, a competitor shipping. This tool pulls the signals into one private page you can open on your phone — with automatic Chinese translation for English content (optional, works without LLM too).

独立开发者精力分散，仓库多了信号就漏：新 fork、热 PR、竞品动作。这个工具把信号聚到一个私有页面，手机可开；英文内容可选 LLM 自动翻译成中文（不配 LLM 也能正常用）。

## Features / 功能

- **Repo tracking** — stars/forks/watchers/issues + latest release for any repos you list
- **Trending tools** — GitHub search for high-star repos created in the last N days (query configurable)
- **News** — Hacker News top stories + pluggable RSS feeds (defaults: IT之家 / Solidot / 少数派 / InfoQ)
- **Deals intel (羊毛资讯)** — aggregates cloud-vendor promos & free-tier intel (Aliyun/Tencent/Huawei/Volc/Baidu vendor filter + free LLM API tracking + VPS deals); overseas feeds are reachability-probed from a CN network and auto-hidden if blocked
- **AI PR review** — pulls PR diffs, asks an LLM (Volc Ark / OpenAI-compatible) for verdict `merge|reject|needs-review` with prompt-injection hardening; fully optional
- **Auto translation** — batch-translates English descriptions/titles to Chinese (optional)
- **Onboarding wizard** — first-run 3-step setup: paste a zero-permission GitHub token (guided), pick search topics & tracked repos; skippable, editable anytime in Settings
- **User preferences** — dark/light theme, interest topics, personalized recommendation scoring (bilingual keyword matching) for news & tools
- **Repo import** — one-click import of your own GitHub repos via token (up to 20, checkbox picker)
- **Admin PIN** — settings writes (token/repos/topics) protected by a salted-sha256 PIN; GET stays open for read-only dashboard
- **PWA** — installable, pull-to-refresh triggers a collection run
- **Zero dependencies** — Python stdlib only, single SQLite file, ~600 lines total

## Quick start / 快速开始

```bash
git clone https://github.com/dierwuai123/ai-intel-station.git
cd ai-intel-station
python3 server.py                 # http://localhost:8097
# first data run / 首次采集：
python3 server.py --collect && python3 cn_news.py && python3 deals.py
```

Open `http://localhost:8097`. That's it — no pip install, no config file required.

打开 `http://localhost:8097` 即可。无需 pip install，无必需配置文件。

### Optional: LLM translation & PR review / 可选：LLM 翻译与 PR 审核

```bash
export VOLC_ARK_API_KEY=your_key          # Volcengine Ark (OpenAI-compatible)
export VOLC_ARK_EP_ID=your_endpoint_id    # e.g. a GLM / doubao text endpoint
python3 cn_news.py --translate            # translate pending items
python3 pr_review.py --watch              # AI-review all open PRs of tracked repos
```

Any OpenAI-compatible endpoint works if you adapt the base URL in `cn_news.py` / `pr_review.py`. Without a key, everything else still works — you just skip translation and AI review.

任意 OpenAI 兼容端点均可（改一下 base URL 即可）。不配 key 时其余功能完全不受影响。

## Configuration / 配置（全环境变量，均有默认值）

| Variable | Default | Meaning |
|---|---|---|
| `INTEL_PORT` | `8097` | HTTP listen port |
| `INTEL_MY_REPOS` | `torvalds/linux,python/cpython` | Comma-separated repos to track |
| `INTEL_DAYS_WINDOW` | `30` | Trending search window (days) |
| `INTEL_RSS_SOURCES` | IT之家/Solidot/少数派/InfoQ | Custom feeds: `id\|name\|url\|count,...` |
| `INTEL_RSS_KEEP` | `25` | Items to keep per RSS source |
| `VOLC_ARK_API_KEY` / `VOLC_ARK_EP_ID` | — | Optional LLM for translation + PR review |

## Cron / 定时任务

```cron
10 6 * * * cd /path/to/ai-intel-station && python3 server.py --collect >> collect.log 2>&1
25 6 * * * cd /path/to/ai-intel-station && python3 cn_news.py >> cnnews.log 2>&1
30 6 * * * cd /path/to/ai-intel-station && python3 deals.py >> deals.log 2>&1
35 6 * * * cd /path/to/ai-intel-station && python3 cn_news.py --translate >> translate.log 2>&1
45 6 * * * cd /path/to/ai-intel-station && python3 pr_review.py --watch >> pr.log 2>&1
```

## Deployment notes / 部署建议

- Put it behind nginx (it serves plain HTTP; TLS terminates at the proxy)
- `/api/collect` is POST-only with a lock (429 while running) — safe to expose, but prefer keeping the port private behind a reverse proxy
- All data lives in `intel.db` (SQLite, auto-created)

- 建议置于 nginx 之后（服务本身只出 HTTP，TLS 交给反代）
- `/api/collect` 仅接受 POST 且带运行锁（运行中返回 429），但更建议端口不对公网开放
- 所有数据在 `intel.db`（SQLite，自动建表）

## Security / 安全设计

- All interpolated output is HTML-escaped; URLs whitelist `http(s)` only (blocks `javascript:` injection via hijacked feeds)
- PR diffs are wrapped in `<<<UNTRUSTED>>>` fences with anti-injection system prompts
- File permissions: keep `.volc_key` (if used) at 600 — the code does this for you

- 所有输出经 HTML 转义；URL 白名单仅 http(s)（RSS 源被劫持时的 `javascript:` 注入被拦截）
- PR diff 以 `<<<UNTRUSTED>>>` 围栏隔离并配抗提示注入 system prompt
- 密钥缓存文件自动设 600 权限

## License

MIT
