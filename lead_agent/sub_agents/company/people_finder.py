"""Free decision-maker finder (no paid APIs, no logins).

find_decision_makers(company, website, city, state) -> [{"name","title","source","evidence_url"}, ...] best first.
Order (stops as soon as a good owner/president/CEO/GM is found):
  1. own site: home + sitemap.xml + about/team/leadership/... pages, text patterns and name-above-title cards
  2. schema.org JSON-LD on those pages (founder / employee / member / Person.jobTitle)
  3. search-engine snippets (DuckDuckGo via connectors.duckduckgo._ddg, cached + throttled), accepted only when the snippet names the
     company AND a plausible person next to a title.
"""
import json
import re
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from lead_agent.connectors.duckduckgo import _ddg
from lead_agent.core.enrichment import (FIRST_NAMES, PLACEHOLDER_NAMES, STOPNAME, clean_title, historical_context, strip_noise,
                                        testimonial_context, title_tier)
from lead_agent.core.utils import GENERIC_NAME_WORDS, domain_of, fetch, name_matches_site

PAGE_KEYS = ("about", "team", "leadership", "management", "our-story", "ourstory", "company", "history", "contact",
             "staff", "people", "who-we-are", "meet", "owner", "founder", "principal")
PAGE_RANK = ("team", "leadership", "management", "our-story", "ourstory", "about", "who-we-are", "meet", "owner", "founder",
             "people", "staff", "history", "company", "contact")
BAD_PATH = ("blog", "news", "product", "category", "tag/", "wp-content", "careers", "/jobs", ".pdf", ".jpg", ".png")
MAX_PAGES = 9
SEARCH_BUDGET = 45  # seconds of search-engine waiting per company (the free endpoint can hang)

ROLE = (r"(?:Co-?Owners?|Owners?(?:\s*(?:&|and|/)\s*(?:President|CEO|Founder|Operator))?|Co-?Founders?|Founders?|"
        r"(?:Vice|Executive|Senior)\s+President|President|CEO|Chief\s+(?:Executive|Operating|Financial|Technology)\s+Officer|"
        r"COO|CFO|CTO|General\s+Manager|Managing\s+(?:Director|Partner|Member)|Principal|Proprietor|Partner|"
        r"Plant\s+Manager|Operations\s+Manager|Director\s+of\s+[A-Z][a-z]+|Chairman|Sales\s+Manager|VP(?:\s+of\s+[A-Z][a-z]+)?)")
# a person name: capitalised words; (?-i:) keeps the capital-letter rule when the rest is case-insensitive
NAME = r"(?-i:((?:Dr\.\s)?[A-Z][a-z]{1,15}(?:\s[A-Z]\.)?\s[A-Z][a-zA-Z'’-]{1,20}))"
SEP = r"\s*(?:[,\-–—|:(/]|\bis\b|\bas\b|\bserves as\b)?\s*(?:the\s+|our\s+|its\s+)?"
SN_RX = [
    ("name_role", re.compile(NAME + r"\s*[,\-–—|(]\s*(?:the\s+)?" + r"(" + ROLE + r")\b", re.I)),
    ("role_name", re.compile(r"\b(" + ROLE + r")\b\s*[,:\-–—|]?\s*" + NAME, re.I)),
    ("is_role", re.compile(NAME + r"\s+(?:is|was|serves as|has been)\s+(?:the\s+|our\s+|its\s+|a\s+)?(?:\w+\s+){0,2}?(" + ROLE + r")\b", re.I)),
    ("role_of", re.compile(NAME + r",?\s+(?:the\s+|our\s+)?(" + ROLE + r")\s+(?:of|at|for)\b", re.I)),
    ("by", re.compile(r"\b(Founded|Owned|Established|Started|Run|Operated|Founder)\s+(?:and\s+operated\s+)?by\s+" + NAME, re.I)),
]
BY_TITLE = {"founded": "Founder", "founder": "Founder", "owned": "Owner", "established": "Founder", "started": "Founder",
            "run": "Owner", "operated": "Owner"}
