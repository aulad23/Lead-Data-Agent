"""Website crawl, industry verification, people extraction, email checks, signals."""
import re
from functools import lru_cache
from urllib.parse import urljoin, urlparse

import dns.resolver
from bs4 import BeautifulSoup

from lead_agent.config.settings import DATA_DIR
from lead_agent.core.utils import _session, fetch, us_phone, usable_name

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\(?\b[2-9]\d{2}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
TITLES = r"((?:Vice|Executive) President|Plant Manager|Operations Manager|Founding Partner|Managing Attorney|Founding Attorney|Managing Partner|Shareholder|Attorney at Law|Attorney|Broker|Owner|Co-?Founder|Founder|CEO|Chief [A-Z][a-z]+ Officer|President|Managing Partner|Partner|Principal|" \
         r"Practice Owner|Medical Director|Director|General Manager|Office Manager|Practice Manager|Head of [A-Z][a-z]+)"
NAME = r"((?:Dr\.\s)?[A-Z][a-z]+(?:\s[A-Z]\.)?\s[A-Z][a-zA-Z'-]+)"
PERSON_RE = [re.compile(NAME + r"\s*[,\-–|]?\s*" + TITLES), re.compile(TITLES + r"\s*[,:\-–|]?\s*" + NAME)]
STOPNAME = {"clinical", "director", "office", "manager", "practice", "dental", "medical", "general", "chief",
            "officer", "owner", "founder", "partner", "our", "the", "meet", "about", "contact", "team", "center",
            "family", "care", "services", "group", "street", "suite", "austin", "texas", "city", "new", "read", "more"}
_FN = DATA_DIR / "first_names.txt"
FIRST_NAMES = set(_FN.read_text().split()) if _FN.exists() else set()  # US SSA baby names, 1935-2008
STOPNAME |= {"bar", "board", "member", "law", "firm", "legal", "attorney", "attorneys", "associates", "dallas", "houston",
             # practice areas / services that look like "First Last" (e.g. "Brain Injury", "Grace Period")
             "injury", "injuries", "accident", "accidents", "defense", "divorce", "estate", "planning", "trust", "trusts",
             "probate", "immigration", "criminal", "bankruptcy", "custody", "insurance", "claims", "claim", "wrongful",
             "death", "malpractice", "implants", "implant", "whitening", "orthodontics", "cleaning", "veneers",
             "crowns", "braces", "emergency", "cosmetic", "pediatric", "surgery", "therapy", "review", "reviews"}
STOPNAME |= {"machine", "machining", "metal", "steel", "tool", "tools", "precision", "plastic", "plastics", "mold", "molding",
             "fabrication", "engineering", "industries", "products", "manufacturing", "welding", "grinding", "los", "angeles",
             "california", "construction", "sheet", "quality", "custom", "contact", "email", "phone"}
PLACEHOLDER_NAMES = {"john doe", "jane doe", "joe smith", "john smith", "jane smith", "your name", "first last"}
SUBPAGES = ("about", "team", "staff", "our-team", "leadership", "contact", "people", "doctors", "providers", "careers")
TECH = {"WordPress": "wp-content", "Shopify": "cdn.shopify", "Wix": "wix.com", "Squarespace": "squarespace",
        "HubSpot": "hs-scripts", "Google Analytics": "googletagmanager", "Facebook Pixel": "fbevents.js",
        "Calendly": "calendly.com", "Intercom": "intercom", "Drift": "drift.com", "Zendesk": "zdassets"}
# ---- titles: short role phrases only (never sentences, never "President at Company Name Other Person")
_ROLE_RX = re.compile(
    r"\b(Co-?Owners?|Owners?|Co-?Founders?|Founders?|(?:Vice|Executive|Senior|Assistant)\s+President|President|"
    r"Chief\s+[A-Za-z]+\s+Officer|CEO|COO|CFO|CTO|General\s+Manager|Managing\s+(?:Director|Partner|Member|Attorney)|"
    r"Founding\s+(?:Partner|Attorney)|Principal|Proprietor|Partner|Shareholder|Attorney(?:\s+at\s+Law)?|Broker|"
    r"Plant\s+Manager|Operations\s+Manager|Practice\s+(?:Owner|Manager)|Office\s+Manager|Medical\s+Director|"
    r"Director\s+of\s+[A-Z][A-Za-z]+|Director|Chairman|Sales\s+Manager|Head\s+of\s+[A-Z][a-z]+|VP(?:\s+of\s+[A-Z][a-z]+|\s+[A-Z][a-z]+)?)\b",
    re.I)
_FILLER = {"at", "of", "the", "and", "&", "/", "-", "–", "|", ",", "in", "for", "co", "inc", "llc"}


def clean_title(raw: str, strict: bool = True) -> str:
    """Raw text -> short role phrase ('Owner / President'), or '' when there is no role.
    strict: reject text that is mostly something else (a sentence, a company name); False: take the leading role(s)."""
    raw = re.sub(r"\s+", " ", raw or "").strip(" ,.;:-")
    ms = list(_ROLE_RX.finditer(raw))
    if not ms:
        return ""
    roles, seen = [], set()
    for m in ms:
        r = re.sub(r"\s+", " ", m.group(1))
        r = "CEO" if r.lower() == "ceo" else (r.upper() if r.lower() in ("coo", "cfo", "cto") else r.title())
        r = re.sub(r"^Vp", "VP", r)
        if r.lower() not in seen and not (r.lower() in ("owner", "founder", "partner", "president") and any(r.lower() in x.lower() and x.lower() != r.lower() for x in roles)):
            seen.add(r.lower())
            roles.append(r)
        if len(roles) == 2:
            break
    if strict:
        rest = _ROLE_RX.sub(" ", raw)
        words = [w for w in re.findall(r"[A-Za-z0-9']+", rest) if w.lower() not in _FILLER]
        if len(words) > 3 or len(raw) > 70:
            return ""
    return " / ".join(roles)


def title_tier(title: str) -> int:
    """0 owner/founder/president/CEO, 1 GM/managing/principal/partner/COO, 2 VP, 3 other manager/director, 9 none."""
    t = (title or "").lower()
    if not t:
        return 9
    if re.search(r"\b(owner|founder|ceo|chief executive|proprietor|chairman)\b", t) or re.search(r"(?<!vice )(?<!executive )(?<!senior )(?<!assistant )\bpresident\b", t):
        return 0
    if re.search(r"general manager|managing|principal|partner|\bcoo\b|chief|shareholder", t):
        return 1
    if re.search(r"vice president|\bvp\b", t):
        return 2
    return 3


JUNK_EMAIL = ("example.", "sentry", "wixpress", "domain.com", "email.com", ".png", ".jpg", "godaddy")

# ---- testimonials / reviews / quotes / bylines: a name there is a customer or an author, never a decision-maker
_NOISE_PARTS = {"testimonial", "testimonials", "review", "reviews", "reviewer", "blockquote", "quote", "quotes", "rating",
                "ratings", "stars", "feedback", "praise", "kudos", "byline", "casestudy", "casestudies", "cite"}
_NOISE_SKIP = {"get", "request", "free", "rfq", "form", "btn", "button", "cta", "link", "menu", "nav", "instant", "quick",
               "your", "submit", "order", "fast", "online", "contact", "popup", "modal"}
_NOISE_COMPOUND = re.compile(r"(?:post|entry|blog|article|news)[-_ ]?(?:author|meta|byline|by)|posted[-_ ]?by|"
                             r"author[-_ ]?(?:box|bio|meta|name|info)|google[-_ ]?reviews?|case[-_ ]?stud", re.I)
_NOISE_HEADING = re.compile(
    r"testimonial|\breviews?\b|what (?:our |the )?(?:clients?|patients?|customers?|people)(?: are)? (?:say|saying)|"
    r"(?:clients?|patients?|customers?) (?:say|love|stories|reviews)|success stor|case stud|kind words|"
    r"happy (?:patients|clients|customers)|from our (?:patients|clients|customers)|(?:patient|client|customer) stor|"
    r"words from|in the news|recent (?:posts|articles)|latest (?:posts|news|articles)", re.I)
_HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]


