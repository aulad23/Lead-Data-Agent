"""MainAgent: top orchestrator, decides which sub-agent to call and when. Only it holds every memory.

  MainAgent (orchestrator, holds every memory)
    DiscoveryAgent, CompanyAgent, CompanyProfileAgent, PostAgent, PeopleAgent, QCAgent
    each sub-agent = own private memory + own MiniOrchestrator (steps it plans, skips, reorders itself)
"""
import csv
import datetime as dt
import re

from lead_agent.config.settings import OUTPUT_DIR
from lead_agent.connectors import apify
from lead_agent.core.crm import CRM
from lead_agent.core.enrichment import crawl
from lead_agent.core.schema import FIELDS
from lead_agent.core.utils import usable_name
from lead_agent.master.memory import MemoryHub
from lead_agent.sub_agents.base import log, size_lower_bound
from lead_agent.sub_agents.company.agent import CompanyAgent
from lead_agent.sub_agents.company.brands import llm_brain
from lead_agent.sub_agents.company.site_finder import assess_location, industry_off
from lead_agent.sub_agents.company_profile.agent import CompanyProfileAgent
from lead_agent.sub_agents.discovery.agent import DiscoveryAgent
from lead_agent.sub_agents.discovery.brands.openstreetmap import geocode_point, in_area
from lead_agent.sub_agents.people.agent import PeopleAgent
from lead_agent.sub_agents.posts.agent import PostAgent
from lead_agent.sub_agents.qc.agent import QCAgent

OUT = OUTPUT_DIR


