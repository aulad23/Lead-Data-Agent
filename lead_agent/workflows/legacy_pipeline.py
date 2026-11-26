"""Legacy pipeline: Plan -> Discover -> Verify -> People -> Enrich -> QC -> Output (single-file CSV run).

Usage (US first):
  python -m lead_agent pipeline --industry dental --city Austin --state TX --limit 30
"""
import argparse
import csv
import datetime as dt
import re
import time

from lead_agent.config.settings import OUTPUT_DIR
from lead_agent.core.enrichment import crawl, extract, industry_score, guess_emails, email_status
from lead_agent.core.schema import DECISION_RX, FIELDS
from lead_agent.core.utils import US_STATES
from lead_agent.master.memory import AgentMemory
from lead_agent.sub_agents.company.site_finder import (GUESS_LABEL, find_site, industry_off, merge, pick_name,
                                                        same_domain, site_location, verify_site)
from lead_agent.sub_agents.discovery.brands.npi_registry import npi
from lead_agent.sub_agents.discovery.brands.openstreetmap import geocode_point, in_area, osm
from lead_agent.sub_agents.discovery.brands.web_search import find_linkedin_people, web_search


def process(c: dict, industry: str, use_linkedin_search: bool, mem: AgentMemory, where: dict | None = None) -> dict:
    """mem = the Company agent's private memory (email patterns, rejected words)."""
    notes, signals = [], []
    find_site(c, industry, where)
    drop_location = False
    score, info = 0.0, {"emails": [], "phones": [], "people": [], "linkedin_company": "", "tech": [], "hiring": False}
    if c.get("website"):
        site = crawl(c["website"])
        ok, why = (True, "") if c.get("site_accepted") or where is None else verify_site(c, c["domain"], site, industry, where)
        if not ok:  # a website of another business: do not read it, do not mail its domain
            notes.append(f"website rejected ({c['domain']}: {why})")
            drop_location = c["sources"] == {"ddg"} and "another location" in why
            c["website"], c["domain"], site = "", "", {"ok": False, "status": "rejected"}
        if site["ok"]:
            info = extract(site, c["domain"], mem.bad_words())
            info["emails"] = [e for e in info["emails"] if same_domain(e, c["domain"])]
            score = industry_score(site["text"], industry)
            if site_location(where, site["text"]) is False and in_area(
                    where["origin"], where["state"], c.get("city", ""), c.get("state", ""), c.get("zip", "")) is None:
                drop_location = True  # site shows only other cities and the company has no address of its own
            c["company"] = pick_name(c, site, industry)[0]
        elif site["status"] != "rejected":
            notes.append(f"site not reachable ({site['status']})")
    else:
        notes.append("no website found")
    raw_score = score
    if "npi" in c["sources"] or "osm" in c["sources"]:
        score = max(score, 0.8)  # category comes from the registry / map tag itself
    people = c.get("people", []) + info["people"]
    if use_linkedin_search and not any(DECISION_RX.search(p.get("title", "")) for p in people):
        people += find_linkedin_people(c["company"], c.get("city", ""))
    seen, uniq = set(), []
    for p in people:
        k = p["name"].lower().replace("dr. ", "")
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    m = re.search(r"(?:Office|Offices|Practice|Firm) of ((?:Dr\.\s)?[A-Z][a-z]+(?:\s[A-Z]\.)?\s[A-Z][a-zA-Z'-]+)", c["company"])
    if m and m.group(1).lower() not in seen:
        uniq.insert(0, {"name": m.group(1), "title": "Owner (from company name)", "source": "name"})
    dm = next((p for p in uniq if DECISION_RX.search(p.get("title", ""))), uniq[0] if uniq else None)
    contact_email, status = "", ""
    if dm and c.get("domain"):
        first = dm["name"].replace("Dr. ", "").split()[0].lower()
        on_site = [e for e in info["emails"] if e.startswith(first)]
        nm = dm["name"].replace("Dr. ", "").split()
        if on_site:
            contact_email, status = on_site[0], email_status(on_site[0], True)
            if len(nm) >= 2:
                mem.learn_email(on_site[0], nm[0], nm[-1])  # brain gets sharper: pattern reused next time
        elif len(nm) >= 2 and guess_emails(dm["name"], c["domain"]):
            contact_email, how = mem.email_guess(re.sub(r"\W", "", nm[0]), re.sub(r"\W", "", nm[-1]), c["domain"])
            status = email_status(contact_email, False)
            status = status if status.startswith("invalid") else GUESS_LABEL
    if info["hiring"]:
        signals.append("hiring")
    signals += info["tech"]
    priority = round(score * 40 + (25 if dm else 0) + (15 if info["emails"] else 0) + (10 if info["hiring"] else 0)
                     + (10 if c.get("phone") or info["phones"] else 0))
    needs_review = score < 0.5 or not dm or status.startswith("invalid")
    return {
        "company": c["company"], "industry_match": score, "website": c.get("website", ""),
        "phone": c.get("phone") or (info["phones"][0] if info["phones"] else ""),
        "address": c.get("address", ""), "city": c.get("city", ""), "state": c.get("state", ""),
        "zip": c.get("zip", ""), "country": "US",
        "company_email": info["emails"][0] if info["emails"] else "",
        "linkedin_company": info["linkedin_company"],
        "contact_name": dm["name"] if dm else "", "contact_title": dm.get("title", "") if dm else "",
        "contact_linkedin": dm.get("linkedin", "") if dm else "",
        "contact_email": contact_email, "email_status": status, "people_found": len(uniq),
        "signals": ", ".join(signals), "priority": priority, "sources": "+".join(sorted(c["sources"])),
        "needs_review": "yes" if needs_review else "", "notes": "; ".join(notes),
        "_raw_score": raw_score, "_drop_location": drop_location,  # internal, not written to the CSV
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Universal lead agent POC (US first)")
    ap.add_argument("--industry", required=True, help='e.g. "dental", "law", "hvac", "real estate"')
    ap.add_argument("--city", required=True)
    ap.add_argument("--state", required=True, help="2-letter US state, e.g. TX")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--linkedin-search", action="store_true",
                    help="Look up public LinkedIn profiles via search engine (no login)")
    a = ap.parse_args()
    state = a.state.upper()
    if state not in US_STATES:
        ap.error("state must be a 2-letter US state code")
    loc = f"{a.city}, {state}"
    t0 = time.time()

    print(f"[plan] industry={a.industry!r} location={loc!r} limit={a.limit}")
    found = []
    for name, fn in (("npi", lambda: npi(a.industry, a.city, state, a.limit * 2)),
                     ("osm", lambda: osm(a.industry, loc, a.limit * 2)),
                     ("web", lambda: web_search(a.industry, loc, a.limit * 2))):
        rows = fn()
        print(f"[discover] {name}: {len(rows)}")
        found += rows
    companies = merge(found)
    # Registry/map companies first (verified category), then with website, then multi-source
    companies.sort(key=lambda c: (c["sources"] == {"ddg"}, not c.get("website"), -len(c["sources"])))
    print(f"[discover] unique companies: {len(companies)}")
    where = {"origin": geocode_point(a.city, state), "state": state, "city": a.city}

    out, dropped_location, dropped_industry = [], 0, 0
    for i, c in enumerate(companies, 1):  # rows can be dropped, so go on until `limit` survive
        if len(out) >= a.limit:
            break
        if in_area(where["origin"], state, c.get("city", ""), c.get("state", ""), c.get("zip", "")) is False:
            dropped_location += 1
            continue
        r = process(c, a.industry, a.linkedin_search, AgentMemory("company"), where)
        if r.pop("_drop_location"):
            dropped_location += 1
            continue
        if industry_off(r, a.industry):
            dropped_industry += 1
            continue
        out.append(r)
        print(f"[{i}/{len(companies)}] {r['company'][:40]:40} match={r['industry_match']:.2f} "
              f"contact={r['contact_name'] or '-'} priority={r['priority']}")
    out.sort(key=lambda r: -r["priority"])

    OUTPUT_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"\W+", "-", f"{a.industry}-{a.city}-{state}".lower())
    path = OUTPUT_DIR / f"{slug}-{dt.date.today()}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)

    n = len(out) or 1
    print("\n=== POC metrics ===")
    print(f"companies           : {len(out)}")
    print(f"industry match>=0.5 : {sum(r['industry_match'] >= 0.5 for r in out) / n:.0%}")
    print(f"with decision-maker : {sum(bool(r['contact_name']) for r in out) / n:.0%}")
    print(f"with any contact    : {sum(bool(r['company_email'] or r['contact_email'] or r['phone']) for r in out) / n:.0%}")
    print(f"invalid emails      : {sum(r['email_status'].startswith('invalid') for r in out)}")
    print(f"dropped location    : {dropped_location}")
    print(f"dropped industry    : {dropped_industry}")
    print(f"needs human review  : {sum(r['needs_review'] == 'yes' for r in out)}")
    print(f"time                : {time.time() - t0:.0f}s")
    print(f"output              : {path}")


if __name__ == "__main__":
    main()
