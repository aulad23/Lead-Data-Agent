"""Client file intake: read any Excel/CSV, understand its columns, decide per row which agents to call.

  python -m lead_agent excel client.xlsx [--industry dental] [--linkedin] [--map "Practice=company"]

1. FieldMapper detects what each column is, from the header name AND from the values
   (emails, URLs, phones, US states, LinkedIn links...). Mappings it learns go to the main agent's memory.
2. For every row the main agent looks at what is filled and what is missing, and writes a plan, e.g.
   "website missing -> find_website; contact missing -> find decision-maker; email missing -> email pattern".
3. Client data is never overwritten: only empty cells are filled, new info goes into new 'agent_*' columns.
4. Output: <name>-enriched.xlsx with sheets 'enriched' and 'field_report'.
"""
import argparse
import re
import time
from pathlib import Path

import pandas as pd

from lead_agent.config.settings import OUTPUT_DIR
from lead_agent.connectors import linkedin
from lead_agent.core.enrichment import FIRST_NAMES, email_status
from lead_agent.core.utils import US_STATES, domain_of, us_phone
from lead_agent.master.main_agent import MainAgent
from lead_agent.sub_agents.base import log

FIELDS = {  # canonical field -> header keywords (lower-case, matched as whole words or substrings)
    "company": ["company", "business", "organization", "organisation", "practice", "firm", "account", "employer", "clinic"],
    "website": ["website", "web site", "url", "domain", "site", "web"],
    "first_name": ["first name", "firstname", "first", "fname", "given name"],
    "last_name": ["last name", "lastname", "surname", "lname", "family name"],
    "full_name": ["full name", "contact name", "contact", "owner", "person", "name", "decision maker", "dm"],
    "title": ["title", "job title", "position", "role", "designation", "headline"],
    "email": ["email", "e-mail", "mail"],
    "phone": ["phone", "telephone", "tel", "mobile", "cell", "contact number"],
    "address": ["address", "street", "address line"],
    "city": ["city", "town", "locality"],
    "state": ["state", "st", "province", "region"],
    "zip": ["zip", "postal", "postcode", "zip code"],
    "linkedin_person": ["linkedin profile", "person linkedin", "profile url", "linkedin url", "linkedin"],
    "linkedin_company": ["company linkedin", "linkedin company", "company page"],
    "post_url": ["post url", "post link", "linkedin post", "post"],
    "industry": ["industry", "category", "vertical", "sector", "niche", "type"],
    "company_size": ["size", "employees", "headcount", "staff"],
}
VALUE_TESTS = {
    "email": lambda v: bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", v, re.I)),
    "post_url": lambda v: "linkedin.com/posts/" in v or "linkedin.com/feed/update" in v,
    "linkedin_person": lambda v: "linkedin.com/in/" in v,
    "linkedin_company": lambda v: "linkedin.com/company/" in v,
    "website": lambda v: bool(re.fullmatch(r"(https?://)?(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?", v, re.I)) and "linkedin" not in v,
    "phone": lambda v: bool(us_phone(v)),
    "state": lambda v: v.strip().upper() in US_STATES,
    "zip": lambda v: bool(re.fullmatch(r"\d{5}(-\d{4})?", v.strip())),
    "company": lambda v: bool(re.search(r"(llc|pllc|inc|corp|co|ltd|lp|llp|pa|pc|law|dental|dentistry|clinic|firm|group|"
                                        r"associates|services|center|partners|solutions|studio|agency)", v, re.I)),
    "full_name": lambda v: (len(v.split()) in (2, 3) and v.replace("Dr. ", "").split()[0].lower() in FIRST_NAMES
                            and not re.search(r"(llc|inc|law|dental|firm|group)", v, re.I)),
}
FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "live.com", "msn.com"}


