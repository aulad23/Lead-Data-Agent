"""Shared helper for the discovery sources: the common company-dict shape."""
from lead_agent.core.utils import domain_of, us_phone


def _company(name, source, **kw):
    d = {"company": (name or "").strip(), "source": source, "website": "", "phone": "",
         "address": "", "city": "", "state": "", "zip": "", "country": "US", "people": []}
    d.update({k: v for k, v in kw.items() if v})
    d["phone"] = us_phone(d["phone"]) or d["phone"]
    d["domain"] = domain_of(d["website"])
    return d
