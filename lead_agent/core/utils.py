"""Shared helpers: polite HTTP with SQLite cache, robots.txt, US formatting."""
import hashlib
import math
import re
import sqlite3
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests

from lead_agent.config.settings import CACHE_DB

UA = "Mozilla/5.0 (compatible; LeadAgentPOC/0.1; research use)"
DB_PATH = CACHE_DB
DB_PATH.parent.mkdir(exist_ok=True)

_db = sqlite3.connect(DB_PATH)
_db.execute("CREATE TABLE IF NOT EXISTS http (k TEXT PRIMARY KEY, status INT, body TEXT, ts REAL)")
_session = requests.Session()
_session.headers["User-Agent"] = UA
_last_hit: dict[str, float] = {}
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def _throttle(host: str, gap: float) -> None:
    wait = _last_hit.get(host, 0) + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()


def allowed(url: str) -> bool:
    """Respect robots.txt for website crawling."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = _session.get(base + "/robots.txt", timeout=10)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            rp = None
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(UA, url)


def fetch(url: str, params: dict | None = None, gap: float = 1.5, max_age_days: float = 14,
          check_robots: bool = False, timeout: int = 20, data: dict | None = None, headers: dict | None = None) -> tuple[int, str]:
    """GET with cache + per-host throttle. Returns (status, text); status 0 on network error."""
    key = hashlib.sha1((url + repr(sorted((params or data or {}).items()))).encode()).hexdigest()
    row = _db.execute("SELECT status, body, ts FROM http WHERE k=?", (key,)).fetchone()
    if row and time.time() - row[2] < max_age_days * 86400:
        return row[0], row[1]
    if check_robots and not allowed(url):
        return 999, ""
    _throttle(urlparse(url).netloc, gap)
    try:
        r = _session.post(url, data=data, timeout=timeout, headers=headers) if data else _session.get(url, params=params, timeout=timeout, headers=headers)
        status, body = r.status_code, r.text if "text" in r.headers.get("content-type", "") or "json" in r.headers.get("content-type", "") else ""
    except requests.RequestException:
        return 0, ""
    if status != 200:
        return status, body
    _db.execute("REPLACE INTO http VALUES (?,?,?,?)", (key, status, body, time.time()))
    _db.commit()
    return status, body


def domain_of(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    return urlparse(url).netloc.lower().removeprefix("www.")


GENERIC_NAME_WORDS = {"the", "of", "and", "inc", "llc", "pllc", "ltd", "corp", "corporation", "company", "co", "pa", "pc",
                      "group", "law", "firm", "dental", "care", "center", "home", "welcome", "index", "official", "site",
                      "manufacturing", "manufacturer", "manufacturers", "products", "industries", "industrial", "services",
                      "solutions", "international", "usa", "america", "american", "los", "angeles", "california"}


def usable_name(name: str) -> bool:
    """True when a company name is distinctive enough to search a person/company with (not empty, not only stop words)."""
    n = (name or "").strip()
    if re.search(r"\.(com|net|org|co|us|io|biz)\b", n.lower()):  # a bare domain is not a company name
        return False
    return len(n) >= 3 and any(t not in GENERIC_NAME_WORDS for t in re.findall(r"[a-z0-9]{3,}", n.lower()))


# Directories / aggregators / listings: never a company's own website, never an email domain.
DIRECTORY_DOMAINS = (
    "yelp.", "yellowpages.", "facebook.", "linkedin.", "bbb.org", "mapquest.", "tripadvisor.", "instagram.", "angi.",
    "thumbtack.", "indeed.", "glassdoor.", "zocdoc.", "healthgrades.", "wikipedia.", "usnews.", "lawyers.com",
    "bestlawyers", "bestlawfirms", "justia.", "avvo.", "findlaw.", "martindale.", "superlawyers.", "goodfirms.",
    "clutch.co", "bcgsearch.", "expertise.com", "manta.", "chamberofcommerce.", "reddit.", "youtube.", "nextdoor.",
    "google.", "bing.", "apple.com", "builtin", "f6s.", "factoryregistry.", "thomasnet.", "dnb.", "zoominfo.",
    "crunchbase.", "ensun.", "industrynet.", "opencorporates.",
    # health / business listing sites
    "npino", "npiprofile", "npidb", "npiregistry", "webmd.", "vitals.", "sharecare.", "bizprofile", "allbiz.",
    "bizapedia", "dental.me", "1-800-dentist", "ratemds.", "caredash.", "wellness.com", "doximity.", "doctor.com",
    "topdentists", "findadentist", "ada.org", "superpages.", "citysearch.", "merchantcircle.", "buzzfile.",
    "dandb.", "birdeye.", "porch.com", "houzz.", "datanyze.", "rocketreach.", "apollo.io", "cylex", "hotfrog.",
    "foursquare.", "alignable.", "yahoo.", "bloomberg.", "pitchbook.", "owler.", "cbinsights.", "naics",
    "mapquest", "yellowbook", "local.com", "showmelocal", "chamberofcommerce", "us-business", "bizstanding",
    "corporationwiki", "companyreg", "company-information", "kompass.", "alibaba.", "made-in-china", "globalspec.",
    "makeitla", "dentistry.com", "yourdentistryguide", "toothpickdental", "1800dentist", "dentalinsurance",
    "dentalplans", "trustedratings", "askallegiance", "trustpilot.", "sitejabber.", "consumeraffairs.", "thebluebook", "opennpi", "qrrx", "dentalvitals", "ehealthscores", "nationalprovider", "medicarelist", "opengov", "myhygienist", "healthcare6", "npidirectory", "docinfo", "usa.com", "medicalprovider", "providerdirect",
    # week-4 retest: lead-gen / city directories that were accepted as a company's own site
    "myspecialtydentist", "losangelesdirect", "directory", "directories", "citysquares", "localdatabase", "brownbook",
    "yellowbot", "whitepages.", "spoke.com", "buzzfile", "americantowns", "cityfos", "bizjournals.", "industrialquick",
)

# Site-builder hosts: "name.wixsite.com" is a business's own (free) site, any other "slug.parent.tld" is a listing page.
SITE_BUILDER_HOSTS = ("wixsite.", "weebly.", "squarespace.", "godaddysites.", "business.site", "wordpress.com", "blogspot.",
                      "webflow.io", "myshopify.", "netlify.app", "vercel.app", "github.io", "carrd.co", "jimdo", "strikingly.")


def is_directory(domain: str) -> bool:
    """Directory / listing / aggregator domain. Also a business slug hosted under somebody else's domain
    ("miguels-sheet-metal.losangelesdirect.us"), which is a listing page, not the company's own site."""
    d = (domain or "").lower().removeprefix("www.")
    if any(s in d for s in DIRECTORY_DOMAINS):
        return True
    labels = d.split(".")
    base = 3 if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net", "gov", "edu") and len(labels[-1]) == 2 else 2
    if len(labels) > base and "-" in labels[0] and not any(h in d for h in SITE_BUILDER_HOSTS):
        return True
    return False


