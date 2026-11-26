"""Web-search discovery source: DuckDuckGo (US region) for businesses with websites, and LinkedIn people via search."""
import re

from lead_agent.connectors.duckduckgo import _ddg
from lead_agent.core.enrichment import clean_title
from lead_agent.core.utils import domain_of, usable_name, GENERIC_NAME_WORDS, is_directory
from lead_agent.sub_agents.discovery.brands._common import _company


# Industry -> search phrases (sub-niches); others get just their own name. Add more industries here.
QUERY_VARIANTS = {
    "manufacturing": ["CNC machine shop", "metal fabrication", "injection molding", "contract manufacturer",
                      "precision machining", "sheet metal fabrication", "electronics manufacturer", "food manufacturer",
                      "plastic injection molding company", "apparel manufacturer", "aerospace parts manufacturer",
                      "machine shop", "CNC machining", "welding and fabrication shop", "industrial equipment manufacturer",
                      "furniture manufacturer", "packaging manufacturer", "custom manufacturing",
                      "tool and die metal stamping", "die casting foundry", "powder coating and finishing",
                      "medical device manufacturer", "cosmetics contract manufacturer", "printed circuit board assembly",
                      "rubber and plastics products manufacturer", "cabinet and millwork manufacturer",
                      "OEM manufacturing company", "food processing company"],
}
# Used only when the pool is still small: the same sub-niches in nearby cities of the same metro (all in-state).
NEARBY = {
    "los angeles": ["Vernon CA", "Gardena CA", "Torrance CA", "Carson CA", "Commerce CA", "Santa Fe Springs CA",
                    "City of Industry CA", "Burbank CA", "Chatsworth CA", "Pasadena CA", "Anaheim CA", "Irwindale CA"],
    "san diego": ["Carlsbad CA", "Chula Vista CA", "Oceanside CA", "El Cajon CA", "Vista CA", "San Marcos CA"],
}
# Search-result titles that are never a manufacturer / practice site.
_NOT_COMPANY = re.compile(r"\b(brewery|brewing|winery|distillery|tasting room|taproom)\b", re.I)
_LISTICLE = re.compile(r"\b(best|top \d+|\d+ top|top [a-z ]*companies|(?:companies|manufacturers|firms|shops|services) in|near me|"
                       r"reviews?|directory|list of|law firms|lawyers|association|chamber of commerce)\b", re.I)


def web_search(industry: str, location: str, limit: int, queries: tuple[str, ...] | None = None,
               errors: dict | None = None) -> list[dict]:
    """DuckDuckGo (US region) for businesses that have websites. Skips directories. `errors` (caller-owned dict)
    counts queries that returned nothing, so an empty result is visible instead of silent."""
    errors = errors if errors is not None else {}
    out, seen = [], set()
    terms = QUERY_VARIANTS.get(industry.lower())
    cap = limit * 4
    if queries:
        qs = list(queries)
    elif terms:  # sub-niche variants: one query each, most specific first
        qs = [f"{t} {location}" for t in terms] + [f"{industry} company {location}"]
        cap = max(cap, 100)  # sparse industry: wider pool so enough survive the area / size filters
    else:
        qs = [f"{industry} firm {location}", f"{industry} {location} contact us", f"{industry} company {location}"]
    nearby = [] if queries or not terms else NEARBY.get(location.split(",")[0].strip().lower(), [])
    empty = 0
    for qi, q in enumerate(qs + [None]):
        if q is None:  # core queries done: widen to nearby cities only if the pool is still small
            if len(out) >= cap or not nearby:
                break
            qs2 = [f"manufacturer {c}" for c in nearby] + [f"machine shop {c}" for c in nearby]
            for q2 in qs2:
                if len(out) >= cap:
                    break
                empty += _collect(q2, limit, out, seen, industry)
            break
        if len(out) >= cap:  # enough candidates, spare the free search endpoint
            break
        empty += _collect(q, limit, out, seen, industry)
    if empty:
        errors["web"] = errors.get("web", 0) + empty
        print(f"[discovery] web: {empty} search queries returned no results", flush=True)
    return out


def _collect(q: str, limit: int, out: list, seen: set, industry: str) -> int:
    """Run one search, append new company dicts to `out`. Returns 1 when the search gave nothing at all."""
    hits = _ddg(q, min(limit * 2, 15))  # n<=15: the first backend already satisfies the connector retry threshold (fast)
    for h in hits:
        d = domain_of(h.get("href", ""))
        if not d or d in seen or is_directory(d):
            continue
        title = h.get("title", "")
        if _LISTICLE.search(title):
            continue  # listicles and directories, not a company
        if industry.lower() in ("manufacturing", "manufacturer") and (_NOT_COMPANY.search(title) or _NOT_COMPANY.search(d)):
            continue
        seen.add(d)
        name = re.split(r"\s[|\-–:]\s", title)[0]
        name = "" if name.lower().startswith("contact") else name
        out.append(_company(name, "ddg", website="https://" + d))
    return 0 if hits else 1


def find_linkedin_people(company: str, city: str) -> list[dict]:
    """Public LinkedIn profiles via search engine only (no login, no LinkedIn requests)."""
    if not usable_name(company):  # empty / generic name would accept anybody (all rows got the same person)
        return []
    q = f'site:linkedin.com/in "{company}" (owner OR founder OR CEO OR president OR director OR partner)'
    hits = _ddg(q, 5)
    people = []
    for h in hits:
        url, title = h.get("href", ""), h.get("title", "")
        if "linkedin.com/in/" not in url:
            continue
        parts = [p.strip() for p in re.split(r"\s[-–|]\s", title.replace("| LinkedIn", "")) if p.strip()]
        toks = [t for t in re.findall(r"[a-z0-9]{3,}", company.lower()) if t not in GENERIC_NAME_WORDS]
        text = (title + " " + h.get("body", "")).lower()
        if not parts or not toks or sum(t in text for t in toks) < min(2, len(toks)):
            continue
        if len(toks) < 2 and city and city.lower() not in text:  # one-word names are ambiguous: also need the city
            continue
        parts = [p for p in parts if p.lower() != "linkedin"]
        name, job = parts[0], parts[1] if len(parts) > 1 else ""
        if " at " in name or "attorney" in name.lower():  # title came first: take the name from the URL slug
            slug = url.split("/in/")[1].strip("/").split("?")[0]
            words = [w for w in slug.split("-") if w.isalpha()]
            if len(words) < 2:
                continue
            name, job = " ".join(w.title() for w in words[:2]), parts[0]
        people.append({"name": name, "title": clean_title(job, strict=False) or job, "linkedin": url.split("?")[0], "source": "search"})
    return people
