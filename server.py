#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 情报站后端：repo追踪 + 高分工具抓取 + 新闻(RSS+HN) + SQLite + 首次配置向导/偏好推荐
独立服务：默认 0.0.0.0:8097（INTEL_PORT 可改），建议置于 nginx 反代之后
数据源：GitHub API + Hacker News + 可配置 RSS（cn_news.py）
定时采集：cron 调 python3 server.py --collect
配置：首次打开页面会出现配置向导（GitHub Token / 兴趣方向 / 追踪仓库 / 管理 PIN），
     也支持环境变量 INTEL_MY_REPOS / INTEL_DAYS_WINDOW 预置；设置存 SQLite settings 表。
安全：管理 PIN 只存 sha256(salt+pin)；GitHub Token 只入库（文件 600）永不下发前端；
     设置写入需 PIN；跨站 Origin 校验；Token/仓库/方向均做格式白名单。
"""
import json, os, sqlite3, time, re, subprocess, sys, hashlib, hmac
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.parse import quote
from urllib.error import URLError, HTTPError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, "intel.db")
UA = {"User-Agent": "Mozilla/5.0 (ai-intel-station)"}

def _cfg(key, default=""):
    return os.environ.get(key, default)

DEFAULT_TOPICS = ["ai agent", "code review", "saas boilerplate"]
DEFAULT_REPOS = [r.strip() for r in _cfg("INTEL_MY_REPOS").split(",") if r.strip()]
DEFAULT_DAYS = int(_cfg("INTEL_DAYS_WINDOW", "30") or 30)
ALLOWED_ORIGIN = _cfg("INTEL_ORIGIN")  # 例: https://your.domain —— 非空时校验跨站 Origin

RE_REPO = re.compile(r"^[\w.-]+/[\w\.-]{1,100}$")
RE_TOKEN = re.compile(r"^[A-Za-z0-9_]{20,128}$")
RE_KW = re.compile(r"^[\w\u4e00-\u9fff .+\-]{1,60}$")

_GH_TOKEN = {"v": None, "loaded": False}
_VT_HITS = {}  # verify-token 每 IP 限速表 {ip: [ts,...]}

def now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

def _date_days_ago(n):
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400))

def db():
    c = sqlite3.connect(DB, timeout=20)
    c.execute("PRAGMA busy_timeout=20000")
    try:
        c.execute("PRAGMA journal_mode=WAL")  # 读写不互斥，采集长任务不再拖死页面查询
    except Exception:
        pass  # WAL 不可用时退回默认 journal，功能不受影响
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
    c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS deals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        src TEXT, vendor TEXT, title TEXT, url TEXT,
        score INTEGER DEFAULT 0, fetched_at TEXT)""")
    return c

# ---------- 设置 ----------
DEFAULTS = {"theme": "dark", "days_window": DEFAULT_DAYS,
            "my_repos": DEFAULT_REPOS, "search_topics": DEFAULT_TOPICS}

def _raw_setting(key):
    c = db()
    try:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    finally:
        c.close()
    return r[0] if r else None

def get_settings():
    c = db()
    try:
        rows = dict(c.execute("SELECT key,value FROM settings").fetchall())
    finally:
        c.close()
    out = {}
    for k, dv in DEFAULTS.items():
        v = rows.get(k)
        if v is None:
            out[k] = dv
        elif isinstance(dv, str):
            out[k] = v  # 字符串型默认值（theme）直接存取，不要求 JSON
        else:
            try:
                out[k] = json.loads(v)
            except Exception:
                out[k] = dv
    out["setup_done"] = rows.get("setup_done") == "1"
    out["token_set"] = bool(rows.get("github_token"))
    out["pin_set"] = bool(rows.get("pin_hash"))
    return out

