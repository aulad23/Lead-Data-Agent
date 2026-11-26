"""Apify fallback, WITHOUT login:
  - company_employees(): decision-makers of a company   (actor harvestapi/linkedin-company-employees)
  - profile_posts():     recent posts of profiles       (actor harvestapi/linkedin-profile-posts, $0.002 / post)

Paid (free plan: $5 credit / month), so it is a LAST RESORT:
  - only runs when the user passes --apify
  - only for a company where the free steps found no decision-maker
  - only decision-makers (seniority Director / VP / CXO / Owner-Partner, or Experienced Manager), max N per company
  - every company is cached (data/apify_cache.json) -> never paid twice; profile posts cached 3 days
  - monthly budget cap (APIFY_MONTHLY_USD, default 4.50) -> stops before the free credit runs out
Token: APIFY_TOKEN (setx APIFY_TOKEN "apify_api_..."; read from the saved Windows setting, no restart needed).
"""
import datetime as dt
import json
import os
import time

import requests

from lead_agent.config.settings import DATA_DIR
from lead_agent.core.enrichment import clean_title

ACTOR = "harvestapi~linkedin-company-employees"
POSTS_ACTOR = "harvestapi~linkedin-profile-posts"
POST_USD, POSTS_START_USD = 0.002, 0.0001          # free-plan prices, Nov 2026
POSTS_CACHE_DAYS = 3
API = "https://api.apify.com/v2"
START_USD, PER_PROFILE_USD = 0.02, 0.003          # free-plan prices, Nov 2026 (Short profile mode)
DM_SENIORITY = ["210", "220", "300", "310", "320"]  # Experienced Manager, Director, VP, CXO, Owner / Partner
STATE = DATA_DIR / "apify_cache.json"


def _load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"spend": {}, "companies": {}}


def _charge(cost: float) -> None:
    s, month = _load(), dt.date.today().strftime("%Y-%m")
    s["spend"][month] = round(s["spend"].get(month, 0.0) + float(cost), 4)
    _save(s)


def _left() -> float:
    return budget() - spent_this_month()


def _run(actor: str, body: dict, cap: float, max_items: int) -> tuple[dict, list]:
    """Start an actor, wait for it, return (run, dataset items). Raises requests / Key / Value errors."""
    h = {"Authorization": f"Bearer {os.environ['APIFY_TOKEN']}"}
    r = requests.post(f"{API}/acts/{actor}/runs", headers=h, json=body, timeout=60,
                      params={"maxTotalChargeUsd": cap, "maxItems": max_items})
    r.raise_for_status()
    run = r.json()["data"]
    t0 = time.time()
    while run["status"] in ("READY", "RUNNING") and time.time() - t0 < 900:
        run = requests.get(f"{API}/actor-runs/{run['id']}", headers=h, params={"waitForFinish": 60},
                           timeout=90).json()["data"]
    items = requests.get(f"{API}/datasets/{run['defaultDatasetId']}/items", headers=h,
                         params={"clean": "true"}, timeout=60).json()
    return run, items if isinstance(items, list) else []


def _save(s: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1, ensure_ascii=False), encoding="utf-8")


def available() -> bool:
    return bool(os.environ.get("APIFY_TOKEN"))


def budget() -> float:
    return float(os.environ.get("APIFY_MONTHLY_USD", "4.50"))


def spent_this_month() -> float:
    return round(_load()["spend"].get(dt.date.today().strftime("%Y-%m"), 0.0), 4)


def _key(url: str) -> str:
    return url.lower().split("?")[0].rstrip("/")


def cached(company_url: str) -> list[dict] | None:
    return _load()["companies"].get(_key(company_url), {}).get("people")