class MainAgent:
    """Top orchestrator: decides which sub-agent to call and when. Only it holds every memory."""
    AGENTS = ["discovery", "company", "company-profile", "posts", "people", "qc", "main"]

    def __init__(self):
        self.hub = MemoryHub(self.AGENTS)
        m = self.hub.of
        self.discovery, self.company = DiscoveryAgent(m("discovery")), CompanyAgent(m("company"))
        self.profile, self.posts = CompanyProfileAgent(m("company-profile")), PostAgent(m("posts"))
        self.people, self.qc = PeopleAgent(m("people")), QCAgent(m("qc"))
        self.mem = m("main")

    def companies(self, industry, city, state, limit, use_linkedin, crm_path=None, keep_crm=False,
                  use_apify=False, apify_max=10, max_employees=None):
        log("main", f"brain: {llm_brain.name()}; memory " + self.hub.summary())
        if use_apify:
            if apify.available():
                log("main", f"apify fallback ON (paid, only when free sources find no decision-maker); "
                            f"spent this month {apify.spent_this_month():.2f}/{apify.budget():.2f} USD")
            else:
                log("main", "apify fallback requested but APIFY_TOKEN is not set -> free sources only")
                use_apify = False
        crm = CRM(crm_path, self.mem) if crm_path else None
        if crm:
            log("main", f"CRM loaded: {len(crm)} companies (matched on {crm.fields})")
        companies, per = self.discovery.run(industry, city, state, limit)
        companies.sort(key=lambda c: (c["sources"] == {"ddg"}, not c.get("website"), not c.get("people"), -len(c["sources"])))
        if crm:  # main agent decision: do not spend time on companies we already have
            for c in companies:
                c["in_crm"] = crm.match(c["company"], c.get("website", ""), "", c.get("phone", ""))
            known = sum(bool(c["in_crm"]) for c in companies)
            if not keep_crm:
                companies = [c for c in companies if not c["in_crm"]]
            log("main", f"CRM: {known} already known -> {'kept, marked' if keep_crm else 'skipped'}")
        rows, dropped_size, dropped_location, dropped_industry, sites_rejected = [], 0, 0, 0, 0
        location_unverified = 0
        where = {"origin": geocode_point(city, state), "state": state, "city": city}
        if not where["origin"]:
            log("main", f"could not geocode {city}, {state}: location check limited to the state")
        if max_employees is not None and not use_linkedin:
            use_linkedin = True
            log("main", "--max-employees needs the company size -> LinkedIn profile lookup turned ON")
        # with a size filter, rows are dropped after enrichment, so keep going through candidates until `limit` survive
        pool = companies  # rows can be dropped (location / industry / size / not a company), so keep going until `limit` survive
        for i, c in enumerate(pool, 1):
            if len(rows) >= limit:
                break
            if in_area(where["origin"], state, c.get("city", ""), c.get("state", ""), c.get("zip", "")) is False:
                dropped_location += 1  # its own address is another state / country / far from the requested city
                log("main", f"{i}/{len(pool)} DROPPED (location {c.get('city', '')} {c.get('state', '')}): {c['company'][:40]}")
                continue
            r = self.company.run(c, industry, use_linkedin, use_apify, apify_max, where)
            sites_rejected += "website rejected" in r["notes"]
            if not r["company"].strip():
                continue  # nothing usable (no website, no name)
            if r["_drop_location"]:
                dropped_location += 1
                log("main", f"{i}/{len(pool)} DROPPED (website shows another location): {r['company'][:40]}")
                continue
            site = crawl(r["website"]) if r.get("website") else None  # HTTP cache: the company agent just fetched it
            verdict, why = assess_location(r, where, site)  # discovery address + website addresses / JSON-LD + phone area codes
            if verdict is False:
                dropped_location += 1  # other state / Canada / outside the metro radius (site_finder.METRO_RADIUS_KM)
                log("main", f"{i}/{len(pool)} DROPPED (location: {why}): {r['company'][:40]} {r['website']}")
                continue
            if industry_off(r, industry, (site or {}).get("text", "") if site and site.get("ok") else ""):
                dropped_industry += 1
                log("main", f"{i}/{len(pool)} DROPPED (not {industry}, match {r['_raw_score']}): {r['company'][:40]} {r['website']}")
                continue
            if verdict is None:  # no address anywhere: keep, but say so (never drop silently)
                location_unverified += 1
                r["notes"] = "; ".join(x for x in [r["notes"], "location unverified"] if x)
            if crm:  # re-check after the website/email were found
                r["in_crm"] = c.get("in_crm") or crm.match(r["company"], r["website"], r["company_email"], r["phone"])
            if use_linkedin:  # main agent decides: profile agent only in LinkedIn mode
                reliable = usable_name(r["company"]) and "company name from domain" not in r["notes"]
                url, info = self.profile.run(r["company"] if reliable else "", r["city"], r["linkedin_company"])
                r["linkedin_company"] = url
                r["company_size"], r["linkedin_employees"] = info.get("company_size", ""), info.get("linkedin_employees", "")
                r["latest_post"] = (info.get("recent_posts") or [{}])[0].get("date", "")
            if max_employees is not None:
                low = size_lower_bound(r.get("company_size")) or size_lower_bound(r.get("linkedin_employees"))
                if low is None:
                    r["notes"] = "; ".join(x for x in [r["notes"], "size unknown"] if x)
                elif low > max_employees:
                    dropped_size += 1
                    log("main", f"{i}/{len(pool)} DROPPED (size {r.get('company_size') or r.get('linkedin_employees')} "
                                f"> {max_employees}): {r['company'][:40]}")
                    continue
            rows.append(r)
            log("main", f"{i}/{len(pool)} {r['company'][:34]:34} dm={r['contact_name'] or '-':22} "
                        f"size={r.get('company_size') or r.get('linkedin_employees') or '?':9} {r['agent_trace']}")
        if len(rows) < limit:
            log("main", f"only {len(rows)} of {limit} rows: all {len(pool)} candidates processed (try a bigger --limit pool or another source)")
        self.discovery.record(industry, per, rows)
        rows.sort(key=lambda r: -r["priority"])
        n = len(rows) or 1
        metrics = {"companies": len(rows), "with_dm": round(sum(bool(r["contact_name"]) for r in rows) / n, 2),
                   "with_contact": round(sum(bool(r["company_email"] or r["contact_email"] or r["phone"]) for r in rows) / n, 2),
                   "with_size": sum(bool(r.get("company_size")) for r in rows),
                   "needs_review": sum(r["needs_review"] == "yes" for r in rows),
                   "with_website": sum(bool(r["website"]) for r in rows),
                   "with_contact_email": sum(bool(r["contact_email"]) for r in rows)}
        if max_employees is not None:
            metrics["dropped_size"] = dropped_size
            metrics["size_unknown"] = sum("size unknown" in r["notes"] for r in rows)
        metrics["dropped_location"] = dropped_location
        metrics["location_unverified"] = location_unverified
        metrics["dropped_industry"] = dropped_industry
        metrics["websites_rejected"] = sites_rejected
        metrics["candidates"] = {k: len(v) for k, v in per.items()}
        metrics["source_errors"] = dict(getattr(self.discovery, "source_errors", {}) or {})
        if use_apify:
            metrics["apify_used"] = sum("apify_people:" in r["agent_trace"] for r in rows)
            metrics["apify_usd_month"] = apify.spent_this_month()
        self.mem.log("run", {"mode": "companies", "query": f"{industry}|{city}|{state}", **metrics})
        path = self._write(f"{industry}-{city}-{state}", rows,
                           FIELDS[:2] + ["ai_industry", "in_crm"] + FIELDS[2:13] + ["seniority"] + FIELDS[13:]
                           + ["company_size", "linkedin_employees", "latest_post", "agent_trace"])
        log("main", f"done {metrics} -> {path}")

    def posts_mode(self, keywords, limit, lookup):
        posts = self.posts.run(keywords, limit)
        leads = []
        for p in posts:
            for c in p["comments"]:
                if lookup and c["commenter"]:  # main agent calls People agent only when asked
                    info = self.people.run(c["commenter"], c["commenter_profile"])
                    c["commenter_profile"] = c["commenter_profile"] or info.get("profile", "")
                    c["headline"] = info.get("headline", "")
                c["post_author"] = p["author"]
                leads.append(self.qc.run(c))
        leads.sort(key=lambda c: -c["lead_score"])
        slug = f"posts-{keywords}"
        p1 = self._write(slug, [{k: v for k, v in p.items() if k != "comments"} for p in posts],
                         ["post_url", "author", "author_profile", "date", "content", "likes", "comment_count", "comments_visible"])
        p2 = self._write(slug + "-comments", leads, ["lead_score", "commenter", "commenter_profile", "headline",
                         "is_decision_maker", "date", "comment", "likes", "post_author", "post_url"])
        self.mem.log("run", {"mode": "posts", "query": keywords, "posts": len(posts), "comments": len(leads)})
        log("main", f"{len(posts)} posts, {len(leads)} commenters -> {p1}, {p2}")

    def learn(self, path):
        """Human review -> the right sub-agent's memory (only the main agent can do this)."""
        bad = good = 0
        for r in csv.DictReader(open(path, encoding="utf-8-sig")):
            v, name = (r.get("verdict") or "").strip().lower(), r.get("contact_name", "")
            if v == "wrong" and name:
                for w in name.lower().split():
                    if w.isalpha() and len(w) > 2:
                        self.hub.of("company").remember("bad_word", w, True)
                bad += 1
            elif v == "right" and name:
                self.hub.of("people").remember("person", r.get("contact_linkedin") or name,
                                               {"profile": r.get("contact_linkedin", ""), "headline": r.get("contact_title", "")})
                good += 1
        log("main", f"learned {bad} wrong and {good} confirmed contacts")

    def show_memory(self):
        for a, m in self.hub.stores.items():
            print(f"\n== {a} == {m.stats()}")
            for k, v in sorted(m.all_of("step").items()):
                print(f"   step {k:40} tried {v['tries']:3}  useful {v['useful']:3}")

    @staticmethod
    def _write(slug, rows, fields):
        OUT.mkdir(exist_ok=True)
        path = OUT / (re.sub(r"\W+", "-", slug.lower()).strip("-") + f"-{dt.date.today()}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        return path
