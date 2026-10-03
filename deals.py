#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""羊毛资讯采集：国内外主流云厂商优惠/免费额度/VPS 特价
源：挖站否/主机测评/老蒋部落/老蒋IT部落格/小众软件/腾讯云服务器网(博客)
   + GitHub free-for-dev 与 free-llm-api-resources 的 commit 流(免费额度追踪)
   + lowendbox(海外 VPS 优惠)
管道：先抓进内存、成功才写库（防清零）；URL 白名单；标题去重增量入库
"""
import json, os, re, sqlite3, sys, time
from urllib.request import Request, urlopen

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intel.db")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "*/*"}

def now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

def fetch_text(url, timeout=20):
    try:
        req = Request(url, headers=UA)
        with urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[deals] fetch {url} err: {e}")
        return ""

def safe_href(u):
    """URL 白名单：仅 http/https 允许进 href，防 javascript:/data: 注入"""
    u = (u or "").strip()
    return u if re.match(r"^https?://", u, re.I) else "#"

def parse_rss_items(xml, limit=15):
    """宽松解析 RSS/Atom：标题 + 链接（与 cn_news 同一套逻辑）"""
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        block = m.group(1)
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
        l = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", block, re.S)
        if t:
            title = re.sub(r"<[^>]+>", "", t.group(1)).strip()
            link = (l.group(1).strip() if l else "")
            items.append({"title": title, "url": link})
        if len(items) >= limit: break
    if len(items) < 3:
        for m in re.finditer(r"<entry>(.*?)</entry>", xml, re.S):
            block = m.group(1)
            t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
            l = re.search(r'<link[^>]*href="([^"]+)"', block)
            if t:
                title = re.sub(r"<[^>]+>", "", t.group(1)).strip()
                link = (l.group(1).strip() if l else "")
                items.append({"title": title, "url": link})
            if len(items) >= limit: break
    return items

def _clean_gh_commit_title(t):
    """GitHub commit atom 标题形如 "Update README.md" 无信息量，用仓库语义标题替代"""
    return t

# (src_id, 厂商标签, 展示名, feed地址, 每源条数)
SOURCES = [
    ("wzfou",     "idc",   "挖站否",   "https://www.wzfou.com/feed/", 12),
    ("zjpj",      "idc",   "主机测评",  "https://zhujipingjia.com/feed/", 12),
    ("laozuo",    "idc",   "老蒋部落", "https://www.laozuo.org/feed/", 12),
    ("itbulu",    "idc",   "老蒋IT",  "https://www.itbulu.com/feed/", 10),
    ("appinn",    "free",  "小众软件", "https://www.appinn.com/feed/", 8),
    ("txyfwq",    "tencent", "腾讯云活动", "https://txyfwq.com/feed/", 12),
    ("lowendbox", "overseas", "LowEndBox", "https://lowendbox.com/feed/", 10),
    ("freefordev", "free", "免费资源追踪", "https://github.com/ripienaar/free-for-dev/commits/master.atom", 15),
    ("freellm",   "ailm",  "免费LLM额度", "https://github.com/mnfst/awesome-free-llm-apis/commits/main.atom", 15),
]

# 厂商标签规则：从标题猜（用于前端过滤 chip）
VENDOR_RULES = [
    ("aliyun",  ["阿里云", "aliyun", "Aliyun", "99计划", "百炼", "万小智"]),
    ("tencent", ["腾讯云", "轻量服务器", "TokenHub", "混元"]),
    ("huawei",  ["华为云", "ModelArts", "昇腾"]),
    ("volc",    ["火山", "方舟", "豆包", " doubao", "Seedream"]),
    ("baidu",   ["百度", "千帆", "文心"]),
    ("ailm",    ["免费LLM", "免费大模型", "LLM API", "tokens", "大模型API", "free llm", "免费额度"]),
    ("oracle",  ["甲骨文", "Oracle", "ARM"]),
    ("aws",     ["AWS", "亚马逊", "Amazon"]),
    ("azure",   ["Azure", "微软云"]),
    ("gcp",     ["Google Cloud", "GCP", "谷歌云"]),
]

def guess_vendor(title):
    for vid, kws in VENDOR_RULES:
        for k in kws:
            if k in title:
                return vid
    return ""

def collect_deals():
    results = []  # 全部抓完进内存再写库
    for src_id, vendor, name, url, limit in SOURCES:
        if not url:
            continue
        xml = fetch_text(url)
        if not xml:
            continue
        items = parse_rss_items(xml, limit)
        for it in items:
            if not it["title"]:
                continue
            results.append((src_id, vendor, it["title"][:200], safe_href(it["url"]), now()))
    if not results:
        print("[deals] no sources reachable, db untouched")
        return 0
    c = sqlite3.connect(DB, timeout=20)
    c.execute("PRAGMA busy_timeout=20000")
    total_new = 0
    for src_id, vendor, title, url, ts in results:
        dup = c.execute("SELECT 1 FROM deals WHERE title=? AND src=?", (title, src_id)).fetchone()
        if dup:
            continue
        v = vendor or guess_vendor(title)
        c.execute("INSERT INTO deals(src,vendor,title,url,fetched_at) VALUES(?,?,?,?,?)",
                  (src_id, v, title, url, ts))
        total_new += 1
    c.commit()
    # 每源保留最新 30 条，总量可控
    for src_id, _, _, _, _ in SOURCES:
        c.execute("""DELETE FROM deals WHERE src=? AND id NOT IN (
                     SELECT id FROM deals WHERE src=? ORDER BY id DESC LIMIT 30)""",
                  (src_id, src_id))
    c.commit()
    cnt = c.execute("SELECT src, COUNT(*) FROM deals GROUP BY src").fetchall()
    c.close()
    print(f"[deals] +{total_new} new, totals: {cnt}")
    return total_new

def ensure_table():
    c = sqlite3.connect(DB, timeout=20)
    c.execute("""CREATE TABLE IF NOT EXISTS deals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        src TEXT, vendor TEXT, title TEXT, url TEXT,
        score INTEGER DEFAULT 0, fetched_at TEXT)""")
    c.commit()
    c.close()

if __name__ == "__main__":
    ensure_table()
    collect_deals()