def name_matches_site(name: str, domain: str, title: str = "") -> bool:
    """A distinctive word of the company name is in the domain or in the page title."""
    toks = [t for t in re.findall(r"[a-z0-9]{3,}", (name or "").lower()) if t not in GENERIC_NAME_WORDS]
    dom = re.sub(r"[^a-z0-9]", "", (domain or "").split(".")[0].lower())
    low = (title or "").lower()
    return any(t in dom or t in low for t in toks)


SECTOR_WORDS = r"manufactur\w*|fabricat\w*|machin\w*|molding|injection|cnc|sheet metal|contract|dental|dentist\w*|attorneys?|law firm"


def seo_title(name: str, industry: str = "", city: str = "") -> bool:
    """True when a 'company name' is really a page title / search phrase ("Metal Fabrication In Los Angeles, California")."""
    low = (name or "").lower()
    city = (city or "").lower().strip()
    sector = SECTOR_WORDS + "".join("|" + re.escape(w) for w in re.findall(r"[a-z]{4,}", (industry or "").lower()))
    if "|" in low or re.search(r"\b(best|near me|serving|top[- ]rated|#1|free quote|call now|welcome to|get a quote|affordable)\b", low):
        return True
    if city and re.search(r"\b(in|near|serving|around)\s+" + re.escape(city), low):
        return True
    if city and city in low and re.search(sector, low):
        return True
    return bool(re.search(r"\b(services?|company|companies|manufacturers?)\s+in\s+[a-z]", low)
                or (re.search(r"\bservices\b", low) and re.search(sector, low)))


