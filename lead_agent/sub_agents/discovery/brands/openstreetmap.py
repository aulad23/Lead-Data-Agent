"""OpenStreetMap discovery source: Nominatim geocoding + Overpass search."""
import hashlib
import json
import re
import time

from lead_agent.core.utils import _db, fetch, norm_state, km_between
from lead_agent.sub_agents.discovery.brands._common import _company

# Real manufacturing only. Weak tags (industrial land / buildings / office=company) must have a website or an
# industrial-sounding name, otherwise they are business parks, warehouses and shops.
_IND_WORDS = ("manufactur|mfg|fabricat|machin|industries|industrial|precision|tool|plastic|metal|aerospace|engineering|"
              "products|foods|apparel|electronics|assembly|works|steel|aluminum|casting|forge|welding|packaging|"
              "components|systems|laboratories|labs|instruments|cabinet|furniture|textile|garment")
MANUFACTURING_TAGS = [
    '"industrial"="factory"',
    '"man_made"="works"',
    '"craft"~"^(metal_construction|sawmill|toolmaker|stonemason|window_construction)$"',
    '"industrial"~"^(machine_shop|engineering|manufacturing|assembly|metal|food|chemical|electronics)$"',
    '"landuse"="industrial"]["website"',
    '"building"="industrial"]["website"',
    f'"office"="company"]["name"~"{_IND_WORDS}",i',
    f'"building"~"^(industrial|warehouse|factory)$"]["name"~"{_IND_WORDS}",i',
]
# Names / tags that are not manufacturers even when OSM tags them as works / craft.
NOT_MANUFACTURER = re.compile(r"brew|winer|distill|cidery|taproom|vineyard|cellars|\b(bar|pub|grill|cafe|restaurant)\b|"
                              r"\b(mart|store|outlet|market|supermarket)\b", re.I)

# Industry keyword -> OpenStreetMap tags. Unknown industries fall back to a name search.
OSM_TAGS = {
    "dental": ['"amenity"="dentist"', '"healthcare"="dentist"'],
    "dentist": ['"amenity"="dentist"', '"healthcare"="dentist"'],
    "veterinary": ['"amenity"="veterinary"'],
    "vet": ['"amenity"="veterinary"'],
    "law": ['"office"="lawyer"'],
    "lawyer": ['"office"="lawyer"'],
    "accounting": ['"office"="accountant"'],
    "accountant": ['"office"="accountant"'],
    "real estate": ['"office"="estate_agent"'],
    "insurance": ['"office"="insurance"'],
    "restaurant": ['"amenity"="restaurant"'],
    "gym": ['"leisure"="fitness_centre"'],
    "fitness": ['"leisure"="fitness_centre"'],
    "hotel": ['"tourism"="hotel"'],
    "auto repair": ['"shop"="car_repair"'],
    "plumbing": ['"craft"="plumber"'],
    "plumber": ['"craft"="plumber"'],
    "hvac": ['"craft"="hvac"'],
    "pharmacy": ['"amenity"="pharmacy"'],
    "chiropractor": ['"healthcare"="chiropractor"'],
    "physiotherapy": ['"healthcare"="physiotherapist"'],
    "salon": ['"shop"="hairdresser"', '"shop"="beauty"'],
    "pet": ['"shop"="pet"', '"amenity"="animal_boarding"', '"shop"="pet_grooming"'],
    "school": ['"amenity"="school"'],
    "clinic": ['"amenity"="clinic"'],
    # man_made=works dropped: in big cities it is mostly studios / refineries / unrelated shops. craft=upholsterer,
    # glaziery, carpenter etc. are shops, not manufacturers.
    "manufacturing": MANUFACTURING_TAGS,
    "manufacturer": MANUFACTURING_TAGS,
}


def geocode_bbox(location: str) -> tuple[float, float, float, float] | None:
    """Nominatim (OSM) city lookup, US only. Returns (south, west, north, east)."""
    st, body = fetch("https://nominatim.openstreetmap.org/search",
                     {"q": location, "countrycodes": "us", "format": "json", "limit": 1}, gap=1.1)
    try:
        bb = json.loads(body)[0]["boundingbox"]
        return float(bb[0]), float(bb[2]), float(bb[1]), float(bb[3])
    except (ValueError, IndexError, KeyError):
        return None


def geocode_point(city: str = "", state: str = "", zip_code: str = "") -> tuple[float, float] | None:
    """Nominatim (OSM) centre point of a US ZIP (preferred) or city. None when unknown."""
    z = re.match(r"\d{5}", (zip_code or "").strip())
    if z:
        params = {"postalcode": z.group(0), "countrycodes": "us", "format": "json", "limit": 1}
    elif city.strip():
        params = {"q": f"{city}, {state}".strip(", "), "countrycodes": "us", "format": "json", "limit": 1}
    else:
        return None
    st, body = fetch("https://nominatim.openstreetmap.org/search", params, gap=1.1)
    try:
        hit = json.loads(body)[0]
        return float(hit["lat"]), float(hit["lon"])
    except (ValueError, IndexError, KeyError, TypeError):
        return None