EXTRA_STOP = {"machine", "machining", "metal", "metals", "steel", "tool", "tools", "precision", "plastic", "plastics",
              "mold", "molding", "injection", "fabrication", "fabricators", "engineering", "industries", "products",
              "manufacturing", "welding", "grinding", "screen", "screens", "iron", "ironworks", "supply", "los", "angeles",
              "california", "construction", "company", "corporation", "inc", "llc", "since", "years", "contact", "email",
              "phone", "address", "sales", "quality", "custom", "south", "north", "east", "west", "valley", "beach",
              "park", "san", "santa", "long", "culver", "orange", "county", "industrial", "aerospace", "electro",
              "plating", "sheet", "shop", "shops", "work", "works", "laser", "cutting", "design", "designs", "systems",
              "technologies", "technology", "solutions", "enterprises", "enterprise", "associates", "brothers", "sons",
              "capabilities", "equipment", "processing", "finishing", "coating", "stamping", "assembly", "materials",
              "linkedin", "facebook", "instagram", "twitter", "youtube", "bbb", "yelp", "view", "profile", "business",
              "free", "get", "request", "learn", "read", "click", "call", "today", "first", "last", "best", "top",
              "customer", "service", "team", "leadership", "management", "message", "our", "your", "about"}
SKIP_HOSTS = ("facebook.", "instagram.", "twitter.", "x.com", "youtube.", "pinterest.", "tiktok.")
# review / rating sites: the people named there are reviewers or reply-signers, not the company's decision-makers
REVIEW_HOSTS = ("yelp.", "trustpilot.", "sitejabber.", "consumeraffairs.", "birdeye.", "ratemds.", "healthgrades.", "vitals.",
                "google.", "glassdoor.", "indeed.", "reddit.", "nextdoor.", "zocdoc.", "tripadvisor.", "angi.", "thumbtack.",
                "yellowpages.", "mapquest.", "wellness.com", "caredash.", "reviews.")


# ------------------------------------------------------------------ helpers
def _toks(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]{3,}", (s or "").lower()) if t not in GENERIC_NAME_WORDS]


def valid_name(name: str, co_toks: set[str] = frozenset(), strict_first: bool = True) -> bool:
    n = re.sub(r"^Dr\.\s", "", (name or "").strip())
    words = n.replace(".", "").lower().split()
    if len(words) < 2 or len(words) > 3 or len(n) > 34 or n.lower() in PLACEHOLDER_NAMES:
        return False
    if set(words) & (STOPNAME | EXTRA_STOP | GENERIC_NAME_WORDS):
        return False
    if FIRST_NAMES and strict_first and words[0] not in FIRST_NAMES:
        return False
    joined = " ".join(co_toks)
    if co_toks and all(w in co_toks or (len(w) >= 4 and w in joined) for w in words):  # every word is a company word
        return False
    return True


def _display_name(company: str, website: str, home_html: str) -> str:
    """Best company name for searching: the given one if it resembles the domain, else site title / og:site_name / domain."""
    dom = domain_of(website)
    company = (company or "").strip()
    if re.search(r"\s[|\-–—:]\s", company):  # 'Name | Slogan': keep the part that looks like the domain
        parts = [p.strip() for p in re.split(r"\s[|\-–—:]\s", company) if p.strip()]
        company = next((p for p in parts if name_matches_site(p, dom, p)), parts[0] if parts else company)
    keywordy = bool(re.search(r"\b(?:in|los angeles|california|supplier|near me)\b|,", company, re.I))
    if company and not keywordy and name_matches_site(company, dom, company) and len(company) <= 60:
        return company
    try:
        soup = BeautifulSoup(home_html or "", "html.parser")
        og = soup.find("meta", property="og:site_name")
        cands = [og["content"]] if og and og.get("content") else []
        if soup.title:
            cands += re.split(r"\s[|\-–—:·•]\s", soup.title.get_text(" ", strip=True))
        for c in cands:
            c = c.strip()
            if 3 <= len(c) <= 50 and name_matches_site(c, dom, c) and _toks(c) and not re.search(r"\b(?:in|los angeles|california|supplier)\b|,", c, re.I):
                return c
    except Exception:
        pass
    return dom.split(".")[0].replace("-", " ").title() if dom else (company or "")


