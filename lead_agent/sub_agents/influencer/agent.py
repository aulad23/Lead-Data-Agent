"""Influencer post tracker (FREE, logged out, no account).

Reads an Excel/CSV with LinkedIn profile URLs (any column holding linkedin.com/in/ links), opens each PUBLIC
profile page, takes the posts/articles LinkedIn shows logged out (usually the latest ~8 posts + newsletter
articles), then opens each post page for its comment count.

  python -m lead_agent influencer "inbox/crm/Influencer List.xlsx"            # all rows
  python -m lead_agent influencer file.xlsx --limit 3 --no-post-pages          # quick: likes only, no comment counts
  python -m lead_agent influencer file.xlsx --days 30                         # only posts of the last 30 days
  python -m lead_agent influencer file.xlsx --days 0                          # every post found, any age
  python -m lead_agent influencer file.xlsx --apify                           # + Apify for profiles the free pass misses

--apify: a profile with fewer than --apify-min (3) recent posts from the free pass gets its posts from Apify
(harvestapi/linkedin-profile-posts, no login, $0.002 per post, free plan $5/month; monthly cap APIFY_MONTHLY_USD
4.50, results cached 3 days -> never paid twice). ~25 profiles x 10 posts = ~$0.50.

Only RECENT posts are kept: the last 60 days by default (--days). Older posts and old newsletter articles are dropped.

Output: output/influencer-posts-<date>.xlsx
  sheet posts    : one row per post (influencer, type, date, likes, comments, text, url)
  sheet summary  : one row per influencer (recent posts kept, older ones dropped, last post date, status)
Notes:
  - Dates of feed posts are approximate: LinkedIn shows "1d / 2w / 3mo" to logged-out visitors.
  - Requests: one every 5 s, cached 3 days (data/cache.sqlite), so re-runs are fast and polite.
"""
import argparse
import datetime as dt
import re
import time
from pathlib import Path

import pandas as pd

from lead_agent.config.settings import OUTPUT_DIR
from lead_agent.connectors import apify, linkedin
from lead_agent.connectors.duckduckgo import _ddg

OUT = OUTPUT_DIR
MAX_TEXT = 32000  # Excel cell limit is 32767 characters
PROFILE_RX = re.compile(r"https?://(?:\w+\.)?linkedin\.com/in/([^/?#\s]+)", re.I)


def _slug(url: str) -> str:
    m = PROFILE_RX.search(url or "")
    return m.group(1).lower() if m else ""


def _likes(d: dict) -> int:
    return linkedin._count(d.get("interactionStatistic"), "Like")


def profile_posts(url: str, since: dt.date | None = None) -> tuple[list[dict], str]:
    """Posts + articles written by this profile, from the public profile page."""
    html = linkedin._get(url)
    if not html:  # profile page not shown logged out -> public post URLs from the search engine
        return search_posts(url, since=since)
    me, posts = _slug(url), []
    for d in linkedin._ld(html):
        t = d.get("@type")
        if t not in ("DiscussionForumPosting", "SocialMediaPosting", "Article"):
            continue
        author = d.get("author") if isinstance(d.get("author"), dict) else {}
        if author.get("url") and _slug(author["url"]) != me:
            continue  # "related posts" by other people
        link = d.get("url") or d.get("mainEntityOfPage") or ""
        posts.append({"type": "article" if t == "Article" else "post", "date": (d.get("datePublished") or "")[:10],
                      "likes": _likes(d), "comments": d.get("commentCount"),
                      "text": (d.get("headline") or d.get("text") or d.get("articleBody") or "").strip()[:MAX_TEXT],
                      "url": link.split("?")[0]})
    return posts, "ok" if posts else "no public posts"


def _post_time(u: str) -> int:
    """LinkedIn activity ids hold the post time, so sorting by id puts the newest posts first."""
    m = re.search(r"(?:activity|ugcPost|share)-(\d{15,})", u)
    return int(m.group(1)) if m else 0


def _id_date(u: str) -> dt.date | None:
    """Post date read from the activity id itself (the top bits are the creation time in ms), no request needed."""
    t = _post_time(u)
    return dt.datetime.fromtimestamp((t >> 22) / 1000, dt.timezone.utc).date() if t else None


