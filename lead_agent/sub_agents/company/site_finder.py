"""Company website finding / verification and name helpers (moved from run.py)."""
import json
import re

from lead_agent.core.enrichment import crawl, industry_score
from lead_agent.core.utils import (CA_CITIES, CA_ZIP3, GENERIC_NAME_WORDS, US_STATES, city_point, find_places,
                                   is_directory, km_between, norm_city, norm_state, phone_regions, seo_title, usable_name)
from lead_agent.sub_agents.discovery.brands.openstreetmap import in_area
from lead_agent.sub_agents.discovery.brands.web_search import web_search


def norm_key(c: dict) -> str:
    return c.get("domain") or re.sub(r"[^a-z0-9]", "", c["company"].lower())[:30]


def merge(rows: list[dict]) -> list[dict]:
    """Dedupe across sources by domain or normalised name."""
    out: dict[str, dict] = {}
    for c in rows:
        k = norm_key(c)
        if not k:
            continue
        if k not in out:
            c["sources"] = {c["source"]}
            out[k] = c
            continue
        m = out[k]
        m["sources"].add(c["source"])
        for f in ("website", "domain", "phone", "address", "city", "state", "zip"):
            if not m.get(f) and c.get(f):
                m[f] = c[f]
        m["people"] += c.get("people", [])
    return list(out.values())


LEGAL = {"inc", "apc", "dds", "dmd", "pc", "pa", "corporation", "corp", "llc", "pllc", "llp", "ltd", "company", "the",
         "and", "dental", "md", "phd", "professional", "association", "incorporated"}
INDUSTRY_HINTS = {"dental": ("dental", "dentist", "dds", "dmd", "orthodont"), "law": ("attorney", "law firm", "lawyer"),
                  "manufacturing": ("manufactur", "machining", "fabricat")}


# Words that describe a business type, not a brand: a name/domain overlap on these alone proves nothing ("All Smile Dental").
WEAK_BRAND = {"all", "smile", "smiles", "dentistry", "dentists", "dentist", "orthodontics", "endodontics", "family", "metal",
              "metals", "steel", "precision", "machine", "machining", "machinery", "tool", "tools", "tooling", "plastic",
              "plastics", "fabrication", "fabricators", "fabricating", "engineering", "systems", "technology", "technologies",
              "design", "custom", "quality", "advanced", "advance", "general", "universal", "pacific", "western", "national",
              "first", "best", "pro", "premier", "elite", "express", "supply", "works", "associates", "partners"}


def strong_token(t: str) -> bool:
    return len(t) >= 3 and t not in WEAK_BRAND


def name_tokens(name: str) -> list[str]:
    """Distinctive words of a company name; legal suffixes (INC, APC, DDS, DENTAL CORPORATION...) removed."""
    return [t for t in re.findall(r"[a-z0-9]{3,}", (name or "").lower()) if t not in LEGAL and t not in GENERIC_NAME_WORDS]


