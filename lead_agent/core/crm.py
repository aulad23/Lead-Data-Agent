"""CRM / existing-customer dedupe. Load any Excel/CSV export (columns are auto-detected like client files)
and check new leads against it by domain, email, normalised company name, or phone."""
import re
from pathlib import Path

import pandas as pd

from lead_agent.core.utils import domain_of, us_phone

FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "live.com", "msn.com"}
SUFFIX = r"\b(the|llc|pllc|inc|corp|co|ltd|lp|llp|pa|pc|dds|md|and|&)\b"


def norm_company(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(SUFFIX, "", (name or "").lower()))


class CRM:
    def __init__(self, path: str, mem=None):
        from lead_agent.workflows.client_file import FieldMapper  # lazy: avoids a circular import
        src = Path(path)
        df = (pd.read_csv(src, dtype=str) if src.suffix.lower() == ".csv" else pd.read_excel(src, dtype=str)).fillna("")
        mapper = FieldMapper(mem, {}) if mem else None
        fmap = mapper.detect(df) if mapper else {}
        col = {m["field"]: c for c, m in fmap.items() if m["field"]}
        self.domains, self.emails, self.names, self.phones = set(), set(), set(), set()
        for _, r in df.iterrows():
            get = lambda f: str(r[col[f]]).strip() if f in col else ""
            if get("website"):
                self.domains.add(domain_of(get("website")))
            if get("email"):
                e = get("email").lower()
                self.emails.add(e)
                d = e.split("@")[-1]
                if d not in FREE_MAIL:
                    self.domains.add(d)
            if get("company"):
                self.names.add(norm_company(get("company")))
            if us_phone(get("phone")):
                self.phones.add(us_phone(get("phone")))
        self.domains.discard("")
        self.names.discard("")
        self.fields = sorted(col)

    def match(self, company="", website="", email="", phone="") -> str:
        """Returns why it is already in the CRM ('' if new)."""
        d = domain_of(website)
        if d and d in self.domains:
            return "domain"
        if email and email.lower() in self.emails:
            return "email"
        if email and email.split("@")[-1].lower() in self.domains:
            return "email domain"
        if company and norm_company(company) in self.names:
            return "company name"
        if phone and us_phone(phone) in self.phones:
            return "phone"
        return ""

    def __len__(self):
        return len(self.domains | self.names)