def in_area(origin: tuple[float, float] | None, want_state: str, city: str, state: str, zip_code: str = "",
            radius_km: float = 60) -> bool | None:
    """Is a company address inside the requested metro? False = known outside (other state / country / too far),
    True = inside, None = cannot tell. ZIP and city are both tried: either one inside the radius is enough."""
    st = norm_state(state)
    if st and st != want_state:
        return False  # other US state, or not a US state at all (e.g. Baja California)
    if not origin or not (city.strip() or re.match(r"\d{5}", (zip_code or "").strip())):
        return None
    dists = []
    for pt in (geocode_point(zip_code=zip_code) if re.match(r"\d{5}", (zip_code or "").strip()) else None,
               geocode_point(city, st or want_state) if city.strip() else None):
        if pt:
            dists.append(km_between(origin, pt))
    return None if not dists else min(dists) <= radius_km


OVERPASS_MIRRORS = ["https://overpass-api.de/api/interpreter",
                    "https://overpass.kumi.systems/api/interpreter",
                    "https://overpass.private.coffee/api/interpreter"]
_UA = {"User-Agent": "LeadAgentPOC/0.1 (research use)"}
_bad_until: dict[str, float] = {}  # mirror -> time until which it is skipped (it just failed)
WIDE_INDUSTRIES = {"manufacturing", "manufacturer"}  # sparse in OSM: search a padded box (metro area, not only the city)
BBOX_PAD = 0.12  # degrees (~13 km)


def _log(msg: str) -> None:
    print(f"[discovery] {msg}", flush=True)


def _cached_elements(data: dict) -> list | None:
    """A good (non-empty) answer for this query cached under ANY mirror in the last 14 days."""
    for url in OVERPASS_MIRRORS:
        key = hashlib.sha1((url + repr(sorted(data.items()))).encode()).hexdigest()
        row = _db.execute("SELECT body, ts FROM http WHERE k=?", (key,)).fetchone()
        if row and time.time() - row[1] < 14 * 86400:
            try:
                els = json.loads(row[0])["elements"]
                if els:
                    return els
            except (ValueError, KeyError, TypeError):
                pass
    return None


def _forget(url: str, data: dict) -> None:
    """core.utils.fetch caches every HTTP 200 for 14 days, including an Overpass 'runtime error' / empty answer.
    Drop such an entry so the failure is retried next run (the cache key mirrors fetch())."""
    key = hashlib.sha1((url + repr(sorted(data.items()))).encode()).hexdigest()
    _db.execute("DELETE FROM http WHERE k=?", (key,))
    _db.commit()


def _overpass(query: str, tries: int = 2) -> tuple[list | None, str]:
    """Run one Overpass query. Retries with backoff and falls back across mirrors on timeout / 429 / 5xx / remark
    errors. Returns (elements, "") or (None, reason). Failed answers are never left in the cache."""
    data = {"data": query}
    reason = "no mirror tried"
    hit = _cached_elements(data)
    if hit is not None:
        return hit, ""
    for attempt in range(tries):
        live = [m for m in OVERPASS_MIRRORS if _bad_until.get(m, 0) < time.time()] or OVERPASS_MIRRORS
        for url in live:
            host = url.split("/")[2]
            st, body = fetch(url, data=data, gap=2, timeout=45, headers=_UA)
            els = None
            if st == 200:
                try:
                    js = json.loads(body)
                    remark = str(js.get("remark", "")).lower()
                    if "error" in remark or "timed out" in remark or "out of memory" in remark:
                        reason = f"{host}: {remark[:80]}"
                    else:
                        els = js["elements"]
                except (ValueError, KeyError, TypeError):
                    reason = f"{host}: bad JSON"
            else:
                reason = f"{host}: HTTP {st}" if st else f"{host}: timeout/network error"
            if els is not None:
                if not els:
                    _forget(url, data)  # an empty answer is retried next time, not trusted for 14 days
                return els, ""
            _forget(url, data)
            _bad_until[url] = time.time() + 90
        time.sleep(3 * (attempt + 1))  # backoff before the next round of mirrors
    return None, reason


def _want_state(location: str) -> str:
    return norm_state(location.rsplit(",", 1)[-1]) if "," in location else ""


