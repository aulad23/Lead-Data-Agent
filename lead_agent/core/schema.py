"""Lead output schema: CSV columns and decision-maker title pattern."""
import re

DECISION_RX = re.compile(r"attorney|shareholder|broker|owner|founder|ceo|president|principal|partner|director|chief|manager|dds|dmd|md\b", re.I)
FIELDS = ["company", "industry_match", "website", "phone", "address", "city", "state", "zip", "country",
          "company_email", "linkedin_company", "contact_name", "contact_title", "contact_linkedin",
          "contact_email", "email_status", "people_found", "signals", "priority", "sources", "needs_review", "notes"]