def _person(it: dict) -> dict | None:
    name = (it.get("name") or f"{it.get('firstName', '')} {it.get('lastName', '')}").strip()
    if not name:
        return None
    pos = (it.get("currentPosition") or it.get("currentPositions") or [{}])
    pos = pos[0] if isinstance(pos, list) and pos else (pos if isinstance(pos, dict) else {})
    loc = it.get("location")
    raw_title = it.get("position") or it.get("headline") or pos.get("position") or pos.get("title") or ""
    return {"name": name,
            "title": clean_title(raw_title, strict=False) or raw_title,
            "linkedin": (it.get("linkedinUrl") or it.get("profileUrl") or it.get("url") or "").split("?")[0],
            "location": (loc.get("linkedinText") or loc.get("text") or "") if isinstance(loc, dict) else (loc or ""),
            "source": "apify"}


def company_employees(company_url: str, max_people: int = 10, log=print) -> list[dict]:
    """Decision-makers of one LinkedIn company page. Returns [] (and spends nothing) when not allowed."""
    if not available() or "linkedin.com/company/" not in company_url:
        return []
    key, s = _key(company_url), _load()
    if key in s["companies"]:
        return s["companies"][key]["people"]
    month = dt.date.today().strftime("%Y-%m")
    cap = max(0.05, round(START_USD + PER_PROFILE_USD * max_people + 0.01, 3))  # Apify minimum per run: $0.05
    left = budget() - s["spend"].get(month, 0.0)
    if left < cap:
        log(f"[apify] budget reached ({s['spend'].get(month, 0.0):.2f}/{budget():.2f} USD this month) -> skipped")
        return []
    body = {"companies": [key], "profileScraperMode": "Short ($4 per 1k)", "seniorityLevelIds": DM_SENIORITY,
            "maxItems": max_people, "maxItemsPerCompany": max_people}
    try:
        run, items = _run(ACTOR, body, cap, max_people)
    except (requests.RequestException, KeyError, ValueError) as e:
        log(f"[apify] failed: {e}")
        return []
    # Apify often reports 0 right after the run ends -> never count less than the price-list estimate
    cost = round(max(float(run.get("usageTotalUsd") or 0), START_USD + PER_PROFILE_USD * len(items)), 4)
    people = [p for p in (_person(it) for it in items if isinstance(it, dict)) if p]
    s = _load()  # re-read: another run may have written meanwhile
    s["spend"][month] = round(s["spend"].get(month, 0.0) + float(cost), 4)
    s["companies"][key] = {"date": dt.date.today().isoformat(), "status": run["status"], "cost": cost, "people": people}
    _save(s)
    log(f"[apify] {key.split('/company/')[1]}: {len(people)} decision-makers, ${float(cost):.3f} "
        f"(month {s['spend'][month]:.2f}/{budget():.2f})")
    return people


def credit_left() -> float | None:
    """USD left on the Apify account of the CURRENT token (real numbers from Apify, so a new key starts fresh).
    None when Apify does not answer."""
    try:
        d = requests.get(f"{API}/users/me/limits", headers={"Authorization": f"Bearer {os.environ['APIFY_TOKEN']}"},
                         timeout=30).json()["data"]
        return round(float(d["limits"]["maxMonthlyUsageUsd"]) - float(d["current"]["monthlyUsageUsd"]), 4)
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return None


def _slug(url: str) -> str:
    """Cache key of a profile ("name") or company page ("company:name")."""
    u = (url or "").lower().split("?")[0].rstrip("/")
    if "/in/" in u:
        return u.split("/in/")[1].split("/")[0]
    if "/company/" in u:
        return "company:" + u.split("/company/")[1].split("/")[0]
    return ""


def _target(k: str) -> str:
    return f"https://www.linkedin.com/company/{k[8:]}/" if k.startswith("company:") else f"https://www.linkedin.com/in/{k}/"


def _post(it: dict) -> dict:
    eng = it.get("engagement") or {}
    at = it.get("postedAt") or {}
    return {"type": "post", "date": (at.get("date") if isinstance(at, dict) else str(at or ""))[:10],
            "likes": int(eng.get("likes") or 0), "comments": int(eng.get("comments") or 0),
            "shares": int(eng.get("shares") or 0), "text": (it.get("content") or "").strip(),
            "url": (it.get("linkedinUrl") or "").split("?")[0]}