class Co:
    """Company identity used to accept / reject evidence."""

    def __init__(self, company, website, city, state, home_html=""):
        self.dom = domain_of(website)
        self.label = re.sub(r"[^a-z0-9]", "", self.dom.split(".")[0]) if self.dom else ""
        self.name = _display_name(company, website, home_html)
        self.toks = _toks(self.name)
        self.city, self.state = (city or "").strip(), (state or "").strip()
        self.tokset = set(self.toks) | {self.label}

    def in_text(self, text: str, strict_city: bool = True) -> bool:
        low = text.lower()
        compact = re.sub(r"[^a-z0-9]", "", low)
        if self.dom and self.dom in low:
            return True
        if len(self.label) >= 6 and self.label in compact:
            return True
        if not self.toks:
            return False
        hits = sum(t in low for t in self.toks)
        need = len(self.toks) if len(self.toks) <= 2 else 2
        if hits < need:
            return False
        if len(self.toks) == 1 and strict_city and self.city:  # one-word names are ambiguous: also need the city
            return self.city.lower() in low or "los angeles" in low or (self.state and f", {self.state.lower()}" in low)
        return True


def _mk(name, title, source, url):
    return {"name": re.sub(r"['’]s?$", "", name.strip()), "title": title, "source": source, "evidence_url": url}


def _is_team_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(k in path for k in PAGE_KEYS)


def _pairs_in_text(text: str, co: Co, source: str, url: str, allow_by: bool = True) -> list[dict]:
    """Name + role pairs in running text. Rejected: names inside testimonials / reviews / quotes / bylines, historical founders,
    and (outside team-type pages and search snippets) junior roles such as Director / Manager, which are often a customer's title."""
    out = []
    team_page = source == "website" and _is_team_url(url)
    for kind, rx in SN_RX:
        for m in rx.finditer(text):
            g = m.groups()
            if kind == "name_role" or kind == "is_role" or kind == "role_of":
                name, role = g[0], g[1]
            elif kind == "role_name":
                role, name = g[0], g[1]
            else:
                if not allow_by:
                    continue
                name, role = g[1], BY_TITLE.get(g[0].lower(), "Owner")
            title = clean_title(role)
            if testimonial_context(text, m.start(), m.end()) or historical_context(text, m.start(), m.end()):
                continue  # customer quote / review / byline / founder from decades ago
            if not team_page and title_tier(title) >= 3 and source in ("website", "search"):
                continue  # 'Director, Manufacturing Engineering' on a home page is usually a customer, not our contact
            if title_tier(title) == 0 and re.search(r"\b(?:1[0-8]\d\d|19[0-4]\d)\b", text[max(0, m.start() - 60):m.end() + 60]):
                continue  # 'In 1929 founder Andrew ...': a historical founder, not a current decision-maker
            # owner/founder/president named on the company's own site may have a first name that is not in the US list
            relax = source == "website" and bool(title) and title_tier(title) == 0 and kind in ("name_role", "role_name", "is_role")
            if title and valid_name(name, co.tokset, strict_first=not relax):
                out.append(_mk(name, title, source, url))
    return out


