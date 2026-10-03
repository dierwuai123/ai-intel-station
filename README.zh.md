# AI 情报站 · AI Intel Station

零依赖、自托管的个人情报面板：追踪你的 GitHub 仓库、发现近期高分 AI 工具、聚合新闻（中文 RSS + Hacker News），并用 LLM 自动初审 PR。

[English](README.md) | 中文

## 为什么做这个

独立开发者精力分散，仓库多了信号就漏：新 fork、热 PR、竞品动作。这个工具把信号聚到一个私有页面，手机可开（PWA 支持下拉刷新触发采集）；英文内容可选 LLM 自动翻译成中文，不配 LLM 也能正常用。

## 功能

- **仓库追踪** — 自选任意仓库的 stars/forks/watchers/issues + 最新 Release
- **高分工具** — GitHub 搜索近 N 天新建的高星仓库（查询词可配）
- **新闻聚合** — Hacker News 头条 + 可插拔 RSS（默认：IT之家 / Solidot / 少数派 / InfoQ）
- **羊毛资讯** — 聚合云厂商优惠与免费额度情报（阿里/腾讯/华为/火山/百度等标签筛选 + 免费 LLM 额度追踪 + VPS 特价），海外源自动探测国内可达性，被墙即隐藏
- **AI 审 PR** — 拉 PR diff → LLM（火山 Ark / OpenAI 兼容）出 verdict `merge|reject|needs-review`，带提示注入加固；完全可选
- **自动翻译** — 英文描述/标题批量翻译成中文（可选）
- **首次配置向导** — 首次打开三步引导：粘贴零权限 GitHub Token（含教程）、选兴趣方向、选追踪仓库；可跳过，设置里随时改
- **偏好设置** — 深色/浅色主题、兴趣方向，按方向对新闻和工具做双语关键词推荐打分
- **仓库一键导入** — 凭 Token 拉取你名下仓库勾选导入（最多 20 个）
- **管理密码** — 设置类写操作（Token/仓库/方向）需 salted-sha256 管理密码，读操作保持开放
- **PWA** — 可安装到手机，下拉刷新触发采集
- **零依赖** — 纯 Python 标准库，单 SQLite 文件，总共约 600 行

## 快速开始

```bash
git clone https://github.com/dierwuai123/ai-intel-station.git
cd ai-intel-station
python3 server.py                 # http://localhost:8097
# 首次采集：
python3 server.py --collect && python3 cn_news.py && python3 deals.py
```

打开 `http://localhost:8097` 即可。无需 pip install，无必需配置文件。

### 可选：LLM 翻译与 PR 审核

```bash
export VOLC_ARK_API_KEY=你的key          # 火山方舟（OpenAI 兼容协议）
export VOLC_ARK_EP_ID=你的端点ID          # 如 GLM / doubao 文本端点
python3 cn_news.py --translate            # 翻译待翻条目
python3 pr_review.py --watch              # AI 审核追踪仓库的所有 open PR
```

任意 OpenAI 兼容端点均可（改 `cn_news.py` / `pr_review.py` 里的 base URL）。不配 key 时其余功能完全不受影响。

## 配置（全环境变量，均有默认值）

| 变量 | 默认值 | 含义 |
|---|---|---|
| `INTEL_PORT` | `8097` | HTTP 监听端口 |
| `INTEL_MY_REPOS` | `torvalds/linux,python/cpython` | 追踪的仓库（逗号分隔） |
| `INTEL_DAYS_WINDOW` | `30` | 高分工具搜索窗口（天） |
| `INTEL_RSS_SOURCES` | IT之家/Solidot/少数派/InfoQ | 自定义源：`id\|名称\|url\|条数,...` |
| `INTEL_RSS_KEEP` | `25` | 每个 RSS 源保留条数 |
| `VOLC_ARK_API_KEY` / `VOLC_ARK_EP_ID` | — | 可选 LLM（翻译+审 PR） |

## 定时任务

```cron
10 6 * * * cd /path/to/ai-intel-station && python3 server.py --collect >> collect.log 2>&1
25 6 * * * cd /path/to/ai-intel-station && python3 cn_news.py >> cnnews.log 2>&1
30 6 * * * cd /path/to/ai-intel-station && python3 deals.py >> deals.log 2>&1
35 6 * * * cd /path/to/ai-intel-station && python3 cn_news.py --translate >> translate.log 2>&1
45 6 * * * cd /path/to/ai-intel-station && python3 pr_review.py --watch >> pr.log 2>&1
```

## 部署建议

- 建议置于 nginx 之后（服务本身只出 HTTP，TLS 交给反代）
- `/api/collect` 仅接受 POST 且带运行锁（运行中返回 429），但更建议端口不对公网开放
- 所有数据在 `intel.db`（SQLite，自动建表）

## 安全设计

- 所有输出经 HTML 转义；URL 白名单仅 http(s)（RSS 源被劫持时的 `javascript:` 注入被拦截）
- PR diff 以 `<<<UNTRUSTED>>>` 围栏隔离并配抗提示注入 system prompt
- 密钥缓存文件自动设 600 权限

## License

MIT