def site_matches(c: dict, domain: str, site: dict | None, industry: str = "") -> bool:
    """Is this (non-directory) site the company's own? (a) name token in domain, (b) name tokens in title/og/first text,
    (c) person-named practice: owner's last name + city or industry word on the page."""
    toks = name_tokens(c["company"])
    dom = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    if (toks and toks[0] in dom and sum(t in dom for t in toks) >= (len(toks) + 1) // 2
            and any(strong_token(t) and t in dom for t in toks)):  # the brand word (first) must be in the domain  # half of the name's words ("Hugo's Screen Ironworks" != harkness-screens)
        return True
    if not site or not site.get("ok"):
        return False
    head = " ".join([site.get("title", ""), site.get("site_name", ""), site.get("text", "")[:2000]]).lower()
    if toks:
        title = " ".join([site.get("title", ""), site.get("site_name", "")]).lower()
        if toks[0] in title and sum(t in title for t in toks) >= min(2, len(toks)):  # brand word + one more
            return True
        top = (title + " " + site.get("text", "")[:300].lower())
        if len(toks) >= 2 and toks[0] in top and sum(t in top for t in toks) >= 2:  # one word in body text only proves a mention (press, magazine)
            return True
    owner = next((p["name"] for p in c.get("people", []) if p.get("name")), "")
    if not owner and re.search(r"\b(dds|dmd|d\.d\.s|md|attorney)\b", c["company"], re.I) and len(toks) >= 2:
        owner = " ".join(toks)  # "ALBERT G. LEUNG, DDS, INC" -> the person's name is the name itself
    last = (re.findall(r"[a-z]{3,}", owner.lower()) or [""])[-1]
    text = site.get("text", "").lower()
    if last and (last in dom or last in head or last in text):  # last name anywhere on the crawled pages
        hints = INDUSTRY_HINTS.get(industry.lower(), (industry.lower(),) if industry else ())
        city = (c.get("city") or "").lower()
        return bool((city and city in text) or any(h in text for h in hints if h))
    return False


METRO_RADIUS_KM = 60   # a metro = everything within 60 km of the requested city centre (LA: LA County's core + Orange County
                       # incl. Newport Beach at ~57 km; Fresno, Lancaster/Palmdale, Santa Barbara, Riverside = out)
ZIP_SLACK_KM = 30      # a 3-digit ZIP prefix is only a rough centre: inside-30 / beyond+30 is decisive, between -> geocode
_memo: dict = {}


def site_places(text: str) -> list[tuple[str, str]]:
    """(state, zip) pairs written on a page, e.g. 'Los Angeles, CA 90058' (US states only). Kept for old callers."""
    return [(p["state"], p["zip"]) for p in find_places(text) if p["zip"] and p["state"] in US_STATES][:12]


def place_in_area(where: dict | None, city: str = "", state: str = "", zip_code: str = "", offline_only: bool = False) -> bool | None:
    """Is one address inside the requested metro? True / False, None = cannot tell. State and Canada checks, a California
    city table and ZIP-prefix table work offline; anything else is geocoded (cached Nominatim) unless offline_only."""
    if not where:
        return None
    want, origin, radius = where["state"], where.get("origin"), where.get("radius_km", METRO_RADIUS_KM)
    if (state or "").upper() == "CANADA":
        return False
    st = norm_state(state)
    if st and st != want:
        return False  # other US state, Canada, Mexico ('??')
    if city and norm_city(city) == norm_city(where.get("city", "")):
        return True
    if not origin:
        return None
    z5 = re.match(r"\d{5}", (zip_code or "").strip())
    z5 = z5.group(0) if z5 else ""
    key = (origin, want, norm_city(city), z5, offline_only)
    if key in _memo:
        return _memo[key]
    verdicts, unresolved = [], False
    if want == "CA":
        pt = city_point(city) if city.strip() else None
        if pt:
            verdicts.append(km_between(origin, pt) <= radius)
        elif city.strip():
            unresolved = True
        zp = CA_ZIP3.get(z5[:3]) if z5 else None
        if zp:
            d = km_between(origin, zp)
            if d <= radius - ZIP_SLACK_KM:
                verdicts.append(True)
            elif d > radius + ZIP_SLACK_KM:
                verdicts.append(False)
            else:
                unresolved = True
        elif z5:
            unresolved = True
    else:
        unresolved = bool(city.strip() or z5)
    if any(verdicts):
        out = True
    elif verdicts and not unresolved:
        out = False
    elif not (city.strip() or z5) or offline_only:
        out = None  # nothing to look at / would need the network
    else:
        out = in_area(origin, want, city, st, z5)  # geocode (cached, throttled)
    _memo[key] = out
    return out


