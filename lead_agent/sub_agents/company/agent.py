"""CompanyAgent: one company: website -> verify -> people -> decision-maker -> email -> score."""
import re
from urllib.parse import urlparse

from lead_agent.connectors import apify, linkedin
from lead_agent.core.enrichment import (FIRST_NAMES, clean_title, crawl, email_status, extract, final_site, guess_emails,
                                        has_mx, industry_score, looks_like_directory)
from lead_agent.core.schema import DECISION_RX
from lead_agent.core.utils import domain_of, is_directory, name_matches_site, usable_name
from lead_agent.master.orchestrator import Step
from lead_agent.sub_agents.base import SubAgent, has_dm, log
from lead_agent.sub_agents.company.brands import llm_brain
from lead_agent.sub_agents.company.people_finder import find_decision_makers
from lead_agent.sub_agents.company.site_finder import (GUESS_LABEL, brand_name, find_site, pick_name, same_domain,
                                                       site_location, verify_site)
from lead_agent.sub_agents.discovery.brands.openstreetmap import in_area
from lead_agent.sub_agents.discovery.brands.web_search import find_linkedin_people

TRUSTED_SOURCES = {"npi", "jsonld", "name"}  # NPI authorized official, schema.org founder / employee, 'Office of Dr X'
GENERIC_BOX = ("hr@", "privacy@", "careers@", "jobs@", "career@", "webmaster@", "abuse@", "legal@", "noreply@", "no-reply@",
               "unsubscribe@", "billing@", "accounting@", "ap@")  # never the preferred company mailbox
OWNER_IN_NAME = re.compile(r"(?:Office|Offices|Practice|Firm) of ((?:Dr\.\s)?[A-Z][a-z]+(?:\s[A-Z]\.)?\s[A-Z][a-zA-Z'-]+)")