class FieldMapper:
    """Header + value based column detection, with memory of past decisions (main agent's memory)."""

    def __init__(self, mem, overrides: dict[str, str]):
        self.mem, self.overrides = mem, overrides

    def detect(self, df: pd.DataFrame) -> dict[str, dict]:
        report, used = {}, set()
        for col in df.columns:
            h = str(col).strip().lower()
            values = [str(v).strip() for v in df[col].dropna().astype(str) if str(v).strip()][:30]
            scores = {}
            for f, tst in VALUE_TESTS.items():  # what do the values look like?
                if values:
                    scores[f] = scores.get(f, 0) + 60 * sum(map(tst, values)) / len(values)
            for f, kws in FIELDS.items():  # what does the header say?
                for k in kws:
                    if h == k:
                        scores[f] = scores.get(f, 0) + 50
                    elif re.search(rf"\b{re.escape(k)}\b", h):
                        scores[f] = scores.get(f, 0) + 35 + len(k) / 4  # longer keyword = more specific
            if h in self.overrides:
                best, conf, how = self.overrides[h], 100, "user"
            elif self.mem.recall("header", h):
                best, conf, how = self.mem.recall("header", h), 90, "memory"
            elif scores:
                best = max(scores, key=scores.get)
                conf, how = min(99, round(scores[best])), "header+values"
            else:
                best, conf, how = None, 0, "-"
            if best and conf >= 35 and best not in used:
                used.add(best)
                report[col] = {"field": best, "confidence": conf, "how": how}
                if how != "memory":
                    self.mem.remember("header", h, best)  # learn this header for next client files
            else:
                report[col] = {"field": None, "confidence": conf, "how": "kept as is"}
        return report


# Which agent can fill which field, and what it needs as input. The main agent uses this table to decide.
ROUTES = {
    "website":          [("company_from_email", ["email"]), ("company_agent", ["company"])],
    "phone":            [("company_agent", ["company|website|email"])],
    "email":            [("email_for_person", ["person", "company|website|email"]), ("company_agent", ["company|website"])],
    "full_name":        [("company_agent", ["company|website|email"])],
    "first_name":       [("company_agent", ["company|website|email"])],
    "last_name":        [("company_agent", ["company|website|email"])],
    "title":            [("people_lookup", ["person"]), ("company_agent", ["company|website|email"])],
    "linkedin_person":  [("people_lookup", ["person"]), ("company_agent", ["company|website|email"])],
    "linkedin_company": [("company_agent", ["company|website|email"]), ("company_profile", ["company"])],
    "company_size":     [("company_profile", ["company|website|email"])],
    "company":          [("company_from_email", ["email"]), ("company_agent", ["website"])],
}
ORDER = ["read_post", "company_from_email", "company_agent", "email_for_person", "people_lookup", "company_profile"]
LINKEDIN_FIELDS = {"linkedin_person", "linkedin_company", "company_size", "title"}


def _has(r: dict, need: str) -> bool:
    if need == "person":
        return bool(r.get("full_name") or r.get("first_name"))
    return any(r.get(x) for x in need.split("|"))


def plan_row(r: dict, columns: set) -> tuple:
    """Main agent decision for one row.
    targets = client columns that are empty in this row; for each, pick the first agent whose inputs are present.
    Returns (agents to call in order, fields it will try to fill, fields it cannot fill + why)."""
    targets = [f for f in columns if f in ROUTES and not r.get(f)]
    avail = dict(r)  # a field planned earlier can feed a later agent (email -> website -> company agent)
    calls, fill, cannot = set(), [], []
    if r.get("post_url"):
        calls.add("read_post")
    for f in sorted(targets, key=lambda f: f not in ("website", "company")):
        for agent, needs in ROUTES[f]:
            if all(_has(r if n == "person" else avail, n) for n in needs):  # a person must be given, not planned
                calls.add(agent)
                fill.append(f)
                avail[f] = "(planned)"
                break
        else:
            cannot.append(f"{f} (needs {' or '.join(n for _, ns in ROUTES[f] for n in ns)})")
    return [a for a in ORDER if a in calls], fill, cannot


def _first_last(name: str) -> tuple:
    parts = re.sub(r"^Dr\.?\s", "", name or "").split()
    return (parts[0], parts[-1]) if len(parts) > 1 else (name, "")


