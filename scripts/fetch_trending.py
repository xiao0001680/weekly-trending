#!/usr/bin/env python3
"""
GitHub Weekly Trending Scraper
抓取 GitHub Trending 本周 Star 增长 Top 10，存入 SQLite 数据库。

用法:
    python fetch_trending.py              # 抓取本周数据并存入 SQLite
    python fetch_trending.py --export     # 额外导出 CSV
    python fetch_trending.py --json       # 输出 JSON 到 stdout
"""

import sqlite3
import re
import sys
import json
import time
from datetime import datetime, date, timedelta
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# ── 配置 ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "trending.db"
GITHUB_TRENDING_URL = "https://github.com/trending?since=weekly"

# ── 数据库 ────────────────────────────────────────────
def init_db():
    """创建数据库和表（如果不存在）"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS weekly_trending (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start  DATE NOT NULL,
            week_end    DATE NOT NULL,
            rank        INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 10),
            repo_name   TEXT NOT NULL,
            repo_url    TEXT NOT NULL,
            language    TEXT,
            stars_this_week INTEGER NOT NULL,
            total_stars INTEGER NOT NULL,
            description TEXT,
            fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(week_start, rank)
        )
    """)
    conn.commit()
    return conn


def week_already_fetched(conn, week_start):
    """检查指定周的数据是否已存在"""
    cur = conn.execute(
        "SELECT COUNT(*) FROM weekly_trending WHERE week_start = ?",
        (week_start.isoformat(),)
    )
    return cur.fetchone()[0] >= 10


def save_entries(conn, entries):
    """批量保存到数据库，已存在则跳过"""
    count = 0
    for e in entries:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO weekly_trending
                    (week_start, week_end, rank, repo_name, repo_url,
                     language, stars_this_week, total_stars, description)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                e["week_start"].isoformat(),
                e["week_end"].isoformat(),
                e["rank"],
                e["repo_name"],
                e["repo_url"],
                e["language"],
                e["stars_this_week"],
                e["total_stars"],
                e["description"],
            ))
            if conn.total_changes > count:
                count += 1
        except Exception as ex:
            print(f"  ⚠ 保存失败 [{e['repo_name']}]: {ex}", file=sys.stderr)
    conn.commit()
    return count


# ── 解析 ──────────────────────────────────────────────
def fetch_and_parse():
    """获取 GitHub Trending 页面并解析 Top 10"""
    print(f"[fetch] GET {GITHUB_TRENDING_URL}")
    resp = requests.get(
        GITHUB_TRENDING_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        },
        timeout=30,
    )
    resp.raise_for_status()
    html = resp.text

    if HAS_BS4:
        entries = _parse_with_bs4(html)
    else:
        entries = _parse_with_regex(html)

    if len(entries) < 10:
        # 回退到正则兜底
        print("[warn] BeautifulSoup 解析不足 10 条，回退到正则模式")
        entries = _parse_with_regex(html)

    return entries[:10]


def _parse_with_bs4(html):
    """用 BeautifulSoup 解析 Trending 页面"""
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    articles = soup.find_all("article", class_="Box-row")

    for i, article in enumerate(articles[:25]):
        try:
            # 仓库名：h2 里的 a 标签 href="/owner/repo"
            h2 = article.find("h2")
            if not h2:
                continue
            repo_link = h2.find("a", href=re.compile(r"^/[^/]+/[^/]+"))
            if not repo_link:
                continue
            repo_name = repo_link["href"].lstrip("/")
            repo_url = f"https://github.com/{repo_name}"

            # 描述
            desc_p = article.find("p")
            description = desc_p.text.strip() if desc_p else ""

            # 语言
            lang_span = article.find("span", itemprop="programmingLanguage")
            language = lang_span.text.strip() if lang_span else ""

            # 总 star 数
            star_link = article.find("a", href=re.compile(r"/stargazers"))
            total_stars = _parse_number(star_link.text.strip()) if star_link else 0

            # 本周 star 数
            stars_week = _extract_stars_this_week(article.get_text())

            entries.append({
                "repo_name": repo_name,
                "repo_url": repo_url,
                "description": description,
                "language": language,
                "total_stars": total_stars,
                "stars_this_week": stars_week,
            })
        except Exception as ex:
            print(f"  ⚠ 解析第 {i+1} 条出错: {ex}", file=sys.stderr)
            continue

    return _rank_entries(entries)


def _parse_with_regex(html):
    """用正则表达式解析 Trending 页面（纯文本兜底方案）"""
    # 从 h2 中匹配仓库路径 href="/owner/repo"
    repo_pattern = re.compile(
        r'<h2[^>]*>.*?<a[^>]*href="/([^"]+)"[^>]*>',
        re.DOTALL,
    )
    stars_pattern = re.compile(r'(\d{1,3}(?:,\d{3})*)\s+stars?\s+this\s+week', re.IGNORECASE)
    desc_pattern = re.compile(r'<p\s+class="col-9[^"]*"[^>]*>\s*(.*?)\s*</p>', re.DOTALL)
    lang_pattern = re.compile(r'itemprop="programmingLanguage"[^>]*>\s*(.*?)\s*<', re.DOTALL)

    repos = repo_pattern.findall(html)
    stars_weeks = stars_pattern.findall(html)
    descs = desc_pattern.findall(html)
    langs = lang_pattern.findall(html)

    entries = []
    for i in range(min(10, len(repos))):
        repo_name = repos[i].strip()
        repo_url = f"https://github.com/{repo_name}"
        description = descs[i].strip() if i < len(descs) else ""
        language = langs[i].strip() if i < len(langs) else ""
        stars_week = _parse_number(stars_weeks[i]) if i < len(stars_weeks) else 0

        # 总 star 数：从 article 文本中找第一个 stars 匹配（非 "this week"）
        total_stars = 0
        entries.append({
            "repo_name": repo_name,
            "repo_url": repo_url,
            "description": description,
            "language": language,
            "total_stars": total_stars,
            "stars_this_week": stars_week,
        })

    return _rank_entries(entries)