def schema_places(html: str) -> list[dict]:
    """schema.org PostalAddress blocks (JSON-LD) -> [{'city','state','zip'}]; Canadian provinces / country 'CA' -> state 'CANADA'."""
    out = []
    for blob in re.findall(r"<script[^>]+ld\+json[^>]*>(.*?)</script>", html or "", re.S | re.I):
        try:
            stack = [json.loads(blob)]
        except ValueError:
            continue
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack += d
            elif isinstance(d, dict):
                if any(k in d for k in ("addressLocality", "addressRegion", "postalCode")):
                    region = str(d.get("addressRegion", "")).strip()
                    country = d.get("addressCountry", "")
                    country = str(country.get("name", "") if isinstance(country, dict) else country).strip().upper()
                    reg = norm_state(region)
                    if (region.upper() in ("QC", "ON", "BC", "AB", "MB", "NB", "NS", "NL", "PE", "SK", "YT", "NT", "NU")
                            or country in ("CAN", "CANADA") or (country == "CA" and reg in ("", "??"))
                            or re.match(r"[A-Z]\d[A-Z]", str(d.get("postalCode", "")).strip().upper())):
                        reg = "CANADA"
                    if reg or d.get("postalCode") or d.get("addressLocality"):
                        out.append({"city": str(d.get("addressLocality", "")), "state": reg or "", "zip": str(d.get("postalCode", ""))[:5]})
                stack += [v for v in d.values() if isinstance(v, (dict, list))]
    return out[:6]


def _places_verdict(where: dict, places: list[dict]) -> bool | None:
    """True if any address is inside the metro; False if there are addresses and all of them are outside; else None."""
    places = places[:8]
    verdicts = []
    for pl in places:  # cheap offline decisions first, geocoding only if still undecided
        v = place_in_area(where, pl["city"], pl["state"], pl["zip"], offline_only=True)
        if v:
            return True
        verdicts.append(v)
    for i, pl in enumerate(places[:6]):
        if verdicts[i] is None:
            v = place_in_area(where, pl["city"], pl["state"], pl["zip"])
            if v:
                return True
            verdicts[i] = v
    return False if verdicts and not any(v is None for v in verdicts) else None


def site_location(where: dict | None, text: str, html: str = "") -> bool | None:
    """Where does the site say the business is? False = every address it shows (US 'City, ST ZIP', 'City, ST', Canadian
    postal codes, schema.org PostalAddress) is outside the metro, True = one of them is inside, None = no / unclear address.
    A site with several locations counts as inside when one location is."""
    if not where:
        return None
    return _places_verdict(where, schema_places(html) + find_places(text))


def assess_location(r: dict, where: dict | None, site: dict | None = None) -> tuple[bool | None, str]:
    """Final location decision for a finished row. Evidence: discovery address, website addresses (text + JSON-LD), phone area
    codes. (True, why) keep; (False, why) drop; (None, why) unverified -> keep and note "location unverified"."""
    if not where:
        return None, "no location filter"
    own = None
    if r.get("city") or r.get("state") or r.get("zip"):
        own = place_in_area(where, r.get("city", ""), r.get("state", ""), r.get("zip", ""))
    if own:
        return True, "discovery address in area"
    if site is None and r.get("website"):
        site = crawl(r["website"])  # HTTP cache: normally no new request
    text, html = (site.get("text", ""), site.get("html", "")) if site and site.get("ok") else ("", "")
    sv = site_location(where, text, html)
    if sv:
        return True, "website address in area"
    if sv is False:
        got = ", ".join(sorted({(p["city"] + " " + p["state"]).strip() for p in schema_places(html) + find_places(text)}))[:80]
        return False, f"website shows only other places ({got})"
    if own is False:
        return False, "discovery address out of area"
    regions = phone_regions((r.get("phone") or "") + " " + text)
    if regions and where["state"] not in regions and norm_city(where.get("city", "")) not in norm_city(text[:20000]):
        return False, f"phone area code(s) outside {where['state']} ({', '.join(regions)})"
    return None, "no address found"