def run(path: str, industry: str = "", use_linkedin=None, overrides=None, limit=None, crm_path=None,
        out_dir: str = str(OUTPUT_DIR), use_apify=False) -> Path:
    t0 = time.time()
    main = MainAgent()
    from lead_agent.connectors import apify
    if use_apify and not apify.available():
        log("main", "apify fallback requested but APIFY_TOKEN is not set -> free sources only")
        use_apify = False
    elif use_apify:
        log("main", f"apify fallback ON (paid, only rows where free sources find no decision-maker); "
                    f"spent this month {apify.spent_this_month():.2f}/{apify.budget():.2f} USD")
    src = Path(path)
    df = (pd.read_csv(src, dtype=str) if src.suffix.lower() == ".csv" else pd.read_excel(src, dtype=str)).fillna("")
    if limit:
        df = df.head(limit)
    mapper = FieldMapper(main.mem, {k.lower(): v for k, v in (overrides or {}).items()})
    fmap = mapper.detect(df)
    for col, m in fmap.items():
        log("main", f"column '{col}' -> {m['field'] or '(not used)'} ({m['how']}, {m['confidence']})")
    col_of = {m["field"]: col for col, m in fmap.items() if m["field"]}
    columns = set(col_of)
    empty_cols = [f for f in columns if (df[col_of[f]] == "").any()]
    # main agent decision: LinkedIn agents only if the client asked for LinkedIn-type fields (or the user forced it)
    li = use_linkedin if use_linkedin is not None else bool(LINKEDIN_FIELDS & set(empty_cols))
    log("main", f"client columns: {sorted(columns)}; to fill: {sorted(empty_cols)}; LinkedIn agents: {'on' if li else 'off'}")
    from lead_agent.core.crm import CRM
    crm = CRM(crm_path, main.mem) if crm_path else None

    filled_cells, extras = [], []
    for i, row in df.iterrows():
        r = {f: str(row[c]).strip() for f, c in col_of.items()}
        if r.get("state", "").upper()[:2] in US_STATES:
            r["state"] = r["state"].upper()[:2]
        if r.get("full_name") and not r.get("first_name"):
            r["first_name"], r["last_name"] = _first_last(r["full_name"])
        plan, fill, cannot = plan_row(r, columns)
        got, why, ex = {}, {}, {}
        ind = r.get("industry") or industry or ""

        def put(field, value, agent):
            if value and field in columns and not r.get(field) and field not in got:
                got[field], why[field] = str(value), agent

        if "read_post" in plan:
            post = linkedin.read_post(r["post_url"])
            if post:
                ex.update({"agent_post_author": post["author"], "agent_post_date": post["date"],
                           "agent_post_content": post["content"][:500], "agent_post_likes": post["likes"],
                           "agent_post_comments": " || ".join(f"{c['commenter']}: {c['comment'][:120]}" for c in post["comments"])})
        if "company_from_email" in plan:
            d = r["email"].split("@")[-1].lower()
            if d not in FREE_MAIL:
                put("website", "https://" + d, "email domain")
                r["website"] = r.get("website") or "https://" + d
        res = {}
        if "company_agent" in plan:
            c = {"company": r.get("company") or domain_of(r.get("website", "")), "website": r.get("website", ""),
                 "domain": domain_of(r.get("website", "")), "phone": us_phone(r.get("phone", "")),
                 "address": r.get("address", ""), "city": r.get("city", ""), "state": r.get("state", ""),
                 "zip": r.get("zip", ""), "people": [], "sources": {"client"}}
            if c["website"] and not c["website"].startswith("http"):
                c["website"] = "https://" + c["website"]
            res = main.company.run(c, ind or "business", li, use_apify)
            put("website", res["website"], "company agent")
            put("phone", res["phone"], "company agent")
            put("linkedin_company", res["linkedin_company"], "company agent (website)")
            if not r.get("company") and res["company"] and res["company"] != c["company"]:
                put("company", res["company"], "company agent (website)")
            r["domain"] = domain_of(res["website"]) or c["domain"]
            person_given = bool(r.get("full_name") or r.get("first_name"))
            if res["contact_name"] and not person_given:  # client wants a contact: give the decision-maker
                f, l = _first_last(res["contact_name"])
                put("full_name", res["contact_name"], "company agent (decision-maker)")
                put("first_name", f, "company agent (decision-maker)")
                put("last_name", l, "company agent (decision-maker)")
                put("title", res["contact_title"], "company agent (decision-maker)")
                put("linkedin_person", res["contact_linkedin"], "company agent (LinkedIn search)")
                put("email", res["contact_email"], f"company agent ({res['email_status']})")
            elif res["contact_name"] and res["contact_name"].lower() != (r.get("full_name") or "").lower():
                ex["agent_decision_maker"] = f'{res["contact_name"]} ({res["contact_title"]}) {res["contact_email"]}'.strip()
            put("email", res["company_email"], "company agent (website)")
            ex["agent_priority"], ex["agent_signals"] = res["priority"], res["signals"]
        if "email_for_person" in plan:
            dom = r.get("domain") or domain_of(r.get("website", "")) or (r["email"].split("@")[-1] if r.get("email") else "")
            if dom and r.get("last_name"):
                fn, ln = re.sub(r"\W", "", r["first_name"]), re.sub(r"\W", "", r["last_name"])
                em, how = main.company.mem.email_guess(fn, ln, dom)
                put("email", em, "email pattern: " + email_status(em, False).replace("pattern guess", how))
        if "people_lookup" in plan and li:
            name = r.get("full_name") or f'{r.get("first_name", "")} {r.get("last_name", "")}'.strip()
            comp = r.get("company") or got.get("company", "") or r.get("domain", "").split(".")[0]
            info = main.people.run(name, r.get("linkedin_person", ""), comp)
            put("title", info.get("headline"), "people agent (LinkedIn search)")
            put("linkedin_person", info.get("profile"), "people agent (LinkedIn search)")
        if "company_profile" in plan and li:
            name = r.get("company") or got.get("company") or res.get("company", "")
            url, info = main.profile.run(name, r.get("city", ""), r.get("linkedin_company") or got.get("linkedin_company", ""))
            put("company_size", info.get("company_size"), "company-profile agent (LinkedIn page)")
            put("linkedin_company", url, "company-profile agent")
        if crm:
            ex["agent_in_crm"] = crm.match(r.get("company", ""), r.get("website") or got.get("website", ""),
                                           r.get("email", ""), r.get("phone") or got.get("phone", ""))
        for f, v in got.items():
            df.at[i, col_of[f]] = v
            filled_cells.append((i, col_of[f]))
        missed = [col_of[f] for f in fill if f not in got]
        ex["agent_plan"] = " > ".join(plan) or "nothing to do"
        ex["agent_filled"] = "; ".join(f"{col_of[f]} <- {why[f]}" for f in got)
        ex["agent_not_found"] = "; ".join(x for x in [", ".join(missed), ", ".join(cannot)] if x)
        extras.append(ex)
        log("main", f"row {i + 1}/{len(df)}: plan {ex['agent_plan']} | filled {len(got)}/{len(fill)}")

    add = pd.DataFrame(extras).fillna("")
    result = pd.concat([df.reset_index(drop=True), add], axis=1)
    rep = pd.DataFrame([{"column": c, "detected_as": m["field"] or "", "confidence": m["confidence"], "how": m["how"],
                         "cells_filled": sum(1 for _, cc in filled_cells if cc == c)} for c, m in fmap.items()])
    out = Path(out_dir) / f"{src.stem}-filled.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    from openpyxl.styles import PatternFill
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        result.to_excel(xw, sheet_name="data", index=False)
        rep.to_excel(xw, sheet_name="field_report", index=False)
        ws = xw.sheets["data"]
        yellow = PatternFill("solid", fgColor="FFF2A8")
        pos = {c: j + 1 for j, c in enumerate(result.columns)}
        for i, c in filled_cells:  # highlight what the agent filled
            ws.cell(row=i + 2, column=pos[c]).fill = yellow
    main.mem.log("run", {"mode": "excel", "file": src.name, "rows": len(df), "cells_filled": len(filled_cells)})
    log("main", f"done: {len(df)} rows, {len(filled_cells)} cells filled (yellow) -> {out}  ({time.time() - t0:.0f}s)")
    return out


def main():
    ap = argparse.ArgumentParser(description="Fill a client Excel/CSV: the agent reads the columns and decides what to do")
    ap.add_argument("file")
    ap.add_argument("--industry", default="", help="used when the file has no industry column")
    ap.add_argument("--linkedin", action="store_true", default=None, help="force LinkedIn agents on (default: auto)")
    ap.add_argument("--map", action="append", default=[], help='fix a column: --map "Practice Name=company"')
    ap.add_argument("--limit", type=int)
    ap.add_argument("--exclude", help="CRM / existing customers file: adds agent_in_crm")
    ap.add_argument("--apify", action="store_true", help="PAID fallback: Apify LinkedIn employees when free sources find none")
    a = ap.parse_args()
    run(a.file, a.industry, a.linkedin, dict(m.split("=", 1) for m in a.map), a.limit, a.exclude, use_apify=a.apify)


if __name__ == "__main__":
    main()