def set_settings(**kv):
    c = db()
    try:
        for k, v in kv.items():
            val = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            c.execute("""INSERT INTO settings(key,value) VALUES(?,?)
                         ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (k, val))
        c.commit()
    finally:
        c.close()

def _pin_hash_ok(pin, salt, stored):
    if not stored:
        return True  # 未设 PIN = 初始化模式
    if not pin:
        return False
    cand = hashlib.sha256(((salt or "") + pin).encode()).hexdigest()
    return hmac.compare_digest(cand, stored)

# ---------- 抓取 ----------
def gh_headers():
    if not _GH_TOKEN["loaded"]:
        _GH_TOKEN["v"] = _raw_setting("github_token")
        _GH_TOKEN["loaded"] = True
    h = dict(UA)
    if _GH_TOKEN["v"]:
        h["Authorization"] = "Bearer " + _GH_TOKEN["v"]
    return h

def fetch(url, timeout=20, headers=None):
    try:
        h = gh_headers() if "api.github.com" in url else dict(UA)
        if headers:
            h.update(headers)
        req = Request(url, headers=h)
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_err": str(e)[:200]}

def collect():
    """同步采集：cron / POST /api/collect 调用。
    先取数（无 DB 连接）后短事务写库——避免长事务持锁拖死并发写请求。"""
    s = get_settings()
    my_repos = s["my_repos"][:20]
    topics = s["search_topics"][:8]
    days_window = s["days_window"]
    ts = now()
    # 1) 追踪仓库（0.8s 间隔防匿名限流）—— 纯网络取数，不开库
    myrows = []
    for repo in my_repos:
        if not RE_REPO.match(repo):
            continue
        d = fetch(f"https://api.github.com/repos/{repo}")
        if "full_name" in d:
            rel = fetch(f"https://api.github.com/repos/{repo}/releases/latest")
            rel_name = rel.get("tag_name", "") if "tag_name" in rel else ""
            myrows.append((repo, d.get("stargazers_count", 0), d.get("forks_count", 0),
                           d.get("subscribers_count", 0), d.get("open_issues_count", 0),
                           rel_name, d.get("pushed_at", ""), ts))
        time.sleep(0.8)
    # 2) HN：先抓成功才替换（失败保留旧数据，防空窗）—— 纯网络取数
    hn_new = []
    d = fetch("https://hacker-news.firebaseio.com/v0/topstories.json")
    for i in (d if isinstance(d, list) else [])[:15]:
        it = fetch(f"https://hacker-news.firebaseio.com/v0/item/{i}.json")
        if it and "title" in it:
            hn_new.append((it["title"], it.get("url") or f"https://news.ycombinator.com/item?id={i}",
                           it.get("score", 0)))
    # 3) 工具抓取：按用户兴趣方向搜索 —— 纯网络取数
    found, seen = [], set()
    date_q = _date_days_ago(days_window)
    for kw in topics:
        kw = kw.strip()[:60]
        if not kw or not RE_KW.match(kw.replace(" ", "+")):
            continue
        q = quote(kw.replace(" ", "+"), safe="+.-")
        d = fetch(f"https://api.github.com/search/repositories?q={q}+created:%3E{date_q}&sort=stars&order=desc&per_page=8")
        for it in d.get("items", []):
            fn = it["full_name"]
            if fn in seen:
                continue
            seen.add(fn)
            found.append((kw[:20], fn, it["html_url"], it.get("stargazers_count", 0),
                          it.get("language") or "", (it.get("description") or "")[:300],
                          it.get("pushed_at", "")))
        time.sleep(0.6)
    # 短事务写库：全部取数完成后一次写入（持锁 <1s）
    c = db()
    try:
        # myrepos 插入（9 列）
        for row in myrows:
            repo, st, fk, w, oi, rel_name, ps, _ts = row
            c.execute("DELETE FROM myrepos WHERE full_name=?", (repo,))
            c.execute("""INSERT INTO myrepos(full_name,stars,forks,watchers,open_issues,open_prs,release,pushed_at,fetched_at)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (repo, st, fk, w, oi, 0, rel_name, ps, _ts))
        if hn_new:
            c.execute("DELETE FROM news WHERE src='hn'")
            for t, u, sc in hn_new:
                c.execute("INSERT INTO news(src,title,url,score,fetched_at) VALUES(?,?,?,?,?)",
                          ("hn", t, u, sc, ts))
        if found:
            c.execute("DELETE FROM repos")
            c.executemany("""INSERT INTO repos(kind,full_name,url,stars,lang,desc,pushed_at,fetched_at)
                             VALUES(?,?,?,?,?,?,?,?)""",
                          [(k, f, u, st, lg, ds, ps, ts) for k, f, u, st, lg, ds, ps in found])
        c.commit()
        n1 = c.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
        n2 = c.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        n3 = c.execute("SELECT COUNT(*) FROM myrepos").fetchone()[0]
        return f"repos={n1} news={n2} myrepos={n3}"
    finally:
        c.close()

def limit_rows(c, sql, args=()):
    return [dict(zip([k[0] for k in c.execute(sql, args).description], r))
            for r in c.execute(sql, args).fetchall()]