def profile_posts(profile_urls: list[str], since: dt.date, max_posts: int = 10, log=print,
                  chunk: int = 25) -> dict[str, list[dict]]:
    """Own posts (no reposts) of LinkedIn profiles / company pages published on/after `since`, newest first,
    max `max_posts` each. Runs Apify in chunks of `chunk` profiles; every chunk is cached right away
    (POSTS_CACHE_DAYS), so a stop (credit finished, error) loses nothing and a re-run continues where it stopped.
    Returns {cache key: [posts]}; profiles not done (credit / error) are missing from the result."""
    if not available():
        return {}
    s, today = _load(), dt.date.today()
    cache, out, todo = s.setdefault("profiles", {}), {}, []
    for u in profile_urls:
        k = _slug(u)
        c = cache.get(k)
        if c and (today - dt.date.fromisoformat(c["date"])).days < POSTS_CACHE_DAYS and c.get("since", "") <= str(since):
            out[k] = [x for x in c["posts"] if x["date"] >= str(since)]
        elif k and k not in todo:
            todo.append(k)
    if out:
        log(f"[apify] {len(out)} profiles from cache (no cost)")
    for i in range(0, len(todo), chunk):
        part = todo[i:i + chunk]
        n = len(part) * max_posts
        cap = max(0.05, round(POSTS_START_USD + POST_USD * n + 0.01, 3))
        left = credit_left()
        if left is None:
            left = budget() - spent_this_month()
        if left < cap:  # not enough for the whole chunk -> as many profiles as the credit allows
            part = part[:int(max(0.0, left - 0.02 - POSTS_START_USD) / (POST_USD * max_posts))]
            if not part:
                log(f"[apify] CREDIT FINISHED (${left:.2f} left on this key): {len(todo) - i} profiles not done. "
                    f"Set a new APIFY_TOKEN and run again - finished profiles are cached and not paid twice.")
                break
            n = len(part) * max_posts
            cap = max(0.05, round(POSTS_START_USD + POST_USD * n + 0.01, 3))
        body = {"targetUrls": [_target(k) for k in part], "maxPosts": max_posts, "postedLimitDate": str(since),
                "includeReposts": False, "includeQuotePosts": True, "scrapeReactions": False, "scrapeComments": False}
        log(f"[apify] chunk {i // chunk + 1}/{-(-len(todo) // chunk)}: {len(part)} profiles, cap ${cap:.2f}, "
            f"credit left ${left:.2f}")
        try:
            run, items = _run(POSTS_ACTOR, body, cap, n)
        except (requests.RequestException, KeyError, ValueError) as e:
            log(f"[apify] failed: {e} -> stopped; run again to continue")
            break
        got = {k: [] for k in part}
        for it in items:
            if not isinstance(it, dict) or it.get("type", "post") != "post":
                continue
            k = _slug((it.get("query") or {}).get("targetUrl", ""))
            if k not in got:
                a = it.get("author") or {}
                k = ("company:" + (a.get("universalName") or "").lower()) if a.get("type") == "company"                     else (a.get("publicIdentifier") or "").lower()
            if k in got:
                got[k].append(_post(it))
        posts_n = sum(len(v) for v in got.values())
        cost = round(max(float(run.get("usageTotalUsd") or 0), POSTS_START_USD + POST_USD * posts_n), 4)
        _charge(cost)
        ok = run.get("status") == "SUCCEEDED"
        s = _load()
        for k, v in got.items():
            v.sort(key=lambda x: x["date"], reverse=True)
            out[k] = [x for x in v if x["date"] >= str(since)]
            if v or ok:  # an empty result is only trusted from a run that finished normally
                s.setdefault("profiles", {})[k] = {"date": today.isoformat(), "since": str(since), "posts": v}
            elif not v:
                out.pop(k)
        _save(s)
        log(f"[apify] {run.get('status')}: {posts_n} posts from {sum(bool(v) for v in got.values())}/{len(part)} "
            f"profiles, ${cost:.3f}")
        if not ok:
            log("[apify] run did not finish normally (credit?) -> stopped; run again to continue")
            break
    return out