class CompanyAgent(SubAgent):
    """One company: website -> verify -> people -> decision-maker -> email -> score."""
    name = "company"

    def steps(self):
        def find_website(ctx):
            find_site(ctx["c"], ctx["industry"], ctx["where"])
            return bool(ctx["c"].get("website"))

        def crawl_site(ctx):
            c = ctx["c"]
            ctx["site"] = crawl(c["website"])
            if ctx["where"] is not None and not c.get("site_accepted"):  # discovery mode: is it really this business's site?
                ok, why = verify_site(c, c["domain"], ctx["site"], ctx["industry"], ctx["where"])
                if not ok:
                    ctx["notes"].append(f"website rejected ({c['domain']}: {why})")
                    ctx["drop_location"] = c["sources"] == {"ddg"} and "another location" in why  # search seed: site IS the company
                    c.setdefault("rejected", set()).add(c["domain"])
                    c["website"], c["domain"] = "", ""
                    ctx["site"] = {"ok": False, "status": "rejected"}
                    if c["sources"] != {"ddg"}:
                        find_site(c, ctx["industry"], ctx["where"])  # one more try: search for the real site
                        if c.get("website"):
                            ctx["site"] = crawl(c["website"])
            if not ctx["site"]["ok"] and ctx["site"]["status"] != "rejected":
                ctx["notes"].append(f"site not reachable ({ctx['site']['status']})")
            if ctx["site"]["ok"] and c.get("website"):
                check_site_identity(ctx)
            return ctx["site"]["ok"]

        def check_site_identity(ctx):
            """The site loaded. Is it still the same domain after redirects, and is it one business's own site (not a listing)?
            Emails are only ever built on the final, verified domain; a redirect to a directory blocks them."""
            c = ctx["c"]
            final = final_site(c["website"])
            fd = domain_of(final)
            if fd and fd != c["domain"] and not fd.endswith("." + c["domain"]) and not c["domain"].endswith("." + fd):
                if is_directory(fd):
                    ctx["email_block"] = f"website redirects to a directory ({fd})"
                else:
                    ctx["notes"].append(f"website redirects {c['domain']} -> {fd}: using {fd}")
                    pu = urlparse(final)
                    c["website"], c["domain"] = f"{pu.scheme}://{pu.netloc}", fd
            if looks_like_directory(ctx["site"]):
                ctx["email_block"] = "website looks like a directory / listing site"
                ctx["site_dir"] = True
            if ctx.get("email_block"):
                ctx["notes"].append(f"no email: {ctx['email_block']}")

        def name_from_site(ctx):
            """No (usable) company name: take it from the site (og:site_name / title) or, last resort, the domain."""
            c = ctx["c"]
            c["company"], ctx["name_how"] = brand_name(ctx.get("site"), c.get("domain", ""), ctx["industry"], c.get("city", ""))
            ctx["notes"].append(f"company name from {ctx['name_how'] or 'nowhere'}")
            return bool(c["company"])

        def read_site(ctx):
            c, site = ctx["c"], ctx["site"]
            ctx["info"] = extract(site, c["domain"], self.mem.bad_words())
            ctx["info"]["emails"] = [e for e in ctx["info"]["emails"] if same_domain(e, c["domain"])]  # only the site's own domain
            if ctx.get("site_dir"):  # a listing site names other businesses' people and mailboxes
                ctx["info"]["emails"], ctx["info"]["people"] = [], []
            ctx["score"] = industry_score(site["text"], ctx["industry"])
            ctx["site_loc"] = site_location(ctx["where"], site["text"])
            name, how = pick_name(c, site, ctx["industry"])  # never a page title / SEO phrase
            if how:
                c["company"], ctx["name_how"] = name, how
                ctx["notes"].append(f"company name from {how}")
            ctx["people"] += ctx["info"]["people"]
            return bool(ctx["info"]["people"] or ctx["info"]["emails"])

        def people_finder(ctx):
            """Free deep search for an owner / president: site sub-pages, JSON-LD, then web search (own module)."""
            c = ctx["c"]
            found = find_decision_makers(c["company"], c["website"], c.get("city", ""), c.get("state", ""))
            have = {p["name"].lower() for p in ctx["people"]}
            add = [p for p in found if p.get("name") and p["name"].lower() not in have]
            ctx["people"] += add
            ctx["info"]["people"] = list(ctx["info"].get("people", [])) + add
            ctx["pf_added"] = len([p for p in add if DECISION_RX.search(p.get("title", ""))])
            return bool(ctx["pf_added"])

        def name_rule(ctx):
            m = OWNER_IN_NAME.search(ctx["c"]["company"])
            if m:
                ctx["people"].insert(0, {"name": m.group(1), "title": "Owner (from company name)", "source": "name"})
            return bool(m)

        def linkedin_people(ctx):
            found = find_linkedin_people(ctx["c"]["company"], ctx["c"].get("city", ""))
            ctx["people"] += found
            return bool(found)

        def apify_people(ctx):
            """PAID fallback (only with --apify): free steps found no decision-maker -> LinkedIn employees via Apify."""
            url = ctx["info"].get("linkedin_company") or (name_ok(ctx) and linkedin.find_company(ctx["c"]["company"], ctx["c"].get("city", "")))
            if not url:
                ctx["notes"].append("apify: no LinkedIn company page")
                return False
            ctx["info"]["linkedin_company"] = ctx["info"].get("linkedin_company") or url
            found = apify.company_employees(url, ctx["apify_max"], log=lambda m: log("company", m))
            ctx["people"] = found + ctx["people"]
            return bool(found)

        def pick_dm(ctx):
            """A decision-maker needs a role (owner / president / CEO / partner / DDS ...) or a trusted source (NPI authorized
            official, schema.org, 'Office of Dr X'). A name without a role is kept in people_found but never chosen."""
            seen, uniq = set(), []
            for p in ctx["people"]:
                if p.get("source") not in TRUSTED_SOURCES and p.get("title"):
                    p["title"] = clean_title(p["title"], strict=False)  # 'Smartfab Solutions Jorge Amar' is not a role
                k = p["name"].lower().replace("dr. ", "")
                if k not in seen:
                    seen.add(k)
                    uniq.append(p)
            ctx["people"] = uniq
            ctx["dm"] = (next((p for p in uniq if DECISION_RX.search(p.get("title", ""))), None)
                         or next((p for p in uniq if p.get("source") in TRUSTED_SOURCES), None))
            return ctx["dm"] is not None

        def email_on_site(ctx):
            nm = ctx["dm"]["name"].replace("Dr. ", "").split()
            hit = [e for e in ctx["info"]["emails"] if e.startswith(nm[0].lower())]
            if hit:
                st = email_status(hit[0], True)
                if st.startswith("invalid"):
                    ctx["notes"].append(f"email {hit[0]} dropped: {st}")
                    return False
                ctx["email"], ctx["status"] = hit[0], st
                if len(nm) >= 2:
                    self.mem.learn_email(hit[0], nm[0], nm[-1])  # pattern learned -> reused for other people
            return bool(hit)

        def email_guess(ctx):
            """Pattern guess on the verified website domain only: site loaded, final domain after redirects, MX records exist."""
            nm = ctx["dm"]["name"].replace("Dr. ", "").split()
            dom = ctx["c"]["domain"]
            if len(nm) < 2 or not guess_emails(ctx["dm"]["name"], dom):
                return False
            if not has_mx(dom):
                ctx["notes"].append(f"no email guess: {dom} has no MX records")
                return False
            email, how = self.mem.email_guess(re.sub(r"\W", "", nm[0]), re.sub(r"\W", "", nm[-1]), dom)
            if not same_domain(email, dom):
                return False
            ctx["email"], ctx["status"] = email, GUESS_LABEL  # a guess is never "valid"
            return True

        def ai_verify(ctx):
            """LLM checks industry + decision-maker. Cached per domain in this agent's memory (saves free quota).
            Names the LLM says are not people become rejected words -> the rules get sharper too."""
            key = ctx["c"].get("domain") or ctx["c"]["company"]
            res = self.mem.recall("ai_verify", f"{ctx['industry']}|{key}")
            if res is None:
                res = llm_brain.verify_company(ctx["c"]["company"], ctx["industry"], ctx["site"]["text"], ctx["people"])
                if res is None:
                    return False
                self.mem.remember("ai_verify", f"{ctx['industry']}|{key}", res)
            try:
                ctx["score"] = round(float(res.get("industry_match", ctx["score"])), 2)
            except (TypeError, ValueError):
                pass
            for bad in res.get("not_people") or []:
                ctx["people"] = [p for p in ctx["people"] if p["name"].lower() != str(bad).lower()]
                for w in str(bad).lower().split():
                    if w.isalpha() and len(w) > 2 and w not in FIRST_NAMES:
                        self.mem.remember("bad_word", w, True)
            dm = res.get("decision_maker")
            if isinstance(dm, dict) and dm.get("name"):
                ctx["dm"] = {"name": dm["name"], "title": dm.get("title", ""), "seniority": dm.get("seniority", ""),
                             "source": "ai", "linkedin": next((p.get("linkedin", "") for p in ctx["people"]
                                                               if p["name"].lower() == dm["name"].lower()), "")}
            elif ctx["dm"] and ctx["dm"]["name"].lower() in {str(b).lower() for b in res.get("not_people") or []}:
                ctx["dm"] = None
            ctx["ai"] = res.get("industry_label", "")
            return True

        def name_ok(ctx):  # LinkedIn lookups need a real name: not empty, not generic, not just a guessed domain label
            ok = usable_name(ctx["c"]["company"]) and ctx.get("name_how") != "domain"
            if not ok and "LinkedIn lookup skipped (no reliable company name)" not in ctx["notes"]:
                ctx["notes"].append("LinkedIn lookup skipped (no reliable company name)")
            return ok

        site_ok = lambda ctx: bool(ctx.get("site", {}).get("ok"))
        def domain_ok(ctx):  # emails only on the company's own domain: not a directory, not a listing, tied to the company
            c = ctx["c"]
            if not c.get("domain") or is_directory(c["domain"]) or ctx.get("email_block"):
                return False
            if name_matches_site(c["company"], c["domain"], ctx.get("site", {}).get("title", "") + " " + ctx.get("site", {}).get("site_name", "")):
                return True
            if c.get("site_accepted"):  # accepted by phone / address only: the decision-maker's surname must be on the site
                last = (ctx["dm"]["name"].split() or [""])[-1].lower() if ctx.get("dm") else ""
                return len(last) > 2 and last in ctx.get("site", {}).get("text", "").lower()
            return False

        dm_dom = lambda ctx: bool(ctx.get("dm") and domain_ok(ctx))
        return [
            Step("find_website", find_website, when=lambda ctx: not ctx["c"].get("website")),
            Step("crawl_site", crawl_site, when=lambda ctx: bool(ctx["c"].get("website"))),
            Step("name_from_site", name_from_site, when=lambda ctx: not usable_name(ctx["c"]["company"])),
            Step("read_site", read_site, when=site_ok),
            Step("people_finder", people_finder, when=lambda ctx: not has_dm(ctx) and bool(ctx["c"].get("website")) and not ctx.get("site_dir")),
            Step("name_rule", name_rule, when=lambda ctx: not has_dm(ctx)),
            Step("linkedin_people", linkedin_people, when=lambda ctx: ctx["use_linkedin"] and not has_dm(ctx) and name_ok(ctx)),
            Step("apify_people", apify_people, when=lambda ctx: ctx.get("use_apify") and not has_dm(ctx)),
            Step("pick_dm", pick_dm),
            Step("ai_verify", ai_verify, when=lambda ctx: llm_brain.available() and site_ok(ctx)),
            Step("email_on_site", email_on_site, when=lambda ctx: dm_dom(ctx) and bool(ctx["info"]["emails"])),
            Step("email_guess", email_guess, when=lambda ctx: dm_dom(ctx) and site_ok(ctx) and not ctx["email"]),  # no guess on a site we could not read
        ]

    def run(self, c, industry, use_linkedin, use_apify=False, apify_max=10, where=None):
        empty = {"emails": [], "phones": [], "people": [], "linkedin_company": "", "tech": [], "hiring": False}
        bad_site = ""
        if c.get("website") and is_directory(c.get("domain") or c["website"]):  # a listing page is not the company's site
            bad_site, c["website"], c["domain"] = c["website"], "", ""
        ctx = self.orch.run({"c": c, "industry": industry, "use_linkedin": use_linkedin, "use_apify": use_apify,
                             "apify_max": apify_max, "name_how": "", "info": empty, "score": 0.0,
                             "people": list(c.get("people", [])), "notes": [], "dm": None, "email": "", "status": "",
                             "where": where, "site_loc": None, "drop_location": False},
                            scope=industry)
        if where is not None and not ctx["drop_location"] and ctx["site_loc"] is False:  # site shows only other cities
            known = in_area(where["origin"], where["state"], c.get("city", ""), c.get("state", ""), c.get("zip", ""))
            ctx["drop_location"] = known is None  # no address of its own to outweigh the site
        if bad_site:
            ctx["notes"].append(f"directory site ignored ({bad_site})")
        if not c.get("website"):
            ctx["notes"].append("no website found")
        return self._row(ctx)

    @staticmethod
    def _row(ctx):
        c, info, dm, score = ctx["c"], ctx["info"], ctx["dm"], ctx["score"]
        if "npi" in c["sources"] or "osm" in c["sources"]:
            score = max(score, 0.8)  # category comes from the registry / map tag itself
        dom = c.get("domain", "")
        own = [e for e in info["emails"] if same_domain(e, dom)]  # never mail another business's domain
        if ctx.get("email_block") or (own and not has_mx(dom)):
            own = []
        if len(own) < len(info["emails"]) and not ctx.get("email_block"):
            ctx["notes"].append("scraped email on another domain / without MX dropped")
        own.sort(key=lambda e: e.startswith(GENERIC_BOX))  # hr@ / privacy@ last
        info = {**info, "emails": own}
        if ctx["email"] and (ctx.get("email_block") or not same_domain(ctx["email"], dom)):
            ctx["email"], ctx["status"] = "", ""
        signals = (["hiring"] if info["hiring"] else []) + info["tech"]
        priority = round(score * 40 + (25 if dm else 0) + (15 if info["emails"] else 0) + (10 if info["hiring"] else 0)
                         + (10 if c.get("phone") or info["phones"] else 0))
        return {
            "company": c["company"], "industry_match": score, "website": c.get("website", ""),
            "phone": c.get("phone") or (info["phones"][0] if info["phones"] else ""),
            "address": c.get("address", ""), "city": c.get("city", ""), "state": c.get("state", ""),
            "zip": c.get("zip", ""), "country": "US",
            "company_email": info["emails"][0] if info["emails"] else "", "linkedin_company": info["linkedin_company"],
            "contact_name": dm["name"] if dm else "", "contact_title": dm.get("title", "") if dm else "",
            "seniority": dm.get("seniority", "") if dm else "", "ai_industry": ctx.get("ai", ""),
            "contact_linkedin": dm.get("linkedin", "") if dm else "",
            "contact_email": ctx["email"], "email_status": ctx["status"], "people_found": len(ctx["people"]),
            "signals": ", ".join(signals), "priority": priority, "sources": "+".join(sorted(c["sources"])),
            "needs_review": "yes" if score < 0.5 or not dm or ctx["status"].startswith("invalid") else "",
            "notes": "; ".join(ctx["notes"]), "agent_trace": " ".join(ctx["trace"]),
            "_raw_score": ctx["score"], "_drop_location": ctx["drop_location"],  # internal, not written to the CSV
        }