def _noise_attr(el) -> bool:
    attrs = getattr(el, "attrs", None) or {}
    vals = []
    for k in ("class", "id", "itemtype", "aria-label", "data-widget_type"):
        v = attrs.get(k)
        if v:
            vals += v if isinstance(v, list) else [v]
    for v in vals:
        v = re.sub(r"([a-z])([A-Z])", r"\1-\2", str(v))  # reviewCard -> review-Card
        if _NOISE_COMPOUND.search(v):
            return True
        parts = {x for x in re.split(r"[-_\s:/.]+", v.lower()) if x}
        if parts & _NOISE_PARTS and not parts & _NOISE_SKIP:
            return True
    return False


def strip_noise(soup) -> None:
    """Remove testimonial / review / quote / star-rating / blog-byline blocks from a parsed page (in place).
    Never removes <html>/<body>/<main> or an element holding most of the page (a body class like 'has-testimonials')."""
    body = soup.body or soup
    total = len(body.get_text(" ", strip=True)) or 1
    big = lambda el: len(el.get_text(" ", strip=True)) > 1500 and len(el.get_text(" ", strip=True)) > 0.5 * total
    doomed = list(soup.find_all(["blockquote", "q", "cite"]))
    for el in soup.find_all(True):
        if el.name in ("html", "body", "main", "head") or any(el is d for d in doomed):
            continue
        if _noise_attr(el) and not big(el):
            doomed.append(el)
    for h in soup.find_all(_HEADINGS):
        if not _NOISE_HEADING.search(h.get_text(" ", strip=True)[:80]):
            continue
        par = h.parent
        if par is not None and par.name not in ("html", "body", "main") and len(par.find_all(_HEADINGS)) == 1 and not big(par):
            doomed.append(par)  # a section whose only heading is "What our patients say"
            continue
        doomed.append(h)
        level, n = int(h.name[1]), 0
        for sib in h.find_next_siblings():
            if sib.name in _HEADINGS and int(sib.name[1]) <= level:
                break
            doomed.append(sib)
            n += 1
            if n >= 12:
                break
    for el in doomed:
        try:
            if not getattr(el, "decomposed", False):
                el.decompose()
        except Exception:
            pass