# 中英同义词表：用户方向 → 匹配词根（中文新闻译名/英文原名都能命中）
TOPIC_SYNONYMS = {
    "ai agent": ["ai agent", "agent", "智能体", "ai 助手", "agentic"],
    "code review": ["code review", "代码审查", "代码评审"],
    "rag": ["rag", "检索增强"],
    "mcp": ["mcp"],
    "quant": ["quant", "量化交易", "量化"],
    "独立开发": ["独立开发", "indie hacker", "solo dev", "一人公司"],
}

def _topic_roots(topics):
    """展开用户方向为匹配词根列表（含同义词）"""
    roots = []
    for t in topics:
        t = t.lower().strip()
        if not t:
            continue
        if t in TOPIC_SYNONYMS:
            roots.extend(TOPIC_SYNONYMS[t])
        else:
            roots.append(t)
    return [r for r in roots if r]

def _apply_rec(repos, news, topics):
    """按兴趣方向打推荐分：中英同义词匹配，命中标题/描述即 _rec"""
    kws = _topic_roots(topics)
    def score(text):
        tl = (text or "").lower()
        return sum(2 for k in kws if k in tl)
    for r in repos:
        r["_score"] = score((r.get("full_name") or "") + " " + (r.get("desc") or "") + " " + (r.get("desc_zh") or ""))
        r["_rec"] = r["_score"] > 0
    repos.sort(key=lambda r: (-r["_score"], -(r.get("stars") or 0)))
    for n in news:
        n["_rec"] = score((n.get("title") or "") + " " + (n.get("title_zh") or "")) > 0

# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _origin_ok(self):
        """CSRF 防护：默认同源校验（比对 Host），跨站 Origin 一律拒绝。
        反代场景 Host 已被 nginx 透传为公网域名，同源判断依旧成立。"""
        o = self.headers.get("Origin", "")
        if not o:
            return True  # curl/同源 GET 无 Origin，放行
        host = self.headers.get("Host", "")
        if not host:
            return False
        try:
            from urllib.parse import urlparse
            oh = urlparse(o).netloc
        except Exception:
            return False
        if oh == host:
            return True
        return bool(ALLOWED_ORIGIN) and o == ALLOWED_ORIGIN  # 显式配置白名单兜底

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        origin = self.headers.get("Origin", "")
        if origin and ALLOWED_ORIGIN and origin == ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", origin)
        if not self.path.startswith("/api/"):
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(b)

    def _file(self, name, ctype="text/html; charset=utf-8"):
        fp = os.path.join(BASE_DIR, name)
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

    def _body(self, cap=20000):
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
        except (ValueError, TypeError):
            return None
        if ln > cap:
            return None
        try:
            return json.loads(self.rfile.read(ln) or b"{}")
        except Exception:
            return None

    def do_POST(self):
        p = self.path.split("?")[0]
        if not self._origin_ok():
            self._json({"ok": False, "msg": "origin rejected"}, 403)
            return
        c = db()
        try:
            if p == "/api/collect":
                self._handle_collect()
            elif p == "/api/settings":
                self._handle_settings()
            elif p == "/api/verify-token":
                self._handle_verify_token()
            elif p == "/api/list-repos":
                self._handle_list_repos()
            else:
                self._json({"error": "not found"}, 404)
        finally:
            c.close()

    def _handle_collect(self):
        # 锁文件放 700 私有目录（/tmp 全局可写，可被预置锁 DoS）
        lock_dir = os.path.join(BASE_DIR, ".locks")
        os.makedirs(lock_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(lock_dir, 0o700)
        except Exception:
            pass
        lock = os.path.join(lock_dir, "collect.lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            # 陈旧锁自愈：超 30 分钟视为残留，删除后允许重新触发
            try:
                if time.time() - os.path.getmtime(lock) > 1800:
                    os.unlink(lock)
                    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.write(fd, str(os.getpid()).encode())
                    os.close(fd)
                else:
                    self._json({"ok": False, "msg": "采集正在进行中，请稍候"}, 429)
                    return
            except FileExistsError:
                self._json({"ok": False, "msg": "采集正在进行中，请稍候"}, 429)
                return
        d = BASE_DIR
        subprocess.Popen(["bash", "-c",
            f"cd {d} && python3 server.py --collect; python3 cn_news.py; python3 deals.py; rm -f {lock}"],
            stdout=open("/tmp/intel-collect.log", "a"), stderr=subprocess.STDOUT)
        subprocess.Popen(["bash", "-c", f"cd {d} && python3 pr_review.py --watch"],
            stdout=open("/tmp/intel-pr.log", "a"), stderr=subprocess.STDOUT)
        self._json({"ok": True, "msg": "采集+翻译+PR审核已在后台启动，1-3分钟后刷新可见"})

    def _handle_verify_token(self):
        body = self._body()
        if body is None:
            self._json({"ok": False, "msg": "bad request"}, 400)
            return
        t = str(body.get("token") or "").strip()
        if not RE_TOKEN.match(t):
            self._json({"valid": 0, "msg": "Token 格式不对（应为 github_pat_/ghp_ 开头或 40 位字符串）"})
            return
        # 每 IP 简易限速：60s 窗口内最多 5 次校验（防 Token 爆破 oracle）
        ip = self.client_address[0]
        now_t = time.time()
        _VT_HITS.setdefault(ip, []).append(now_t)
        _VT_HITS[ip] = [x for x in _VT_HITS[ip] if now_t - x < 60]
        if len(_VT_HITS[ip]) > 5:
            self._json({"valid": 0, "msg": "校验太频繁，请 1 分钟后再试"})
            return
        time.sleep(0.4)  # 轻防爆破
        try:
            req = Request("https://api.github.com/user",
                          headers={**UA, "Authorization": f"Bearer {t}"})
            with urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode())
            self._json({"valid": 1, "login": d.get("login", "")})
        except Exception:
            self._json({"valid": 0, "msg": "GitHub 校验失败：Token 无效或网络不通"})

    def _handle_list_repos(self):
        """读取 Token 名下仓库（需先在本次请求带 token，或已存 token + PIN）"""
        body = self._body()
        if body is None:
            self._json({"ok": False, "msg": "bad request"}, 400)
            return
        t = str(body.get("token") or "").strip()
        pin = str(body.get("pin") or self.headers.get("X-Intel-Pin") or "")
        stored_hash = _raw_setting("pin_hash")
        stored_salt = _raw_setting("pin_salt")
        if not t:
            # 使用库存 Token：必须已设管理密码且校验通过（fail-closed）
            t = _raw_setting("github_token") or ""
            if not t:
                self._json({"ok": False, "msg": "先在下方保存 Token，再导入名下仓库"}, 400)
                return
            if not stored_hash:
                self._json({"ok": False, "msg": "请先在「管理密码」卡设置密码，再导入名下仓库"}, 403)
                return
            if not _pin_hash_ok(pin, stored_salt, stored_hash):
                time.sleep(0.6)
                self._json({"ok": False, "msg": "管理密码不对"}, 403)
                return
        elif stored_hash:
            # 即使自带 token，只要设置了管理密码也要求校验（防旁路）
            if not _pin_hash_ok(pin, stored_salt, stored_hash):
                time.sleep(0.6)
                self._json({"ok": False, "msg": "管理密码不对"}, 403)
                return
        if not RE_TOKEN.match(t):
            self._json({"ok": False, "msg": "Token 格式不对"}, 400)
            return
        out, page = [], 1
        while page <= 4:  # 最多 400 个仓库
            d = fetch(f"https://api.github.com/user/repos?per_page=100&page={page}&sort=pushed",
                      headers={"Authorization": f"Bearer {t}"})
            if not isinstance(d, list) or not d:
                break
            for it in d:
                out.append({"full_name": it["full_name"], "stars": it.get("stargazers_count", 0),
                            "desc": (it.get("description") or "")[:80],
                            "private": bool(it.get("private"))})
            page += 1
        if not out:
            self._json({"ok": False, "msg": "拉取失败：Token 无效/无 repo 权限/网络不通"})
            return
        out.sort(key=lambda x: -x["stars"])
        self._json({"ok": True, "login": (fetch("https://api.github.com/user",
                    headers={"Authorization": f"Bearer {t}"}).get("login") if out else ""), "repos": out})

    def _handle_settings(self):
        body = self._body()
        if not isinstance(body, dict):
            self._json({"ok": False, "msg": "bad request"}, 400)
            return
        pin = str(body.get("pin") or self.headers.get("X-Intel-Pin") or "")
        stored_hash = _raw_setting("pin_hash")
        stored_salt = _raw_setting("pin_salt")
        setting_pin = False
        if not stored_hash:
            # 初始化模式：pin 即首次设置的管理密码（允许为空=暂不设置）
            # 仅限 setup_done 未置（首次部署）——防止恶意网页抢设 PIN 锁死配置
            if pin:
                if body.get("setup_done"):
                    self._json({"ok": False, "msg": "已初始化，请先在服务器本地设置管理密码"}, 403)
                    return
                if not (4 <= len(pin) <= 64):
                    self._json({"ok": False, "msg": "管理密码需 4-64 位"}, 400)
                    return
                setting_pin = True
        else:
            if not _pin_hash_ok(pin, stored_salt, stored_hash):
                time.sleep(0.6)
                self._json({"ok": False, "msg": "管理密码不对"}, 403)
                return
        updates = {}
        if "theme" in body:
            if body["theme"] not in ("dark", "light", "auto"):
                self._json({"ok": False, "msg": "theme 只支持 dark/light/auto"}, 400)
                return
            updates["theme"] = body["theme"]
        if "days_window" in body:
            try:
                dw = int(body["days_window"])
            except Exception:
                dw = 0
            if not 1 <= dw <= 90:
                self._json({"ok": False, "msg": "抓取窗口需 1-90 天"}, 400)
                return
            updates["days_window"] = dw
        if "my_repos" in body:
            if not isinstance(body["my_repos"], list):
                self._json({"ok": False, "msg": "my_repos 需为数组"}, 400)
                return
            repos, bad = [], []
            for r in body["my_repos"][:30]:
                r = str(r).strip()
                if r and RE_REPO.match(r):
                    if r not in repos:
                        repos.append(r)
                elif r:
                    bad.append(r[:40])
            if bad:
                self._json({"ok": False, "msg": "仓库格式应为 owner/repo，已拒绝：" + "、".join(bad[:3])}, 400)
                return
            if len(repos) > 20:
                self._json({"ok": False, "msg": "追踪仓库最多 20 个"}, 400)
                return
            updates["my_repos"] = repos
        if "search_topics" in body:
            if not isinstance(body["search_topics"], list):
                self._json({"ok": False, "msg": "search_topics 需为数组"}, 400)
                return
            topics, badt = [], []
            for t in body["search_topics"][:20]:
                t = str(t).strip()[:60]
                if t and RE_KW.match(t.replace(" ", "+")):
                    if t.lower() not in [x.lower() for x in topics]:
                        topics.append(t)
                elif t:
                    badt.append(t[:30])
            if badt:
                self._json({"ok": False, "msg": "方向含非法字符，已拒绝：" + "、".join(badt[:3])}, 400)
                return
            if len(topics) > 8:
                self._json({"ok": False, "msg": "兴趣方向最多 8 个"}, 400)
                return
            updates["search_topics"] = topics
        if "github_token" in body:
            t = str(body["github_token"]).strip()
            if t == "":
                updates["github_token"] = ""  # 移除
            elif RE_TOKEN.match(t):
                updates["github_token"] = t
            else:
                self._json({"ok": False, "msg": "Token 格式不对"}, 400)
                return
        new_pin = str(body.get("new_pin") or "")
        if new_pin and not (4 <= len(new_pin) <= 64):
            self._json({"ok": False, "msg": "新管理密码需 4-64 位"}, 400)
            return
        if updates:
            set_settings(**updates)
        if setting_pin:
            import secrets as _s
            salt = _s.token_hex(8)
            set_settings(pin_salt=salt,
                         pin_hash=hashlib.sha256((salt + pin).encode()).hexdigest())
        elif new_pin:
            salt = os.urandom(8).hex()
            set_settings(pin_salt=salt,
                         pin_hash=hashlib.sha256((salt + new_pin).encode()).hexdigest())
        if body.get("setup_done"):
            set_settings(setup_done="1")
        _GH_TOKEN["loaded"] = False
        self._json({"ok": True})

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
                s = get_settings()
                repos = limit_rows(c, "SELECT * FROM repos")
                news = limit_rows(c, "SELECT * FROM news ORDER BY id DESC LIMIT 120")
                _apply_rec(repos, news, s["search_topics"])
                deals = limit_rows(c, "SELECT * FROM deals ORDER BY id DESC LIMIT 150")
                r = {
                    "myrepos": limit_rows(c, "SELECT * FROM myrepos ORDER BY full_name"),
                    "repos": repos[:30],
                    "news": news,
                    "deals": deals,
                    "pr_reviews": limit_rows(c, "SELECT * FROM pr_reviews ORDER BY id DESC LIMIT 20"),
                    "updated": limit_rows(c, "SELECT MAX(fetched_at) AS t FROM news"),
                }
                self._json(r)
            elif p == "/api/settings":
                s = get_settings()
                self._json({k: s[k] for k in
                            ("setup_done", "token_set", "pin_set", "theme",
                             "days_window", "my_repos", "search_topics")})
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
    port = int(_cfg("INTEL_PORT", "8097"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    srv.timeout = 30
    H.timeout = 30
    print(f"ai-intel-station on 0.0.0.0:{port} (ThreadingHTTPServer)")
    srv.serve_forever()