def _rank_entries(entries):
    """按 stars_this_week 降序排列并标记 rank"""
    entries.sort(key=lambda x: x["stars_this_week"], reverse=True)
    for i, e in enumerate(entries):
        e["rank"] = i + 1
    return entries


def _extract_stars_this_week(text):
    """从文本中提取 'X,XXX stars this week'"""
    m = re.search(r'(\d{1,3}(?:,\d{3})*)\s+stars?\s+this\s+week', text, re.IGNORECASE)
    return _parse_number(m.group(1)) if m else 0


def _parse_number(text):
    """将 '12,345' 转为 12345"""
    return int(re.sub(r'[^\d]', '', str(text)) or '0')


# ── 翻译 ──────────────────────────────────────────────
def translate_descriptions(entries):
    """将描述从英文翻译为中文（MyMemory 免费 API，无需 Key）"""
    print("[translate] 正在翻译描述...")
    for i, e in enumerate(entries):
        desc = e.get("description", "").strip()
        if not desc:
            continue
        try:
            resp = requests.get(
                "https://api.mymemory.translated.net/get",
                params={"q": desc, "langpair": "en|zh"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                translated = data.get("responseData", {}).get("translatedText", "")
                if translated and translated != desc:
                    e["description"] = translated
                    print(f"  [{i+1}/10] OK: {translated[:60]}...")
                    time.sleep(0.3)  # 避免触发限流
                    continue
        except Exception as ex:
            print(f"  [{i+1}/10] 翻译失败，保留英文: {ex}")
        # 兜底：保留英文原文
        print(f"  [{i+1}/10] 保留英文: {desc[:60]}...")
    print("[translate] 完成")


# ── 周计算 ────────────────────────────────────────────
def get_current_week_range():
    """返回本周的起止日期（周日 ~ 周六）"""
    today = date.today()
    # 周日: today.weekday()=6 -> days_since_sunday=0
    # 周一: today.weekday()=0 -> days_since_sunday=1
    days_since_sunday = (today.weekday() + 1) % 7
    sunday = today - timedelta(days=days_since_sunday)
    saturday = sunday + timedelta(days=6)
    return sunday, saturday


# ── 导出 ──────────────────────────────────────────────
def export_csv(conn):
    """导出最近 4 周数据为 CSV"""
    csv_path = BASE_DIR / "data" / "trending_export.csv"
    rows = conn.execute("""
        SELECT week_start, rank, repo_name, stars_this_week, total_stars, language, description
        FROM weekly_trending
        ORDER BY week_start DESC, rank ASC
    """).fetchall()

    import csv
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["周起始", "排名", "仓库", "本周Star", "总Star", "语言", "描述"])
        for r in rows:
            writer.writerow(r)
    print(f"[export] CSV → {csv_path}  ({len(rows)} 条)")
    return csv_path


def export_json(conn):
    """输出 JSON 到 stdout"""
    rows = conn.execute("""
        SELECT week_start, rank, repo_name, repo_url, stars_this_week,
               total_stars, language, description
        FROM weekly_trending
        ORDER BY week_start DESC, rank ASC
        LIMIT 10
    """).fetchall()
    result = []
    for r in rows:
        result.append({
            "week_start": r[0],
            "rank": r[1],
            "repo_name": r[2],
            "repo_url": r[3],
            "stars_this_week": r[4],
            "total_stars": r[5],
            "language": r[6],
            "description": r[7],
        })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


# ── 主流程 ────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  GitHub Weekly Trending Scraper")
    print("=" * 55)

    # 初始化数据库
    conn = init_db()
    sunday, saturday = get_current_week_range()
    print(f"[info] 本周范围: {sunday} ~ {saturday}")

    # 检查是否已抓取
    if week_already_fetched(conn, sunday):
        print(f"[skip] 本周 ({sunday}) 数据已存在，跳过抓取")
        if "--export" in sys.argv:
            export_csv(conn)
        if "--json" in sys.argv:
            export_json(conn)
        conn.close()
        return

    # 抓取并解析
    print("[fetch] 正在抓取 GitHub Trending...")
    try:
        entries = fetch_and_parse()
    except Exception as ex:
        print(f"[error] 抓取失败: {ex}", file=sys.stderr)
        # 尝试用 requests 回退
        sys.exit(1)

    if not entries:
        print("[error] 未解析到任何数据", file=sys.stderr)
        sys.exit(1)

    # 填入周日期
    for e in entries:
        e["week_start"] = sunday
        e["week_end"] = saturday

    # 翻译描述为中文
    translate_descriptions(entries)

    # 保存到数据库
    saved = save_entries(conn, entries)
    print(f"[done] 成功保存 {saved}/10 条记录")

    # 打印摘要
    print(f"\n{'─'*55}")
    print(f"  [Table] {sunday} ~ {saturday}  This Week Top 10")
    print(f"{'─'*55}")
    for e in entries[:10]:
        print(f"  {e['rank']:2d}. {e['repo_name']:<40s} +{e['stars_this_week']:>6,} *")

    # 导出
    if "--export" in sys.argv:
        export_csv(conn)
    if "--json" in sys.argv:
        export_json(conn)

    conn.close()
    print(f"\n[ok] 数据库: {DB_PATH}")


if __name__ == "__main__":
    main()