_QUOTE_END = re.compile(r"[\u201c\u201d\"][^\u201c\u201d\"]{10,}[\u201c\u201d\"]?\s*[-\u2013\u2014\u2015~]{0,2}\s*(?:by\s+)?$")
_TESTI_NEAR = re.compile(
    r"testimonial|google\s+reviews?|verified\s+(?:patient|customer|buyer|client|reviewer)|reviewed\s+by|review\s+by|"
    r"\b[1-5](?:\.\d)?\s*(?:/\s*5\s*)?stars?\b|five[- ]star|posted\s+by|written\s+by|submitted\s+by|"
    r"\bauthor\s*:|\bwrote\b|\bby\s*$|\bcustomer\s+review|\bpatient\s+review|\bclient\s+review|\byelp\b", re.I)


def testimonial_context(text: str, start: int, end: int) -> bool:
    """True when the name at text[start:end] sits in a testimonial / review / quote attribution or a blog byline."""
    before, after = text[max(0, start - 160):start], text[end:end + 90]
    if _QUOTE_END.search(before):  # "great service ..." - Jane Doe
        return True
    if re.search(r"[\u2605\u2b50]", before[-40:] + after[:40]):
        return True
    return bool(_TESTI_NEAR.search(before[-90:] + " | " + after[:70]))


_PASSED = re.compile(r"passed away|in memory|in loving|the late\b|late founder|legacy of|grandfather|great[- ]grandfather|"
                     r"\b(?:his|her) (?:three |two |four )?(?:sons?|children|daughters?)\b", re.I)


def historical_context(text: str, start: int, end: int) -> bool:
    """'established by founder George Schaffer in 1953' / 'the late ...': a historical founder, not the current owner."""
    if _PASSED.search(text[max(0, start - 70):end + 60]):
        return True
    return bool(re.match(r"\W{0,6}(?:in\s+|since\s+|\()?(?:18\d\d|19[0-6]\d)\b", text[end:end + 18], re.I))