GEO_GENERIC = {"industry", "commerce", "orange", "ontario", "corona", "vista", "bell", "walnut", "monterey", "riverside", "carson",
               "cypress", "eureka", "valencia", "compton", "paramount", "stanton", "lakeside", "hollywood", "la", "bonita"}
GEO_SUFFIX = {"sf": (37.775, -122.419), "nyc": (40.713, -74.006)}


def domain_geo_conflict(domain: str, where: dict | None) -> str:
    """The domain names another city ("fresnoplumbing.com", "allsmilessf.com") -> that city, else ''."""
    if not where or not domain:
        return ""
    label = re.sub(r"[^a-z]", "", domain.split(".")[0].lower())
    for name in CA_CITIES:
        n = name.replace(" ", "")
        if len(n) >= 6 and n not in GEO_GENERIC and n in label and place_in_area(where, name, "CA", "", offline_only=True) is False:
            return name
    for suf, pt in GEO_SUFFIX.items():
        if label.endswith(suf) and len(label) > len(suf) + 3 and where.get("origin") and km_between(where["origin"], pt) > 150:
            return suf
    return ""


DIRECTORY_PHRASES = ("browse the full directory", "provider profiles", "claim this listing", "claim your listing", "claim your profile",
                     "find a dentist", "find a doctor", "find a provider", "search the directory", "business directory",
                     "add your business", "get listed", "list your business", "write a review", "all 50 states",
                     "nearby businesses", "compare providers", "see all providers", "member directory", "directory of")


def looks_like_directory(site: dict | None) -> bool:
    """A listing / lead-gen portal rather than one business: directory phrases, 'directory' in its title, or addresses in many states."""
    if not site or not site.get("ok"):
        return False
    low = (site.get("text", "")[:12000]).lower()
    head = (site.get("title", "") + " " + site.get("site_name", "")).lower()
    if "directory" in head or "yellow pages" in head:
        return True
    if sum(p in low for p in DIRECTORY_PHRASES) >= 2:
        return True
    return len({p["state"] for p in find_places(site.get("text", "")) if p["state"] in US_STATES}) >= 6


def site_evidence(c: dict, text: str) -> str:
    """Hard proof the page belongs to this business: its phone number or street address / ZIP is on the page."""
    low = re.sub(r"\s+", " ", (text or "").lower())
    digits = re.sub(r"\D", "", text or "")
    ph = re.sub(r"\D", "", c.get("phone", ""))[-10:]
    if len(ph) == 10 and ph in digits:
        return "phone"
    street = (c.get("address") or "").lower().split()
    num = street[0] if street and street[0].isdigit() else ""
    word = next((w for w in street[1:] if w.isalpha() and len(w) > 3 and w not in ("north", "south", "east", "west")), "")
    if num and word and re.search(r"\b" + num + r"\b[^.]{0,30}" + re.escape(word), low):
        return "address"
    z = (c.get("zip") or "")[:5]
    if z.isdigit() and re.search(r"\b" + z + r"\b", low):
        return "zip"
    return ""


def verify_site(c: dict, domain: str, site: dict | None, industry: str = "", where: dict | None = None) -> tuple[bool, str]:
    """Is this site this business's own? Phone / address / ZIP on the page, else the name rule; a site that shows only
    other cities (or Canada) is rejected; a directory / listing portal is rejected; an unreachable site is accepted on a
    strong name match only; non-search sources also need the industry on the page."""
    if is_directory(domain):
        return False, "directory domain"
    if not site or not site.get("ok"):
        if where is not None and domain_geo_conflict(domain, where):
            return False, f"domain names another city ({domain_geo_conflict(domain, where)})"
        return site_matches(c, domain, None, industry), "not reachable"
    text = site.get("text", "")
    if looks_like_directory(site):
        return False, "directory / listing site"
    if where is None:  # client file / old pipeline: only the name rule
        return site_matches(c, domain, site, industry), "name"
    ev = site_evidence(c, text)
    if ev:
        return True, ev
    if site_location(where, text, site.get("html", "")) is False:
        return False, "site shows another location"
    if c["sources"] == {"ddg"}:
        return True, "search result"  # the site itself is the candidate
    if not site_matches(c, domain, site, industry):
        return False, "name/phone/address not on site"
    if industry_score(text, industry) < 0.3:
        return False, "site is not " + industry
    return True, "name"


