"""US NPI Registry discovery source (healthcare organisations + authorised official)."""
import json

from lead_agent.core.utils import fetch
from lead_agent.sub_agents.discovery.brands._common import _company

# Industry keyword -> NPI taxonomy description (US healthcare only).
NPI_TAXONOMY = {
    "dental": "Dentist", "dentist": "Dentist", "chiropractor": "Chiropractor",
    "optometry": "Optometrist", "optometrist": "Optometrist", "pharmacy": "Pharmacy",
    "physiotherapy": "Physical Therapist", "physical therapy": "Physical Therapist",
    "clinic": "Clinic/Center", "podiatry": "Podiatrist", "psychology": "Psychologist",
}


def npi(industry: str, city: str, state: str, limit: int) -> list[dict]:
    """US NPI Registry: organisations (NPI-2) plus their authorised official = decision-maker."""
    tax = NPI_TAXONOMY.get(industry.lower())
    if not tax or not state:
        return []
    st, body = fetch("https://npiregistry.cms.hhs.gov/api/",
                     {"version": "2.1", "taxonomy_description": tax, "city": city, "state": state,
                      "enumeration_type": "NPI-2", "limit": min(200, limit * 3)}, gap=0.5)
    try:
        results = json.loads(body).get("results", [])
    except ValueError:
        return []
    out = []
    for r in results:
        b = r.get("basic", {})
        addr = next((a for a in r.get("addresses", []) if a.get("address_purpose") == "LOCATION"), {})
        c = _company(b.get("organization_name", ""), "npi",
                     phone=addr.get("telephone_number", ""), address=addr.get("address_1", ""),
                     city=addr.get("city", "").title(), state=addr.get("state", ""),
                     zip=(addr.get("postal_code") or "")[:5])
        c["npi"] = r.get("number")
        if b.get("authorized_official_last_name"):
            name = f'{b.get("authorized_official_first_name", "").title()} {b["authorized_official_last_name"].title()}'
            c["people"].append({"name": name.strip(), "title": (b.get("authorized_official_title_or_position") or "Authorized official").title(),
                                "source": "npi"})
        out.append(c)
    return out