STATE_NAMES = {n.upper(): c for c, n in (  # full name -> code, so "California" and "CA" compare equal
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"), ("CA", "California"), ("CO", "Colorado"),
    ("CT", "Connecticut"), ("DE", "Delaware"), ("DC", "District of Columbia"), ("FL", "Florida"), ("GA", "Georgia"),
    ("HI", "Hawaii"), ("ID", "Idaho"), ("IL", "Illinois"), ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"),
    ("KY", "Kentucky"), ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"), ("MA", "Massachusetts"),
    ("MI", "Michigan"), ("MN", "Minnesota"), ("MS", "Mississippi"), ("MO", "Missouri"), ("MT", "Montana"),
    ("NE", "Nebraska"), ("NV", "Nevada"), ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"),
    ("NY", "New York"), ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"), ("OK", "Oklahoma"),
    ("OR", "Oregon"), ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"), ("SD", "South Dakota"),
    ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"), ("VT", "Vermont"), ("VA", "Virginia"), ("WA", "Washington"),
    ("WV", "West Virginia"), ("WI", "Wisconsin"), ("WY", "Wyoming"))}


def norm_state(state: str) -> str:
    """'' when unknown, else a 2-letter US code; anything that is not a US state ('BAJA CALIFORNIA', 'ON') -> '??'."""
    s = (state or "").strip().upper().rstrip(".")
    if not s:
        return ""
    if len(s) == 2 and s in US_STATES:
        return s
    return STATE_NAMES.get(s, "??")