def _nominatim_fallback(tag: str, bbox, limit: int) -> list[dict]:
    """Last resort when every Overpass mirror is down: Nominatim place search for a simple amenity-style tag."""
    m = re.match(r'"(amenity|healthcare|office|leisure|shop|craft|tourism)"="(\w+)"$', tag)
    if not m:
        return []
    s, w, n, e = bbox
    st, body = fetch("https://nominatim.openstreetmap.org/search",
                     {"q": m.group(2).replace("_", " "), "viewbox": f"{w},{n},{e},{s}", "bounded": 1, "countrycodes": "us",
                      "format": "jsonv2", "extratags": 1, "addressdetails": 1, "limit": min(40, limit)}, gap=1.1)
    try:
        hits = json.loads(body)
    except ValueError:
        return []
    out = []
    for h in hits if isinstance(hits, list) else []:
        x, a = h.get("extratags") or {}, h.get("address") or {}
        out.append({"tags": {"name": h.get("name", ""), "website": x.get("website") or x.get("contact:website") or "",
                             "phone": x.get("phone") or x.get("contact:phone") or "",
                             "addr:housenumber": a.get("house_number", ""), "addr:street": a.get("road", ""),
                             "addr:city": a.get("city") or a.get("town") or "", "addr:state": a.get("state", ""),
                             "addr:postcode": a.get("postcode", "")}})
    return out


def osm(industry: str, location: str, limit: int, errors: dict | None = None) -> list[dict]:
    """OpenStreetMap candidates. Never fails silently: every failure / empty tag is logged and counted in `errors`
    (a dict the caller owns, e.g. {"osm": n_failed_queries})."""
    errors = errors if errors is not None else {}
    bbox = geocode_bbox(location)
    if not bbox:
        errors["osm"] = errors.get("osm", 0) + 1
        _log(f"osm FAILED (geocode of '{location}' returned nothing)")
        return []
    if industry.lower() in WIDE_INDUSTRIES:  # the metro, not just the city polygon (still filtered to the state below)
        bbox = (bbox[0] - BBOX_PAD, bbox[1] - BBOX_PAD, bbox[2] + BBOX_PAD, bbox[3] + BBOX_PAD)
    b = ",".join(f"{v:.5f}" for v in bbox)
    tags = OSM_TAGS.get(industry.lower())
    if tags:
        sels = [(t, f'nwr[{t}]["name"]({b});') for t in tags]
    else:
        word = re.escape(industry.split()[0])
        sels = [(f"name~{word}", f'nwr["name"~"{word}",i]({b});')]
    # One request per tag: a union over a big city exceeds the Overpass timeout and returns nothing; the name filter is
    # inside the query so unnamed objects do not use up the result cap.
    els = []
    for tag, sel in sels:
        q = f"[out:json][timeout:40];({sel});out center tags {max(limit * 5, 150)};"
        got, why = _overpass(q)
        if got is None:
            errors["osm"] = errors.get("osm", 0) + 1
            got = _nominatim_fallback(tag, bbox, limit * 3)
            _log(f"osm FAILED ({why}) for {tag}; Nominatim fallback gave {len(got)}")
            if not got and (errors["osm"] >= 2 or not els):
                _log("osm FAILED (Overpass unreachable on all mirrors), giving up this source")
                break
        elif not got:
            _log(f"osm: 0 results for {tag}")
        else:
            _log(f"osm: {len(got)} results for {tag}")
        els += got
    want = _want_state(location)
    out, seen = [], set()
    for e in els:
        t = e.get("tags", {})
        name = (t.get("name") or "").strip()
        if not name:
            continue
        if industry.lower() in WIDE_INDUSTRIES and (NOT_MANUFACTURER.search(name) or t.get("shop") or t.get("amenity")
                                                    or t.get("craft") in ("brewery", "winery", "distillery")
                                                    or t.get("industrial") in ("brewery", "winery", "distillery")):
            continue
        if industry.lower() in WIDE_INDUSTRIES and not (t.get("website") or t.get("contact:website") or t.get("industrial")
                                                        or re.search(_IND_WORDS, name, re.I)):
            continue  # man_made=works also tags studios / refineries / sound stages: need a website or an industrial name
        ns = norm_state(t.get("addr:state", ""))
        if want and ns and ns != want:
            continue  # padded box can cross the state line
        key = (name.lower(), t.get("addr:housenumber", ""), t.get("addr:street", ""))
        if key in seen:  # same object returned by two tag queries
            continue
        seen.add(key)
        out.append(_company(
            name, "osm",
            website=t.get("website") or t.get("contact:website") or "",
            phone=t.get("phone") or t.get("contact:phone") or "",
            address=" ".join(x for x in [t.get("addr:housenumber"), t.get("addr:street")] if x),
            city=t.get("addr:city", ""), state=t.get("addr:state", ""), zip=t.get("addr:postcode", ""),
        ))
    return out