def _lines_pairs(html: str, co: Co, url: str) -> list[dict]:
    """Team 'cards': a name on one line and a short role on the neighbouring line."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "form"]):
        t.decompose()
    strip_noise(soup)
    lines = [re.sub(r"\s+", " ", l).strip() for l in soup.get_text("\n", strip=True).split("\n")]
    lines = [l for l in lines if l]
    name_rx = re.compile(r"^" + NAME + r"(?:,\s*[A-Z][A-Za-z.]{1,6})?$")
    out = []
    for i, l in enumerate(lines):
        m = name_rx.match(l)
        if not m or not valid_name(m.group(1), co.tokset):
            continue
        around = " ".join(lines[max(0, i - 2):i + 4])
        if (i and len(lines[i - 1]) > 90 and re.search(r"[\"”“]$", lines[i - 1])) or testimonial_context(around, around.find(l), around.find(l) + len(l)):
            continue  # a quoted paragraph just above the name: a testimonial signature
        for j in (i + 1, i - 1, i + 2):
            if 0 <= j < len(lines) and len(lines[j]) <= 60:
                t = clean_title(lines[j])
                if t and title_tier(t) <= 3 and not name_rx.match(lines[j]):
                    out.append(_mk(m.group(1), t, "website", url))
                    break
    return out


def _jsonld(html: str, co: Co, url: str) -> list[dict]:
    out = []

    def person(p, default_title):
        if isinstance(p, str):
            name, title = p, ""
        elif isinstance(p, dict):
            name, title = p.get("name", ""), p.get("jobTitle", "") or ""
            if isinstance(title, list):
                title = ", ".join(map(str, title))
        else:
            return
        name = re.sub(r"\s+", " ", str(name)).strip()
        t = clean_title(str(title)) or default_title
        if t and valid_name(name, co.tokset):
            out.append(_mk(name, t, "jsonld", url))

    def walk(o, depth=0):
        if depth > 8:
            return
        if isinstance(o, list):
            for x in o:
                walk(x, depth + 1)
        elif isinstance(o, dict):
            typ = o.get("@type", "")
            typ = " ".join(typ) if isinstance(typ, list) else str(typ)
            if re.search(r"Review|Rating|Comment|Article|BlogPosting|Testimonial", typ):
                return  # reviewers and authors are not the company's people
            if "Person" in typ and o.get("jobTitle"):
                person(o, "")
            for key, dt in (("founder", "Founder"), ("founders", "Founder"), ("employee", ""), ("employees", ""),
                            ("member", ""), ("members", ""), ("owner", "Owner"), ("author", "")):
                if key in o and key not in ("author",):
                    v = o[key]
                    for p in (v if isinstance(v, list) else [v]):
                        person(p, dt)
            for v in o.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)

    for m in re.finditer(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html, re.S | re.I):
        try:
            walk(json.loads(m.group(1).strip()))
        except Exception:
            continue
    if not out and "jobTitle" in html:  # broken / pasted-as-text JSON-LD (curly quotes, <br>): loose regex on the plain text
        txt = re.sub(r"<[^>]+>", " ", html).replace("“", '"').replace("”", '"')
        for m in re.finditer(r'"name"\s*:\s*"([^"]{3,40})"\s*,\s*"jobTitle"\s*:\s*"([^"]{2,80})"', txt):
            person({"name": m.group(1), "jobTitle": m.group(2)}, "")
    return out


def _copyright(text: str, co: Co, url: str) -> list[dict]:
    out = []
    for m in re.finditer(r"(?:©|&copy;|\(c\)|Copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?)?\s*,?\s*(?-i:((?:[A-Z][a-z]+\s){1,2}[A-Z][a-zA-Z'-]+))", text):
        n = m.group(1).strip()
        if valid_name(n, co.tokset):
            out.append(_mk(n, "Owner (copyright holder)", "website", url))
    return out


def _candidate_pages(home: str, html: str, site_root: str) -> list[str]:
    host = urlparse(home).netloc.lower().removeprefix("www.")
    cands = {}

    def consider(u: str, text: str = ""):
        u = u.split("#")[0].split("?")[0]
        p = urlparse(u)
        if p.netloc.lower().removeprefix("www.") != host or u.rstrip("/") == home.rstrip("/"):
            return
        low = (p.path + " " + text).lower()
        if any(b in p.path.lower() for b in BAD_PATH):
            return
        for rank, k in enumerate(PAGE_RANK):
            if k in low:
                if u not in cands or rank < cands[u]:
                    cands[u] = rank
                return

    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        consider(urljoin(home, a["href"]), a.get_text(" ", strip=True)[:40])
    for sm in (urljoin(site_root, "/sitemap.xml"), urljoin(site_root, "/sitemap_index.xml"), urljoin(site_root, "/wp-sitemap.xml")):
        st, body = fetch(sm, check_robots=True)
        if st != 200 or "<loc>" not in body:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
        subs = [l for l in locs if l.endswith(".xml")][:3]
        for sub in subs:  # one level of sitemap index
            s2, b2 = fetch(sub, check_robots=True)
            if s2 == 200:
                locs += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", b2)
        for l in locs:
            if not l.endswith(".xml"):
                consider(l)
        break
    return [u for u, _ in sorted(cands.items(), key=lambda kv: (kv[1], len(kv[0])))][: MAX_PAGES - 1]


# ------------------------------------------------------------------ source 1 + 2
def _site_people(co: Co, website: str, html: str) -> tuple[list[dict], list[dict]]:
    """(own-site people, jsonld people)."""
    if not website or not html:
        return [], []
    if "://" not in website:
        website = "http://" + website
    pu = urlparse(website)
    root = f"{pu.scheme}://{pu.netloc}"
    pages = {website: html}
    for u in _candidate_pages(website, html, root):
        s2, h2 = fetch(u, check_robots=True)
        if s2 == 200 and h2:
            pages[u] = h2
    web, ld = [], []
    for u, h in pages.items():
        ld += _jsonld(h, co, u)
        sp = BeautifulSoup(h, "html.parser")
        for t in sp(["script", "style", "noscript"]):
            t.decompose()
        strip_noise(sp)  # testimonials / reviews / quotes / blog bylines
        text = sp.get_text(" ", strip=True)
        web += _lines_pairs(h, co, u) + _pairs_in_text(text, co, "website", u)
        if u == website or not web:
            web += _copyright(text, co, u)
    return web, ld


# ------------------------------------------------------------------ source 3
def _search_people(co: Co) -> list[dict]:
    out = []
    nm = co.name
    where = co.city or "Los Angeles"
    queries = [f'"{nm}" {where} (owner OR president OR CEO OR founder OR "general manager")',
               f'"{nm}" {co.state} "founded by" OR "owner" OR "president" {where}']
    if len(co.toks) <= 1 and co.dom:
        queries.append(f'"{co.dom}" owner OR president OR founder')
    t0 = time.time()
    for qi, q in enumerate(queries):
        left = SEARCH_BUDGET - (time.time() - t0)
        if left <= 3:
            break
        try:
            hits = _ddg(q, 8)  # main thread only: the cache connection is not thread-safe
        except Exception:
            break
        for h in hits:
            url, title, body = h.get("href", ""), h.get("title", ""), h.get("body", "")
            if any(s in url for s in SKIP_HOSTS) and "linkedin" not in url:
                continue
            if any(s in domain_of(url) for s in REVIEW_HOSTS):
                continue
            text = f"{title}. {body}"
            if not co.in_text(text):
                continue
            if "linkedin.com/in/" in url:
                parts = [p.strip() for p in re.split(r"\s[-–—|]\s", title.replace("| LinkedIn", "")) if p.strip()]
                own = " ".join(parts) + " " + re.split(r"Education:|connections on LinkedIn", body)[0]  # this profile's own words
                geo = bool(re.search(r"los angeles|california|, ca\b|" + re.escape(co.city.lower() or "zzzz"), own.lower()))
                if len(parts) >= 2 and valid_name(parts[0], co.tokset) and co.in_text(own, strict_city=False)                         and (geo or len(co.toks) >= 2):
                    t = next((clean_title(p, False) for p in parts[1:] if len(p) <= 60 and clean_title(p, False)), "")
                    if t:
                        out.append(_mk(parts[0], t, "search", url.split("?")[0]))
                continue
            out += _pairs_in_text(text, co, "search", url)
        if any(title_tier(p["title"]) <= 1 for p in out):
            break
    return out


# ------------------------------------------------------------------ main
def _rank(people: list[dict]) -> list[dict]:
    src_rank = {"website": 0, "jsonld": 1, "search": 2}
    cnt = {}
    for p in people:
        cnt[p["name"].lower()] = cnt.get(p["name"].lower(), 0) + 1
    best = {}
    for p in sorted(people, key=lambda p: (title_tier(p["title"]), src_rank.get(p["source"], 3))):
        best.setdefault(p["name"].lower(), p)
    return sorted(best.values(), key=lambda p: (title_tier(p["title"]), -cnt[p["name"].lower()], src_rank.get(p["source"], 3)))


def _good(people: list[dict]) -> bool:
    return any(title_tier(p["title"]) <= 1 for p in people)


def find_decision_makers(company: str, website: str, city: str, state: str) -> list[dict]:
    """Free-source decision-makers for one company, best first. Stops early once an owner/president/CEO/GM is found."""
    people: list[dict] = []
    home = ""
    if website:
        st, home = fetch(website if "://" in website else "http://" + website, check_robots=True)
        home = home if st == 200 else ""
    co = Co(company, website, city, state, home)
    try:
        web, ld = _site_people(co, website, home)
    except Exception:
        web, ld = [], []
    people += web
    if _good(people):
        return _rank(people)
    people += ld
    if _good(people):
        return _rank(people)
    try:
        people += _search_people(co)
    except Exception:
        pass
    return _rank(people)
