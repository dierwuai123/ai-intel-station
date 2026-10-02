#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 情报站后端：repo追踪 + 高分AI工具抓取 + 新闻抓取(RSS+HN) + SQLite存储
独立服务：默认 0.0.0.0:8097（INTEL_PORT 可改），建议置于 nginx 反代之后
数据源：GitHub API（匿名 search/repo）+ Hacker News API + 可配置 RSS（cn_news.py）
定时采集：cron 调 `python3 server.py --collect`，详见 README
"""
import json, os, sqlite3, time, re, subprocess, sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intel.db")
UA = {"User-Agent": "Mozilla/5.0 (ai-intel-station)"}

# ---- 可配置项（环境变量或 config.py，见 README）----
def _cfg(key, default):
    return os.environ.get(key, default)

# 追踪的 GitHub 仓库（逗号分隔 owner/repo）
MY_REPOS = _cfg("INTEL_MY_REPOS",
    "torvalds/linux,python/cpython").split(",")
# GitHub 搜索查询（kind 显示标签；q 支持 GitHub search 语法，:date 由程序注入）
SEARCH_QUERIES = [
    ("skill",      "claude skill"),
    ("review",     "agent code review"),
    ("boilerplate","fastapi saas boilerplate"),
]
# 高分工具抓取窗口：近 N 天新建
DAYS_WINDOW = int(_cfg("INTEL_DAYS_WINDOW", "30"))

def _date_days_ago(n):
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - n*86400))

def db():
    c = sqlite3.connect(DB, timeout=20)  # 等锁 20s，缓解 cron 与 /api/collect 并发写竞态
    c.execute("PRAGMA busy_timeout=20000")
    c.execute("""CREATE TABLE IF NOT EXISTS repos(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT, full_name TEXT, url TEXT, stars INTEGER, lang TEXT,
        desc TEXT, pushed_at TEXT, fetched_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS news(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        src TEXT, title TEXT, url TEXT, score INTEGER, fetched_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS myrepos(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT UNIQUE, stars INTEGER, forks INTEGER, watchers INTEGER,
        open_issues INTEGER, open_prs INTEGER, release TEXT, pushed_at TEXT, fetched_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS pr_reviews(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        repo TEXT, pr INTEGER, title TEXT, author TEXT,
        verdict TEXT, confidence REAL, summary TEXT, risks TEXT, created_at TEXT)""")
    return c

def fetch(url, timeout=20):
    try:
        req = Request(url, headers=UA)
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_err": str(e)[:200]}

def now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

def collect():
    """同步采集：由 cron 独立进程调用（collect_cli），不阻塞 API 请求"""
    c = db()
    # 1) 我的发布仓库追踪（带 0.8s 间隔防匿名限流）
    for repo in [r.strip() for r in MY_REPOS if r.strip()]:
        d = fetch(f"https://api.github.com/repos/{repo}")
        if "full_name" in d:
            c.execute("DELETE FROM myrepos WHERE full_name=?", (repo,))
            release = fetch(f"https://api.github.com/repos/{repo}/releases/latest")
            rel = release.get("tag_name", "") if "tag_name" in release else ""
            c.execute("INSERT INTO myrepos(full_name,stars,forks,watchers,open_issues,open_prs,release,pushed_at,fetched_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (repo, d.get("stargazers_count",0), d.get("forks_count",0), d.get("subscribers_count",0),
                       d.get("open_issues_count",0), 0, rel, d.get("pushed_at",""), now()))
            time.sleep(0.8)
    # 2) 高分 AI 工具/agent 抓取（近7天新建且星数高）——全量刷新：先清旧（保留 zh 列由翻译表重建）
    c.execute("DELETE FROM repos")
    c.execute("DELETE FROM news WHERE src='hn'")
    seen = set()
    for q, kind in [(f"{kw}+created:>2026-09-20&sort=stars&order=desc".replace("2026-09-20", _date_days_ago(DAYS_WINDOW)), kind)
                    for kind, kw in SEARCH_QUERIES]:
        d = fetch(f"https://api.github.com/search/repositories?q={q}&per_page=8")
        for it in d.get("items", []):
            fn = it["full_name"]
            if fn in seen: continue
            seen.add(fn)
            c.execute("INSERT INTO repos(kind,full_name,url,stars,lang,desc,pushed_at,fetched_at) VALUES(?,?,?,?,?,?,?,?)",
                      (kind, fn, it["html_url"], it["stargazers_count"], it.get("language") or "",
                       (it.get("description") or "")[:300], it.get("pushed_at",""), now()))
    # 3) 新闻：Hacker News top + AI 相关
    d = fetch("https://hacker-news.firebaseio.com/v0/topstories.json")
    ids = (d if isinstance(d, list) else [])[:15]
    for i in ids:
        it = fetch(f"https://hacker-news.firebaseio.com/v0/item/{i}.json")
        if it and "title" in it:
            t = it["title"]
            c.execute("INSERT INTO news(src,title,url,score,fetched_at) VALUES(?,?,?,?,?)",
                      ("hn", t, it.get("url") or f"https://news.ycombinator.com/item?id={i}", it.get("score",0), now()))
    c.commit()
    n1 = c.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
    n2 = c.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    n3 = c.execute("SELECT COUNT(*) FROM myrepos").fetchone()[0]
    c.close()
    return f"repos={n1} news={n2} myrepos={n3}"

def limit_rows(c, sql, args=()):
    return [dict(zip([k[0] for k in c.execute(sql, args).description], r))
            for r in c.execute(sql, args).fetchall()]

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # CORS 收紧：仅放行面板所在站点源（INTEL_ORIGIN 环境变量可配）；无 Origin（curl/同源）也放行
        origin = self.headers.get("Origin", "")
        allowed = os.environ.get("INTEL_ORIGIN", "")
        if origin in ("", allowed) and allowed:
            self.send_header("Access-Control-Allow-Origin", origin or allowed)
        if code == 200 and not self.path.startswith("/api/"):
            # 安全头（静态页；API 保持 CORS 通配供面板同源 fetch）
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(b)
    def _file(self, name, ctype="text/html; charset=utf-8"):
        fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        try:
            b = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            if name == "sw.js":
                self.send_header("Service-Worker-Allowed", "/")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(b)
        except FileNotFoundError:
            self._json({"error": "not found"}, 404)
    def do_POST(self):
        p = self.path.split("?")[0]
        c = db()
        try:
            if p == "/api/collect":
                self._handle_collect(c)
            else:
                self._json({"error": "not found"}, 404)
        finally:
            c.close()

    def _handle_collect(self, c):
        # 防滥用：原子锁（运行中拒绝 429）
        lock = "/tmp/intel-collect.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            self._json({"ok": False, "msg": "采集正在进行中，请稍候"}, 429)
            return
        # 异步触发：采集+中文新闻 一起跑（跑完自动删锁）；PR 扫描独立链
        d = os.path.dirname(os.path.abspath(__file__))
        subprocess.Popen(["bash", "-c",
            f"cd {d} && python3 server.py --collect; python3 cn_news.py; rm -f {lock}"],
            stdout=open("/tmp/intel-collect.log", "a"), stderr=subprocess.STDOUT)
        subprocess.Popen(["bash", "-c", f"cd {d} && python3 pr_review.py --watch"],
            stdout=open("/tmp/intel-pr.log", "a"), stderr=subprocess.STDOUT)
        self._json({"ok": True, "msg": "采集+翻译+PR审核已在后台启动，1-3分钟后刷新可见"})

    def do_GET(self):
        p = self.path.split("?")[0]
        c = db()
        try:
            if p in ("/", "/index.html"):
                self._file("index.html")
            elif p == "/sw.js":
                self._file("sw.js", "application/javascript")
            elif p == "/manifest.json":
                self._file("manifest.json", "application/manifest+json")
            elif p == "/api/overview":
                r = {
                    "myrepos": limit_rows(c, "SELECT * FROM myrepos ORDER BY full_name"),
                    "repos": limit_rows(c, "SELECT * FROM repos ORDER BY stars DESC LIMIT 30"),
                    "news": limit_rows(c, "SELECT * FROM news ORDER BY id DESC LIMIT 120"),
                    "pr_reviews": limit_rows(c, "SELECT * FROM pr_reviews ORDER BY id DESC LIMIT 20"),
                    "updated": limit_rows(c, "SELECT MAX(fetched_at) AS t FROM news"),
                }
                self._json(r)
            elif p == "/api/collect":
                self._json({"ok": False, "msg": "use POST"}, 405)
            elif p == "/health":
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        finally:
            c.close()

if __name__ == "__main__":
    if "--collect" in sys.argv:
        print(f"[{now()}] collect start: {collect()}")
        sys.exit(0)
    port = int(os.environ.get("INTEL_PORT", "8097"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    srv.timeout = 30  # 慢连接 30s 断开，防 slowloris 单线程挂死
    H.timeout = 30
    print(f"ai-intel-station on 0.0.0.0:{port} (ThreadingHTTPServer)")
    srv.serve_forever()
