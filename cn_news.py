#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中文新闻源抓取 + LLM 中文翻译
新闻源：默认 4 个中文科技 RSS（IT之家/Solidot/少数派/InfoQ），环境变量 INTEL_RSS_SOURCES 可整表替换
翻译：火山 Ark GLM（OpenAI 兼容协议），VOLC_ARK_API_KEY/VOLC_ARK_EP_ID 环境变量配置
"""
import json, os, re, sqlite3, subprocess, sys, time
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intel.db")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "*/*"}

def now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

def fetch_xml(url, timeout=20):
    try:
        req = Request(url, headers=UA)
        with urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[cnnews] fetch {url} err: {e}")
        return ""

def safe_href(u):
    """URL 白名单：仅 http/https 允许进 href，防 javascript:/data: 注入（RSS 源被劫持时）"""
    u = (u or "").strip()
    return u if re.match(r"^https?://", u, re.I) else "#"

def parse_rss_items(xml, limit=15):
    """宽松解析 RSS/Atom：标题 + 链接（不依赖严格 XML 结构）"""
    items = []
    # RSS <item>
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        block = m.group(1)
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
        l = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", block, re.S)
        if t:
            title = re.sub(r"<[^>]+>", "", t.group(1)).strip()
            link = (l.group(1).strip() if l else "")
            items.append({"title": title, "url": link})
        if len(items) >= limit: break
    # Atom <entry>（RSS 解析不足时补充）
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

SOURCES = [
    ("ithome", "IT之家", "https://www.ithome.com/rss/", 18),
    ("solidot", "Solidot", "https://www.solidot.org/index.rss", 15),
    ("sspai", "少数派", "https://sspai.com/feed", 12),
    ("infoq", "InfoQ", "https://www.infoq.cn/feed", 15),
]
# 可通过环境变量覆盖：INTEL_RSS_SOURCES="id1|名称1|url1|条数1,id2|名称2|url2|条数2"
if os.environ.get("INTEL_RSS_SOURCES"):
    SOURCES = []
    for spec in os.environ["INTEL_RSS_SOURCES"].split(","):
        p = spec.split("|")
        if len(p) == 4:
            SOURCES.append((p[0].strip(), p[1].strip(), p[2].strip(), int(p[3])))
KEEP_PER_SOURCE = int(os.environ.get("INTEL_RSS_KEEP", "25"))

def collect_cn():
    c = sqlite3.connect(DB, timeout=20)
    c.execute("PRAGMA busy_timeout=20000")
    total = 0
    for src_id, src_name, url, limit in SOURCES:
        xml = fetch_xml(url)
        if not xml:
            continue
        items = parse_rss_items(xml, limit)
        # 已存过的标题跳过（增量）
        for it in items:
            if not it["title"]:
                continue
            dup = c.execute("SELECT 1 FROM news WHERE title=? AND src=?", (it["title"], src_id)).fetchone()
            if dup:
                continue
            c.execute("INSERT INTO news(src,title,url,score,fetched_at) VALUES(?,?,?,?,?)",
                      (src_id, it["title"], safe_href(it["url"]), 0, now()))
            total += 1
    c.commit()
    # 保持 news 总量可控：中文源各留最新 N 条（INTEL_RSS_KEEP，默认25），hn 留 top 20
    for src_id, _, _, _ in SOURCES:
        c.execute("""DELETE FROM news WHERE src=? AND id NOT IN (
                     SELECT id FROM news WHERE src=? ORDER BY id DESC LIMIT ?)""", (src_id, src_id, KEEP_PER_SOURCE))
    c.commit()
    cnt = c.execute("SELECT src, COUNT(*) FROM news GROUP BY src").fetchall()
    c.close()
    print(f"[cnnews] +{total} new, totals: {cnt}")
    return total

# ---------- LLM 翻译 ----------
def _llm_cfg():
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".volc_key")
    if os.path.exists(cache):
        try:
            return json.loads(open(cache).read())
        except Exception:
            pass
    import pr_review
    cfg = pr_review.get_llm_key()
    try:
        return json.loads(cfg)
    except Exception:
        return {}

def llm_translate(texts, batch=15):
    """批量翻译：一次请求翻 N 条，返回中文数组。失败返回 None。"""
    cfg = _llm_cfg()
    if not cfg.get("key"):
        print("[translate] no LLM key")
        return None
    payload = [{"i": i, "t": t[:300]} for i, t in enumerate(texts)]
    body = json.dumps({
        "model": cfg["ep"],
        "messages": [
            {"role": "system", "content": "你是技术新闻翻译。把 JSON 数组里的英文标题/描述翻译成简体中文：保留专有名词（产品名/库名/公司名）英文，技术术语用中文社区通用译法。只输出 JSON 数组 [{\"i\":序号,\"zh\":\"译文\"}]，不要其他内容。"},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
        ],
        "max_tokens": 3000, "temperature": 0.2
    }).encode()
    try:
        req = Request("https://ark.cn-beijing.volces.com/api/v3/chat/completions", data=body,
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['key']}"})
        with urlopen(req, timeout=180) as r:
            d = json.loads(r.read().decode())
        raw = d["choices"][0]["message"]["content"]
        m = re.search(r"\[.*\]", raw, re.S)
        if not m:
            return None
        arr = json.loads(m.group(0))
        out = {}
        for x in arr:
            out[int(x["i"])] = x["zh"]
        return out
    except Exception as e:
        msg = str(e)
        print(f"[translate] err: {msg}")
        if "429" in msg:
            time.sleep(20)  # 限流退避，由外层重试
        return None

def translate_pending():
    """翻译 repos 描述（英文）+ hn 新闻标题 → 存 zh 列"""
    BATCH = 15
    c = sqlite3.connect(DB, timeout=20)
    c.execute("PRAGMA busy_timeout=20000")
    c.execute("ALTER TABLE repos ADD COLUMN desc_zh TEXT") if not _col_exists(c, "repos", "desc_zh") else None
    c.execute("ALTER TABLE news ADD COLUMN title_zh TEXT") if not _col_exists(c, "news", "title_zh") else None
    # 待翻译：英文 repos（desc 非空且 desc_zh 空）——429 时重试最多3轮
    for attempt in range(3):
        rows = c.execute("SELECT id, desc FROM repos WHERE desc != '' AND desc_zh IS NULL").fetchall()
        if not rows:
            break
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i+BATCH]
            res = llm_translate([r[1] for r in chunk])
            if res:
                for j, (rid, _) in enumerate(chunk):
                    zh = res.get(j)
                    if zh:
                        c.execute("UPDATE repos SET desc_zh=? WHERE id=?", (zh, rid))
                        c.commit()
            time.sleep(3)
        if c.execute("SELECT COUNT(*) FROM repos WHERE desc != '' AND desc_zh IS NULL").fetchone()[0] == 0:
            break
        time.sleep(10)
    # 待翻译：hn 新闻标题（cn 源是中文不需要）
    for attempt in range(3):
        rows = c.execute("SELECT id, title FROM news WHERE src='hn' AND title_zh IS NULL").fetchall()
        if not rows:
            break
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i+BATCH]
            res = llm_translate([r[1] for r in chunk])
            if res:
                for j, (rid, _) in enumerate(chunk):
                    zh = res.get(j)
                    if zh:
                        c.execute("UPDATE news SET title_zh=? WHERE id=?", (zh, rid))
                        c.commit()
            time.sleep(3)
        if c.execute("SELECT COUNT(*) FROM news WHERE src='hn' AND title_zh IS NULL").fetchone()[0] == 0:
            break
        time.sleep(10)
    c.commit()
    done_r = c.execute("SELECT COUNT(*) FROM repos WHERE desc_zh IS NOT NULL").fetchone()[0]
    done_n = c.execute("SELECT COUNT(*) FROM news WHERE title_zh IS NOT NULL").fetchone()[0]
    c.close()
    print(f"[translate] repos_zh={done_r} news_zh={done_n}")

def _col_exists(c, table, col):
    return col in [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]

if __name__ == "__main__":
    if "--translate" in sys.argv:
        translate_pending()
    else:
        collect_cn()
        translate_pending()