def search_posts(url: str, n: int = 10, since: dt.date | None = None) -> tuple[list[dict], str]:
    """Public posts of this person found via DuckDuckGo, then each public post page (date, likes, comment count).
    Same throttle and cache as every LinkedIn request. Search returns old and new posts mixed, so posts older
    than `since` are skipped by their id date before any post page is opened."""
    me, urls = _slug(url), []
    for q in (f"site:linkedin.com/posts/{me}", f"linkedin.com/posts {me}"):
        for h in _ddg(q, 20):
            u = re.sub(r"^https://\w+\.linkedin\.com", "https://www.linkedin.com", h.get("href", "").split("?")[0])
            if f"/posts/{me}_" in u.lower() and u not in urls:
                urls.append(u)
    found = len(urls)
    if since:
        urls = [u for u in urls if (_id_date(u) or since) >= since]
        if found and not urls:
            return [], f"no posts since {since}"
    posts = []
    for u in sorted(urls, key=_post_time, reverse=True)[:n]:
        full = linkedin.read_post(u)
        if full:
            posts.append({"type": "post", "date": full["date"], "likes": full["likes"], "comments": full["comment_count"],
                          "text": full["content"].strip()[:MAX_TEXT], "url": u})
    if posts:
        return posts, "ok (via search)"
    return [], "not public" if not urls else "posts not public"


def _recent(posts: list[dict], since: dt.date | None) -> list[dict]:
    """Posts dated on/after `since` (all posts when since is None). Undated ones are dropped: age unknown."""
    if not since:
        return posts
    return [x for x in posts if x["date"] and dt.date.fromisoformat(x["date"][:10]) >= since]