def km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def us_phone(raw: str) -> str:
    """Normalise to (XXX) XXX-XXXX; empty if not a valid US number."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    if len(d) != 10 or d[0] in "01":
        return ""
    return f"({d[:3]}) {d[3:6]}-{d[6:]}"


US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}


# ---------------------------------------------------------------------------------------------------------------------
# Offline location evidence (no geocoding needed for the common cases): US/Canada area codes, California cities and ZIP
# prefixes, "City, ST ZIP" address extraction, Canadian postal codes.
# ---------------------------------------------------------------------------------------------------------------------
TOLL_FREE = {"800", "833", "844", "855", "866", "877", "888"}
CANADA_AREA_CODES = set("""204 226 236 249 250 263 289 306 343 354 365 367 368 382 403 416 418 428 431 437 438 450 468 474 506
514 519 548 579 581 584 587 604 613 639 647 672 683 705 709 742 778 780 782 807 819 825 867 873 879 902 905""".split())
_AREA = {  # state -> area codes (NANP geographic codes)
    "AL": "205 251 256 334 659 938", "AK": "907", "AZ": "480 520 602 623 928", "AR": "479 501 870",
    "CA": "209 213 279 310 323 341 350 369 408 415 424 442 510 530 559 562 619 626 628 650 657 661 669 707 714 747 760 805 "
          "818 820 831 840 858 909 916 925 935 949 951",
    "CO": "303 719 720 970 983", "CT": "203 475 860 959", "DE": "302", "DC": "202 771",
    "FL": "239 305 321 352 386 407 561 689 727 754 772 786 813 850 863 904 941 954",
    "GA": "229 404 470 478 678 706 762 770 912", "HI": "808", "ID": "208 986",
    "IL": "217 224 309 312 331 618 630 708 773 779 815 847 872", "IN": "219 260 317 463 574 765 812 930",
    "IA": "319 515 563 641 712", "KS": "316 620 785 913", "KY": "270 364 502 606 859", "LA": "225 318 337 504 985",
    "ME": "207", "MD": "240 301 410 443 667", "MA": "339 351 413 508 617 774 781 857 978",
    "MI": "231 248 269 313 517 586 616 734 810 906 947 989", "MN": "218 320 507 612 651 763 952",
    "MS": "228 601 662 769", "MO": "314 417 573 636 660 816 975", "MT": "406", "NE": "308 402 531", "NV": "702 725 775",
    "NH": "603", "NJ": "201 551 609 640 732 848 856 862 908 973", "NM": "505 575",
    "NY": "212 315 332 347 363 516 518 585 607 631 646 680 716 718 838 845 914 917 929 934",
    "NC": "252 336 704 743 828 910 919 980 984", "ND": "701", "OH": "216 220 234 326 330 380 419 440 513 567 614 740 937",
    "OK": "405 539 572 580 918", "OR": "458 503 541 971", "PA": "215 223 267 272 412 445 484 570 610 717 724 814 835 878",
    "RI": "401", "SC": "803 821 839 843 854 864", "SD": "605", "TN": "423 615 629 731 865 901 931",
    "TX": "210 214 254 281 325 346 361 409 430 432 469 512 682 713 726 737 806 817 830 832 903 915 936 940 956 972 979",
    "UT": "385 435 801", "VT": "802", "VA": "276 434 540 571 703 757 804", "WA": "206 253 360 425 509 564",
    "WV": "304 681", "WI": "262 274 414 534 608 715 920", "WY": "307",
}
AREA_CODE_STATE = {code: st for st, codes in _AREA.items() for code in codes.split()}
PHONE_RX = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)")


def area_region(code: str) -> str:
    """'CA' / 'MO' (US state), 'CANADA', or '' (toll free / unknown)."""
    if code in CANADA_AREA_CODES:
        return "CANADA"
    return AREA_CODE_STATE.get(code, "")


def phone_regions(text: str, limit: int = 8) -> list[str]:
    """Regions (state codes / CANADA) of the geographic phone numbers written in a text, order kept, de-duplicated."""
    out: list[str] = []
    for m in PHONE_RX.finditer(text or ""):
        r = area_region(m.group(1))
        if r and r not in out:
            out.append(r)
        if len(out) >= limit:
            break
    return out


# City -> (lat, lon), California (SoCal in detail, the rest as far-away markers). Lower case, no punctuation.
CA_CITIES = {
    "los angeles": (34.054, -118.243), "la": (34.054, -118.243), "long beach": (33.770, -118.194), "pasadena": (34.148, -118.145),
    "glendale": (34.142, -118.255), "burbank": (34.181, -118.309), "santa monica": (34.020, -118.491),
    "torrance": (33.836, -118.341), "inglewood": (33.962, -118.353), "compton": (33.896, -118.220), "downey": (33.940, -118.133),
    "pomona": (34.055, -117.752), "el monte": (34.069, -118.027), "west covina": (34.069, -117.939), "norwalk": (33.902, -118.082),
    "carson": (33.831, -118.282), "gardena": (33.888, -118.309), "hawthorne": (33.916, -118.353),
    "santa fe springs": (33.947, -118.085), "commerce": (34.000, -118.160), "vernon": (34.004, -118.230),
    "south gate": (33.955, -118.212), "huntington park": (33.982, -118.225), "bell": (33.978, -118.187),
    "montebello": (34.009, -118.105), "monterey park": (34.063, -118.123), "alhambra": (34.095, -118.127),
    "san gabriel": (34.096, -118.106), "arcadia": (34.140, -118.035), "azusa": (34.134, -117.908), "irwindale": (34.107, -117.935),
    "baldwin park": (34.085, -117.961), "la puente": (34.020, -117.949), "city of industry": (34.019, -117.959),
    "industry": (34.019, -117.959), "covina": (34.090, -117.890), "glendora": (34.136, -117.865), "claremont": (34.097, -117.719),
    "la verne": (34.100, -117.768), "diamond bar": (34.029, -117.811), "walnut": (34.020, -117.865), "san dimas": (34.107, -117.807),
    "chatsworth": (34.257, -118.601), "northridge": (34.228, -118.536), "van nuys": (34.187, -118.451),
    "sherman oaks": (34.151, -118.449), "woodland hills": (34.168, -118.605), "canoga park": (34.201, -118.598),
    "sun valley": (34.217, -118.370), "pacoima": (34.264, -118.417), "sylmar": (34.308, -118.449), "san fernando": (34.282, -118.439),
    "north hollywood": (34.172, -118.378), "panorama city": (34.225, -118.449), "reseda": (34.201, -118.536),
    "tarzana": (34.173, -118.553), "encino": (34.159, -118.501), "calabasas": (34.138, -118.660), "agoura hills": (34.153, -118.761),
    "westlake village": (34.146, -118.806), "santa clarita": (34.392, -118.542), "valencia": (34.444, -118.600),
    "lancaster": (34.698, -118.137), "palmdale": (34.579, -118.117), "el segundo": (33.919, -118.416),
    "manhattan beach": (33.885, -118.411), "redondo beach": (33.849, -118.388), "hermosa beach": (33.862, -118.399),
    "culver city": (34.021, -118.396), "beverly hills": (34.074, -118.400), "west hollywood": (34.090, -118.361),
    "hollywood": (34.098, -118.327), "lakewood": (33.854, -118.133), "bellflower": (33.882, -118.117), "paramount": (33.889, -118.160),
    "cerritos": (33.858, -118.065), "artesia": (33.866, -118.083), "whittier": (33.979, -118.033), "la mirada": (33.917, -118.012),
    "pico rivera": (33.983, -118.097), "rosemead": (34.081, -118.073), "temple city": (34.107, -118.058),
    "south pasadena": (34.116, -118.150), "altadena": (34.190, -118.131), "monrovia": (34.148, -117.999), "duarte": (34.139, -117.977),
    "rancho dominguez": (33.893, -118.222), "wilmington": (33.779, -118.263), "san pedro": (33.736, -118.292),
    "harbor city": (33.791, -118.298), "lomita": (33.792, -118.315), "lynwood": (33.931, -118.211), "south el monte": (34.052, -118.047),
    "rowland heights": (33.976, -117.905), "hacienda heights": (33.993, -117.969), "la canada flintridge": (34.200, -118.200),
    # Orange County
    "anaheim": (33.836, -117.914), "santa ana": (33.746, -117.868), "irvine": (33.684, -117.827), "huntington beach": (33.660, -117.999),
    "garden grove": (33.774, -117.941), "orange": (33.788, -117.853), "fullerton": (33.870, -117.925), "costa mesa": (33.641, -117.919),
    "newport beach": (33.617, -117.929), "brea": (33.917, -117.900), "placentia": (33.872, -117.870), "yorba linda": (33.889, -117.813),
    "buena park": (33.867, -117.998), "cypress": (33.817, -118.037), "la habra": (33.932, -117.946), "tustin": (33.746, -117.826),
    "fountain valley": (33.709, -117.954), "westminster": (33.751, -117.994), "stanton": (33.802, -117.994),
    "seal beach": (33.741, -118.105), "los alamitos": (33.803, -118.073), "laguna beach": (33.542, -117.783),
    "laguna hills": (33.613, -117.712), "laguna niguel": (33.523, -117.707), "lake forest": (33.647, -117.689),
    "mission viejo": (33.600, -117.672), "san clemente": (33.427, -117.612), "dana point": (33.467, -117.698),
    "aliso viejo": (33.576, -117.726), "rancho santa margarita": (33.640, -117.603), "san juan capistrano": (33.501, -117.663),
    # Inland Empire / Ventura
    "ontario": (34.063, -117.651), "rancho cucamonga": (34.106, -117.593), "chino": (34.012, -117.689), "chino hills": (33.994, -117.759),
    "upland": (34.098, -117.648), "fontana": (34.092, -117.435), "san bernardino": (34.108, -117.289), "riverside": (33.953, -117.396),
    "corona": (33.875, -117.566), "norco": (33.931, -117.549), "moreno valley": (33.943, -117.230), "temecula": (33.494, -117.148),
    "murrieta": (33.554, -117.214), "jurupa valley": (34.000, -117.470), "mira loma": (33.990, -117.520), "redlands": (34.056, -117.183),
    "palm springs": (33.830, -116.545), "oxnard": (34.198, -119.177), "ventura": (34.275, -119.229), "thousand oaks": (34.171, -118.838),
    "simi valley": (34.269, -118.781), "camarillo": (34.216, -119.038), "moorpark": (34.286, -118.882), "santa paula": (34.354, -119.059),
    # San Diego County
    "san diego": (32.716, -117.161), "escondido": (33.119, -117.086), "carlsbad": (33.158, -117.351), "oceanside": (33.196, -117.379),
    "chula vista": (32.640, -117.084), "el cajon": (32.795, -116.963), "vista": (33.200, -117.243), "san marcos": (33.144, -117.166),
    "la jolla": (32.847, -117.274), "santee": (32.838, -116.974), "poway": (32.963, -117.036), "encinitas": (33.037, -117.292),
    "national city": (32.678, -117.099), "la mesa": (32.768, -117.023), "lemon grove": (32.743, -117.031), "bonita": (32.659, -117.031),
    "spring valley": (32.745, -117.011), "imperial beach": (32.584, -117.113), "coronado": (32.686, -117.183),
    "solana beach": (32.991, -117.271), "del mar": (32.960, -117.264), "lakeside": (32.857, -116.922), "ramona": (33.042, -116.869),
    "fallbrook": (33.376, -117.251),
    # elsewhere in California (all far from SoCal metros; used to say "out")
    "fresno": (36.738, -119.787), "clovis": (36.826, -119.703), "visalia": (36.330, -119.292), "bakersfield": (35.373, -119.019),
    "sacramento": (38.582, -121.494), "san francisco": (37.775, -122.419), "oakland": (37.804, -122.271), "san jose": (37.339, -121.895),
    "santa barbara": (34.421, -119.698), "san luis obispo": (35.283, -120.659), "stockton": (37.958, -121.291),
    "modesto": (37.639, -120.997), "santa rosa": (38.440, -122.714), "fremont": (37.549, -121.989), "hayward": (37.669, -122.081),
    "santa clara": (37.354, -121.955), "sunnyvale": (37.369, -122.036), "palo alto": (37.442, -122.143), "redding": (40.587, -122.392),
    "eureka": (40.802, -124.164), "monterey": (36.600, -121.895), "salinas": (36.678, -121.655), "santa cruz": (36.974, -122.030),
    "merced": (37.302, -120.483), "chico": (39.729, -121.837), "el centro": (32.792, -115.563), "barstow": (34.899, -117.023),
    "victorville": (34.536, -117.291), "hesperia": (34.426, -117.301),
}
# California ZIP 3-digit prefixes -> approximate centre (a prefix area is ~15-30 km across: use it with a slack margin).
CA_ZIP3 = {
    "900": (34.00, -118.30), "901": (34.05, -118.25), "902": (33.96, -118.35), "903": (33.96, -118.35), "904": (34.02, -118.49),
    "905": (33.84, -118.34), "906": (33.93, -118.08), "907": (33.78, -118.15), "908": (33.77, -118.19), "910": (34.15, -118.13),
    "911": (34.15, -118.13), "912": (34.15, -118.25), "913": (34.20, -118.60), "914": (34.19, -118.45), "915": (34.18, -118.31),
    "916": (34.17, -118.38), "917": (34.00, -117.85), "918": (34.09, -118.12), "919": (32.70, -117.00), "920": (33.10, -117.20),
    "921": (32.80, -117.15), "922": (33.80, -116.40), "923": (34.10, -117.30), "924": (34.10, -117.30), "925": (33.90, -117.40),
    "926": (33.70, -117.85), "927": (33.75, -117.85), "928": (33.83, -117.90), "930": (34.20, -119.15), "931": (34.43, -119.70),
    "932": (36.30, -119.30), "933": (35.37, -119.00), "934": (34.90, -120.40), "935": (35.00, -118.00), "936": (36.70, -119.80),
    "937": (36.70, -119.80), "938": (36.70, -119.80), "939": (36.67, -121.65),
}
for _z in range(940, 962):  # northern California: everything is > 300 km from the SoCal metros
    CA_ZIP3.setdefault(str(_z), (38.0, -122.0))

_ST_ABBR = "|".join(sorted(US_STATES))
_ST_NAMES = "|".join(sorted((n.lower() for n in STATE_NAMES), key=len, reverse=True))
_CITY = r"((?:[A-Z][A-Za-z'.\-]+\s+){0,2}[A-Z][A-Za-z'.\-]+)"
PLACE_ZIP_RX = re.compile(_CITY + r"\s*[,.]?\s+(" + _ST_ABBR + r"|(?i:" + _ST_NAMES + r"))\.?,?\s+(\d{5})(?:-\d{4})?\b")
PLACE_COMMA_RX = re.compile(_CITY + r"\s*,\s+(" + _ST_ABBR + r")\b(?!\.\w)")
CA_POSTAL = r"[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z][ -]?\d[ABCEGHJ-NPRSTV-Z]\d"
PROVINCES = r"QC|ON|BC|AB|MB|NB|NS|NL|PE|SK|YT|NT|NU|Quebec|Ontario|British Columbia|Alberta|Manitoba|Saskatchewan|Nova Scotia|New Brunswick"
FOREIGN_RX = re.compile(r"\b(?:" + PROVINCES + r")\b\.?,?\s+" + CA_POSTAL + r"\b|\d,\s*Canada\b|\b(?:" + PROVINCES + r")\b,\s*Canada\b|\b"
                        + CA_POSTAL + r"\b,?\s*Canada\b")
STREET_WORDS = {"rd", "road", "ave", "avenue", "blvd", "boulevard", "st", "street", "dr", "drive", "ln", "lane", "way", "hwy",
                "highway", "pkwy", "parkway", "suite", "ste", "unit", "floor", "bldg", "building", "phone", "tel", "fax", "email",
                "address", "tower", "plaza", "ct", "court", "pl", "place", "cir", "circle", "usa", "us", "office", "located"}


def clean_city(city: str) -> str:
    """'Hollow Rd. Bland' -> 'Bland' (drop everything up to the last street / label word)."""
    words = city.split()
    for i in range(len(words) - 1, -1, -1):
        if words[i].lower().strip(".,:") in STREET_WORDS:
            return " ".join(words[i + 1:])
    return city


def norm_city(city: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", (city or "").lower().replace("saint ", "st "))).strip()


def city_point(city: str) -> tuple[float, float] | None:
    """Offline coordinates of a California city name; tries the whole phrase, then its last 3 / 2 / 1 words."""
    words = norm_city(city).split()
    for n in dict.fromkeys((len(words), 3, 2, 1)):
        if 0 < n <= len(words):
            p = CA_CITIES.get(" ".join(words[-n:]))
            if p:
                return p
    return None


AMBIGUOUS_ST = {"IN", "OR", "ME", "OK", "HI", "AL", "ID", "OH", "DE", "LA", "PA", "MA", "MD", "CO", "OK"}


def find_places(text: str, limit: int = 12) -> list[dict]:
    """Addresses written in a page: [{'city', 'state' (2 letters, or 'CANADA'), 'zip'}]: US 'City, ST 12345' / 'City, State 12345',
    'City, ST', 'Fresno CA' (known California city), and Canadian addresses (postal code + province / ', Canada')."""
    out: list[dict] = []
    seen = set()

    def add(city, state, zip_=""):
        city = clean_city(city)
        k = (norm_city(city), state)
        if k not in seen and len(out) < limit:
            seen.add(k)
            out.append({"city": city.strip(), "state": state, "zip": zip_})

    t = re.sub(r"\s+", " ", text or "")
    if FOREIGN_RX.search(t):
        add("", "CANADA")
    for m in PLACE_ZIP_RX.finditer(t):
        st = m.group(2).upper().rstrip(".")
        st = st if st in US_STATES else STATE_NAMES.get(st, "")
        if st and m.group(1).split()[-1].lower() != "baja":  # "Tijuana, Baja California 22000" is Mexico
            add(m.group(1), st, m.group(3))
    for m in PLACE_COMMA_RX.finditer(t):
        st = m.group(2)
        if st in US_STATES and not (st in AMBIGUOUS_ST and not (st == "CA" or city_point(m.group(1)))):
            add(m.group(1), st)
    for m in re.finditer(r"([A-Z][A-Za-z'.\-]+(?:\s[A-Z][A-Za-z'.\-]+){0,2})\s+CA\b(?!\.\w)", t):  # "Fresno CA" (no comma)
        if city_point(m.group(1)):
            add(m.group(1), "CA")
    return out