def find_site(c: dict, industry: str = "", where: dict | None = None) -> None:
    """Company has no website yet: search once by name + city, accept a candidate only if it is the company's own site."""
    if c.get("website") or not c.get("company", "").strip():
        return
    loc = f'{c.get("city", "")} {c.get("state", "")}'.strip()
    q2 = " ".join(name_tokens(c["company"]) + [industry, c.get("state", "")]).strip()  # looser: no quotes, no city
    hits = web_search(c["company"], loc, 5, queries=(f'"{c["company"]}" {loc}', q2))  # two queries per company
    cands = [h for h in hits if not is_directory(h["domain"]) and h["domain"] not in c.get("rejected", ())]
    cands.sort(key=lambda h: not site_matches(c, h["domain"], None, industry))  # domain looks right: try first
    for h in cands[:5]:
        ok, _ = verify_site(c, h["domain"], crawl(h["website"]), industry, where)
        if ok:
            c["website"], c["domain"], c["site_accepted"] = h["website"], h["domain"], True
            return


def brand_name(site: dict | None, domain: str, industry: str = "", city: str = "") -> tuple[str, str]:
    """Business name from og:site_name, schema.org, a non-SEO <title> segment, else the domain. Never a search phrase."""
    site = site or {}
    ok = lambda n: bool(n) and usable_name(n) and len(n) <= 60 and not seo_title(n, industry, city)
    if ok(site.get("site_name", "")):
        return site["site_name"].strip(), "site"
    for blob in re.findall(r"<script[^>]+ld\+json[^>]*>(.*?)</script>", site.get("html", ""), re.S | re.I):
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack += d
            elif isinstance(d, dict):
                if re.search(r"Organization|Business|Dentist|Corporation|Store|Attorney|Manufactur", str(d.get("@type", "")))                         and ok(str(d.get("name", ""))):
                    return d["name"].strip(), "schema"
                stack += [v for v in d.values() if isinstance(v, (dict, list))]
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower()) if domain else ""
    for part in re.split(r"\s[|\-–:·•]\s", site.get("title", "")):  # a title is "Page | Brand" or "Brand - slogan"
        part = part.strip()
        if label and ok(part) and label[:5] in re.sub(r"[^a-z0-9]", "", part.lower()):
            return part, "site"
    return (domain.split(".")[0].replace("-", " ").title(), "domain") if domain else ("", "")


def pick_name(c: dict, site: dict | None, industry: str = "") -> tuple[str, str]:
    """Registry / map names are kept; a name taken from a search-result title is replaced when it is a page title / SEO phrase."""
    cur, city = c.get("company", ""), c.get("city", "")
    bad = not usable_name(cur) or "." in cur or seo_title(cur, industry, city)
    if not (bad or c["sources"] == {"ddg"}):
        return cur, ""
    name, how = brand_name(site, c.get("domain", ""), industry, city)
    return (cur, "") if how == "domain" and not bad else (name or cur, how)


OTHER_SECTOR = {"construction", "contractor", "contractors", "roofing", "realty", "restaurant", "salon", "church",
                "landscaping", "plumbing", "insurance"}


