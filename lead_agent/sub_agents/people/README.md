# People agent

**Role.** Answers "who is this person?" for a name, optionally with a LinkedIn profile URL and company.

**Inputs.** `name`, `profile` (optional), `company` (optional).

**Outputs.** A dict with the person's public LinkedIn data (headline, title). A match is accepted only when the
company also matches.

**Steps.** `from_memory` (previously looked-up profile), then `search`.

**Brands.** None of its own; uses the shared connector `connectors/linkedin.py` (`lookup_person`).

**Memory store.** `data/memory/people.sqlite` (looked-up profiles).