def run(path: str, limit: int | None = None, post_pages: bool = True, days: int = 60,
        use_apify: bool = False, apify_min: int = 3, max_posts: int = 10) -> Path:
    t0 = time.time()
    src = Path(path)
    sheets = pd.read_excel(src, sheet_name=None, dtype=str) if src.suffix.lower() != ".csv" else {"csv": pd.read_csv(src, dtype=str)}
    people = []
    for sheet, df in sheets.items():
        df = df.fillna("")
        cols = [c for c in df.columns if df[c].str.contains(r"linkedin\.com/in/", case=False).any()]
        if not cols:
            continue
        name_col = next((c for c in df.columns if str(c).strip().lower() in ("name", "full name", "influencer")), None)
        foll_col = next((c for c in df.columns if "linkedin" in str(c).lower() and "follow" in str(c).lower()), None)
        for _, r in df.iterrows():
            m = PROFILE_RX.search(str(r[cols[0]]))
            if m:
                people.append({"sheet": sheet, "name": r[name_col] if name_col else m.group(1),
                               "profile": f"https://www.linkedin.com/in/{m.group(1)}/",
                               "followers": r[foll_col] if foll_col else ""})
    seen, uniq = set(), []
    for p in people:
        if p["profile"] not in seen:
            seen.add(p["profile"])
            uniq.append(p)
    people = uniq[:limit] if limit else uniq
    rows, summary, today = [], [], dt.date.today()
    since = today - dt.timedelta(days=days) if days else None
    print(f"[influencer] {len(people)} profiles from {src.name}; "
          + (f"keeping posts since {since} (last {days} days)" if since else "keeping posts of any age"))

    res = []  # free pass: public profile page or search engine
    for i, p in enumerate(people, 1):
        posts, status = profile_posts(p["profile"], since)
        if post_pages:
            for x in posts:
                if x["type"] == "post" and x["url"] and not (since and (_id_date(x["url"]) or since) < since):
                    full = linkedin.read_post(x["url"])
                    if full:
                        x["comments"] = full["comment_count"]
                        x["likes"] = full["likes"] or x["likes"]
                        x["date"] = full["date"] or x["date"]
                        x["text"] = full["content"].strip()[:MAX_TEXT] or x["text"]
        dates = sorted((x["date"] for x in posts if x["date"]), reverse=True)  # all posts, for "last active"
        found = len(posts)
        posts = _recent(posts, since)
        if found and not posts and status.startswith("ok"):
            status = f"no posts in {days}d"
        via = "search" if "search" in status else "profile"
        for x in posts:
            x["via"] = via
        res.append({"p": p, "posts": posts, "status": status, "dates": dates, "dropped": found - len(posts)})
        print(f"[influencer] {i}/{len(people)} {p['name'][:24]:24} {status:26} recent={len(posts):2} "
              f"last={dates[0] if dates else '-':10}", flush=True)

    # paid fallback (Apify free credit) only for profiles the free pass could not cover
    weak = [r for r in res if len(r["posts"]) < apify_min] if use_apify else []
    if weak and not apify.available():
        print("[influencer] --apify skipped: APIFY_TOKEN not set")
    elif weak:
        got = apify.profile_posts([r["p"]["profile"] for r in weak], since or today - dt.timedelta(days=365),
                                  max_posts, log=print)
        for r in weak:
            ap = got.get(apify._slug(r["p"]["profile"]))
            if ap is None:
                continue  # skipped (budget / error) -> keep the free result
            if len(ap) >= len(r["posts"]):
                r["posts"] = [{**x, "text": x["text"][:MAX_TEXT], "via": "apify"} for x in ap]
                r["status"] = "ok (apify)" if ap else f"no posts in {days}d (apify)"
                r["dates"] = sorted({*r["dates"], *(x["date"] for x in ap)}, reverse=True)

    rows, summary = [], []
    for r in res:
        p, posts, status, dates = r["p"], r["posts"], r["status"], r["dates"]
        try:
            foll = float(str(p["followers"]).replace(",", ""))
        except ValueError:
            foll = 0
        for n, x in enumerate(sorted(posts, key=lambda x: x["date"] or "", reverse=True), 1):
            com = int(x["comments"]) if str(x["comments"]).isdigit() else None
            eng = x["likes"] + (com or 0)
            rows.append({"influencer": p["name"], "post_no": n, "date": x["date"], "type": x["type"], "content": x["text"],
                         "likes": x["likes"], "comments": com if com is not None else "", "shares": x.get("shares", ""),
                         "total_engagement": eng, "followers": p["followers"],
                         "engagement_rate_%": round(eng / foll * 100, 3) if foll else "",
                         "post_url": x["url"], "found_via": x["via"], "profile": p["profile"]})
        feed = [x for x in posts if x["type"] == "post"]
        recent = [x for x in posts if x["date"] and (today - dt.date.fromisoformat(x["date"][:10])).days <= 30]
        summary.append({"influencer": p["name"], "profile": p["profile"], "followers": p["followers"], "status": status,
                        "posts_found": len(feed), "articles_found": len(posts) - len(feed),
                        "older_dropped": r["dropped"],
                        "last_post": dates[0] if dates else "", "posts_last_30d": len(recent)})

    OUT.mkdir(exist_ok=True)
    out = OUT / f"influencer-posts-{today}.xlsx"
    from openpyxl.styles import Alignment, Font
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        pd.DataFrame(rows).to_excel(xw, sheet_name="posts", index=False)  # one row per post, newest first per person
        pd.DataFrame(summary).to_excel(xw, sheet_name="summary", index=False)
        for ws in xw.sheets.values():
            ws.freeze_panes = "B2"
            for c in ws[1]:
                c.font = Font(bold=True)
            for col in ws.columns:
                head = str(col[0].value)
                ws.column_dimensions[col[0].column_letter].width = 70 if head == "content" else \
                    min(45, max(9, max(len(str(c.value or "")) for c in col[:60]) + 2))
                if head == "content":
                    for c in col[1:]:
                        c.alignment = Alignment(wrap_text=True, vertical="top")
    ok = sum(s["status"].startswith("ok") for s in summary)
    print(f"[influencer] done: {ok}/{len(summary)} profiles readable, {len(rows)} posts -> {out}  ({time.time() - t0:.0f}s)")
    return out


def main():
    ap = argparse.ArgumentParser(description="Free logged-out tracker: posts, dates, likes, comments of LinkedIn profiles")
    ap.add_argument("file")
    ap.add_argument("--limit", type=int, help="only the first N profiles")
    ap.add_argument("--no-post-pages", action="store_true", help="skip opening each post (faster, no comment counts)")
    ap.add_argument("--days", type=int, default=60, help="keep only posts of the last N days (default 60; 0 = any age)")
    ap.add_argument("--apify", action="store_true",
                    help="profiles with too few recent posts -> Apify (free $5/month credit, $0.002 per post, capped)")
    ap.add_argument("--apify-min", type=int, default=3, help="use Apify when the free pass found fewer posts (default 3)")
    ap.add_argument("--max-posts", type=int, default=10, help="max posts per profile from Apify (default 10)")
    a = ap.parse_args()
    run(a.file, a.limit, not a.no_post_pages, a.days, a.apify, a.apify_min, a.max_posts)


if __name__ == "__main__":
    main()