@lru_cache(maxsize=1024)
def final_site(url: str) -> str:
    """Where the website really ends up after redirects (final URL), '' when it cannot be resolved. One cheap request, cached."""
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    try:
        r = _session.head(url, allow_redirects=True, timeout=10)
        if r.status_code >= 400:
            r = _session.get(url, allow_redirects=True, timeout=12, stream=True)
            r.close()
        return r.url if r.status_code < 400 else ""
    except Exception:
        return ""


_DIR_SIGNS = [re.compile(p, re.I) for p in (
    r"find (?:a|your) (?:dentist|doctor|specialist|provider|lawyer|attorney|contractor|manufacturer|supplier|vendor)",
    r"\bdirector(?:y|ies)\b", r"claim (?:your|this) (?:profile|listing|business)", r"list your (?:practice|business|company)",
    r"browse (?:by )?(?:city|state|specialt|categor|location)", r"compare (?:dentists|providers|doctors|suppliers|vendors)",
    r"free for patients", r"\bsearch (?:our )?(?:database|listings)\b", r"top[- ]rated (?:dentists|providers)")]


def looks_like_directory(site: dict | None) -> bool:
    """A listing / marketplace site (several directory phrases on its pages), not a single business's own website."""
    if not site or not site.get("ok"):
        return False
    low = (site.get("title", "") + " " + site.get("text", "")).lower()
    return sum(1 for rx in _DIR_SIGNS if rx.search(low)) >= 2



def crawl(website: str, max_pages: int = 6) -> dict:
    """Home page + likely about/team/contact pages, robots.txt respected."""
    pages, html_all = {}, ""
    st, html = fetch(website, check_robots=True)
    if st != 200 or not html:
        return {"ok": False, "status": st}
    pages[website] = html
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(website).netloc
    links = []
    for a in soup.find_all("a", href=True):
        u = urljoin(website, a["href"]).split("#")[0]
        if urlparse(u).netloc == host and any(k in u.lower() for k in SUBPAGES) and u not in links:
            links.append(u)
    for u in links[: max_pages - 1]:
        s2, h2 = fetch(u, check_robots=True)
        if s2 == 200 and h2:
            pages[u] = h2
    texts, clean_texts, raw = [], [], []
    for u, h in pages.items():
        sp = BeautifulSoup(h, "html.parser")
        raw.append(h)
        for t in sp(["script", "style", "noscript"]):
            t.decompose()
        texts.append(sp.get_text(" ", strip=True))
        strip_noise(sp)  # testimonials / reviews / quotes / blog bylines: never a source of decision-makers
        clean_texts.append(sp.get_text(" ", strip=True))
    html_all, text = "\n".join(raw), "\n".join(texts)
    og = soup.find("meta", property="og:site_name")
    site_name = og["content"].strip() if og and og.get("content") else ""
    ttl = soup.title.get_text(" ", strip=True) if soup.title else ""
    return {"ok": True, "pages": list(pages), "text": text, "people_text": "\n".join(clean_texts), "html": html_all,
            "site_name": site_name, "title": ttl}


def derive_name(site: dict | None, domain: str) -> tuple[str, str]:
    """Company name from the site (og:site_name, <title>) or, last resort, the domain. Returns (name, how)."""
    site = site or {}
    if usable_name(site.get("site_name", "")):
        return site["site_name"].strip(), "site"
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower()) if domain else ""
    parts = [p.strip() for p in re.split(r"\s[|\-–:·•]\s", site.get("title", "")) if p.strip()]
    parts = [p for p in parts if usable_name(p) and len(p) <= 60]
    # prefer the title part that resembles the domain (a title is often "Page | Company")
    best = next((p for p in parts if label and label[:5] in re.sub(r"[^a-z0-9]", "", p.lower())), "")  # slogans do not count
    if best:
        return best, "site"
    return (domain.split(".")[0].replace("-", " ").title(), "domain") if domain else ("", "")