# "Looks like the industry but is not": per industry, phrases that mark a different kind of business. A hit only drops the row
# when the site shows no real production evidence (2+ distinct POSITIVE terms), so manufacturers that also distribute survive.
WB, WE = r"\b(?:", r")\b"  # every phrase below is matched as a whole word / phrase
INDUSTRY_NEGATIVE = {
    "manufacturing": [
        (r"manufacturer(?:s|\W{1,3}s)?\W{0,3}\s*rep(?:resentatives?|s)?", "manufacturers' representative"),
        (r"sales (?:agency|agencies)|independent (?:sales )?rep(?:resentative)?s?|sales rep(?:resentative)?s? (?:firm|agency|company|group)",
         "sales agency / rep"),
        (r"line cards?|representing[^.]{0,60}manufacturers", "represents other manufacturers"),
        (r"(?:authorized |exclusive |stocking |master )?distributors? (?:of|for)|we are (?:a|an|the)[^.]{0,30}distributor",
         "distributor"),
        (r"wholesale (?:only|distributor|supplier|club)", "wholesale only"),
        (r"retail (?:store|shop|showroom)|add to cart", "retail store"),
        (r"(?:auto|car|truck|motorcycle|bike|computer|phone|appliance|watch|shoe|tv|furniture)\s+repair|repair shop", "repair shop"),
        (r"(?:brewery|brewpub|taproom|tasting room|winery|vineyard)", "brewery / winery"),
    ],
}
MFG_POSITIVE = [r"we (?:manufacture|fabricate|machine|produce|build)", r"our (?:manufacturing|machine|fabrication|production) "
                r"(?:facility|plant|shop|capabilit\w+|process\w*)", r"cnc", r"machine shop", r"injection (?:molding|moulding)",
                r"sheet metal", r"stamping", r"welding", r"foundry", r"in-house (?:manufacturing|production|fabrication)",
                r"contract manufactur\w+", r"manufacturing facility", r"manufactured in", r"our factory", r"machining",
                r"fabrication shop", r"assembly line", r"fabricat\w+", r"laser cutting|plasma cutting|waterjet|press brake|saw cutting|powder coat\w*",
                r"manufactur(?:er|ers|ing) of"]
NEGATIVE_NAME = re.compile(WB + r"sales (?:agency|rep\w*)|reps?|representatives?|distribut(?:ing|ors?|ion)|wholesale|brewing|brewery|"
                           r"winery|vineyards?|repair" + WE, re.I)


def industry_negative(text: str, industry: str, name: str = "") -> str:
    """Reason ('manufacturers' representative', 'brewery / winery', ...) when the site / name says the business is a different
    kind of company than the requested industry, and the site shows no real production evidence; '' otherwise."""
    rules = INDUSTRY_NEGATIVE.get((industry or "").lower())
    if not rules:
        return ""
    low = (text or "")[:15000].lower()
    hit = next((why for rx, why in rules if re.search(WB + rx + WE, low)), "")
    if not hit and name and NEGATIVE_NAME.search(name):
        hit = "name: " + NEGATIVE_NAME.search(name).group(0).lower()
    if not hit:
        return ""
    positives = sum(bool(re.search(WB + rx + WE, low)) for rx in MFG_POSITIVE)
    return hit if positives < 2 else ""


def industry_off(r: dict, industry: str, site_text: str = "") -> bool:
    """Row is not in the requested industry. Registry / map rows keep their category (only a name of another sector with
    no support on the site drops them); search rows need the industry on their own site. With the site text (optional) the
    negative keywords of the industry (manufacturers' rep, distributor only, brewery...) also drop a row."""
    raw = r.get("_raw_score", r["industry_match"])
    other = set(re.findall(r"[a-z]+", r["company"].lower())) & OTHER_SECTOR - set(re.findall(r"[a-z]+", industry.lower()))
    if other and raw < 0.5:
        return True
    if raw < 0.8 and industry_negative(site_text, industry, r["company"]):
        return True  # raw >= 0.8: the site itself is full of industry terms, trust it
    return not ({"npi", "osm"} & set(r["sources"].split("+"))) and raw < 0.3


def same_domain(email: str, domain: str) -> bool:
    d = email.split("@")[-1].lower()
    return bool(domain) and (d == domain.lower() or d.endswith("." + domain.lower()))


GUESS_LABEL = "guess (unverified)"
