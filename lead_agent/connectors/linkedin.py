"""LinkedIn PUBLIC pages only (logged out, no account, no cookies).

Reads the schema.org JSON-LD LinkedIn embeds in public post and company pages.
Logged-out pages show only a preview of comments (often 2-10 of N); full threads need login or a vendor.
Throttled hard (one request every 5 s) and cached, to stay polite and avoid blocks.
"""
import json
import re

from lead_agent.connectors.duckduckgo import _ddg
from lead_agent.core.utils import fetch, usable_name

BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/130.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def _get(url: str) -> str:
    st, html = fetch(url, gap=5, headers=BROWSER, max_age_days=3)
    return html if st == 200 and "authwall" not in html[:3000] else ""


def _ld(html: str) -> list[dict]:
    out = []
    for m in LD_RE.findall(html):
        try:
            d = json.loads(m)
        except ValueError:
            continue
        out += d.get("@graph", [d]) if isinstance(d, dict) else d
    return out


def _count(stats, kind: str) -> int:
    for s in stats if isinstance(stats, list) else [stats or {}]:
        if kind in str(s.get("interactionType", "")):
            return int(s.get("userInteractionCount", 0))
    return 0


def find_posts(keywords: str, n: int = 10) -> list[str]:
    """Public post URLs via search engine (no LinkedIn search, no login)."""
    urls = []
    for q in (f"site:linkedin.com/posts {keywords}", f"linkedin.com/posts {keywords}"):
        for h in _ddg(q, n * 2):
            u = re.sub(r"^https://\w+\.linkedin\.com", "https://www.linkedin.com", h.get("href", "").split("?")[0])
            if "linkedin.com/posts/" in u and re.search(r"activity-\d+|ugcPost-\d+", u) and u not in urls:
                urls.append(u)
        if len(urls) >= n:
            break
    return urls[:n]


def read_post(url: str) -> dict | None:
    html = _get(url)
    post = next((d for d in _ld(html) if d.get("@type") in ("DiscussionForumPosting", "SocialMediaPosting")), None)
    if not post:
        return None
    profiles = list(dict.fromkeys(re.findall(r'href="(https://www\.linkedin\.com/in/[^"?]+)', html)))

    def profile_for(name: str) -> str:
        parts = [p for p in re.findall(r"[a-z]+", name.lower()) if len(p) > 1]
        return next((p for p in profiles if parts and all(w in p.lower() for w in parts[:2])), "")

    a = post.get("author", {}) or {}
    comments = []
    for c in post.get("comment", []) or []:
        who = (c.get("author") or {}).get("name", "")
        comments.append({"post_url": url, "commenter": who, "commenter_profile": profile_for(who),
                         "date": (c.get("datePublished") or "")[:10], "comment": (c.get("text") or "").strip(),
                         "likes": _count(c.get("interactionStatistic"), "Like")})
    return {"post_url": url, "author": a.get("name", ""), "author_profile": a.get("url", ""),
            "date": (post.get("datePublished") or "")[:10],
            "content": (post.get("articleBody") or post.get("text") or "").strip(),
            "likes": _count(post.get("interactionStatistic"), "Like"),
            "comment_count": post.get("commentCount") or _count(post.get("interactionStatistic"), "Comment"),
            "comments_visible": len(comments), "comments": comments}


def read_company(url: str) -> dict:
    """Public company page: size, employees on LinkedIn, industry, HQ, recent posts."""
    html = _get(url)
    if not html:
        return {}
    items = _ld(html)
    org = next((d for d in items if d.get("@type") == "Organization"), {})
    size = re.search(r"([\d,]+(?:-[\d,]+)?\+?) employees", re.sub(r"<[^>]+>", " ", html))
    emp = (org.get("numberOfEmployees") or {}).get("value")
    addr = org.get("address") or {}
    industry = re.search(r'Industry</dt>\s*<dd[^>]*>\s*([^<]+)', html)
    posts = [{"date": (p.get("datePublished") or "")[:10], "content": (p.get("text") or p.get("articleBody") or "")[:300],
              "url": p.get("url", "")} for p in items if p.get("@type") == "SocialMediaPosting"]
    return {"company_size": size.group(1) if size else "", "linkedin_employees": emp or "",
            "linkedin_industry": industry.group(1).strip() if industry else "",
            "hq": ", ".join(x for x in [addr.get("addressLocality"), addr.get("addressRegion")] if x),
            "recent_posts": posts[:3]}


def lookup_person(name: str, profile: str = "", company: str = "") -> dict:
    """Commenter's headline via search-engine snippet. With a profile URL, only an exact profile match counts."""
    slug = profile.rstrip("/").split("/in/")[-1] if profile else ""
    hits = (_ddg(f'"{slug}"', 5) if slug else []) + _ddg(f'site:linkedin.com/in "{name}"', 8)
    for h in hits:
        href = h.get("href", "").split("?")[0].rstrip("/")
        if "linkedin.com/in/" not in href or (slug and not href.endswith("/in/" + slug)):
            continue
        parts = [p.strip() for p in re.split(r"\s[-–|]\s", h.get("title", "")) if p.strip() and "LinkedIn" not in p]
        if not slug and company and not _company_match(company, h.get("title", "") + " " + h.get("body", "")):
            continue  # same name, different person
        if parts and name.split()[0].lower() in parts[0].lower():
            head = " - ".join(parts[1:3])
            return {"profile": href, "headline": re.sub(r"\s*\.\.\..*$", "", head)}
    return {}


def _company_match(company: str, text: str) -> bool:
    stop = {"the", "of", "and", "pa", "pc", "llc", "pllc", "inc", "dds", "md", "group", "co", "law", "firm", "dental",
            "care", "center", "office", "practice", "associates"}
    tokens = [t for t in re.findall(r"[a-z0-9]{3,}", company.lower()) if t not in stop]
    low = text.lower()
    return bool(tokens) and sum(t in low for t in tokens) >= min(2, len(tokens))


def find_company(name: str, city: str = "") -> str:
    """Public LinkedIn company page URL via search engine; accepted only if the slug matches the name."""
    if not usable_name(name):
        return ""
    stop = {"the", "of", "and", "pa", "pc", "llc", "pllc", "inc", "dds", "md", "group", "co"}
    tokens = [t for t in re.findall(r"[a-z0-9]{3,}", name.lower()) if t not in stop]
    for h in _ddg(f'site:linkedin.com/company "{name}" {city}'.strip(), 5):
        m = re.search(r"linkedin\.com/company/([^/?#]+)", h.get("href", ""))
        if m and tokens and sum(t in m.group(1).lower() for t in tokens) >= min(2, len(tokens)):
            return f"https://www.linkedin.com/company/{m.group(1)}/"
    return ""