def extract(site: dict, domain: str, bad: set | frozenset = frozenset()) -> dict:
    text, html = site["text"], site["html"]
    ptext = site.get("people_text", text)  # same text without testimonial / review / quote / blog blocks
    emails = sorted({e.lower().rstrip(".") for e in EMAIL_RE.findall(html + " " + text)
                     if not any(j in e.lower() for j in JUNK_EMAIL)})
    own = [e for e in emails if domain and e.endswith("@" + domain)]
    phones = []
    for p in PHONE_RE.findall(text):
        n = us_phone(p)
        if n and n not in phones:
            phones.append(n)
    li_company = re.search(r"https?://(?:www\.)?linkedin\.com/company/[A-Za-z0-9_\-%.]+", html)
    people, seen = [], set()
    for rx in PERSON_RE:
        for m in rx.finditer(ptext):
            g = m.groups()
            if testimonial_context(ptext, m.start(), m.end()) or historical_context(ptext, m.start(), m.end()):
                continue
            name, title = (g[0], g[1]) if rx is PERSON_RE[0] else (g[1], g[0])
            words = set(name.lower().replace("dr. ", "").split())
            if name.lower() in seen or name.lower().replace("dr. ", "") in PLACEHOLDER_NAMES or len(name) > 40 or words & (STOPNAME | bad) or (FIRST_NAMES and name.replace("Dr. ", "").split()[0].lower() not in FIRST_NAMES):
                continue
            role = clean_title(title, strict=False)  # short role phrase only; never the raw matched text
            if not role:
                continue
            name = re.sub(r"['’]s$", "", name)
            seen.add(name.lower())
            people.append({"name": name, "title": role, "source": "website"})
    tech = [k for k, v in TECH.items() if v in html]
    hiring = any("career" in p or "jobs" in p for p in site["pages"]) or bool(re.search(r"we.?re hiring|join our team", text, re.I))
    return {"emails": (own or emails)[:5], "phones": phones[:3], "people": people[:10],
            "linkedin_company": li_company.group(0) if li_company else "", "tech": tech, "hiring": hiring}


INDUSTRY_TERMS = {  # extra verification keywords per industry (others use the words of the industry name)
    "manufacturing": ["manufacturer", "manufacturing", "fabrication", "machining", "cnc", "production", "factory",
                      "oem", "assembly", "custom parts"],
}


def industry_score(text: str, industry: str, extra_terms: list[str] | None = None) -> float:
    """Cheap verification: share of industry terms present. LLM check can replace this later."""
    terms = {w for w in re.findall(r"[a-z]{3,}", industry.lower())} | set(extra_terms or [])
    if industry.lower() in INDUSTRY_TERMS:
        terms = set(INDUSTRY_TERMS[industry.lower()]) | set(extra_terms or [])
    if not terms:
        return 0.0
    low = text.lower()
    hits = sum(1 for t in terms if t in low)
    density = min(1.0, sum(low.count(t) for t in terms) / 8)
    return round(0.6 * min(1.0, hits / min(len(terms), 3)) + 0.4 * density, 2)  # 3 matching terms = full hit share


_MX_CACHE: dict[str, bool] = {}


def has_mx(domain: str) -> bool:
    """MX records exist. Definitive answers are cached; a timeout / network error is retried once and never cached."""
    domain = (domain or "").lower()
    if domain in _MX_CACHE:
        return _MX_CACHE[domain]
    for _ in range(2):
        try:
            ok = bool(dns.resolver.resolve(domain, "MX", lifetime=8))
            _MX_CACHE[domain] = ok
            return ok
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            _MX_CACHE[domain] = False
            return False
        except Exception:
            continue
    return False


def guess_emails(name: str, domain: str) -> list[str]:
    parts = [p for p in re.sub(r"^Dr\.?\s", "", name).lower().replace("'", "").split() if len(p) > 1 or p.isalpha()]
    if len(parts) < 2 or not domain:
        return []
    f, l = parts[0].strip("."), parts[-1].strip(".")
    return [f"{f}@{domain}", f"{f}.{l}@{domain}", f"{f[0]}{l}@{domain}"]


def email_status(email: str, found_on_site: bool) -> str:
    """No SMTP probing in the POC (port 25 blocks, catch-alls, IP reputation)."""
    if not email:
        return ""
    if not has_mx(email.split("@")[1]):
        return "invalid (no MX)"
    return "found on website" if found_on_site else "pattern guess (MX ok, unverified)"
