# Company-profile agent

**Role.** Reads the public LinkedIn company page of a company (logged out).

**Inputs.** `company` name, `city`, and an optional known LinkedIn `url`.

**Outputs.** LinkedIn URL, company size / employee count and latest-post info (used by `--max-employees`).

**Steps.** `find_url` (search for the page if missing), `from_memory` (reuse a page read earlier), `read_page`.

**Brands.** None of its own; uses the shared connector `connectors/linkedin.py`.

**Memory store.** `data/memory/company-profile.sqlite` (LinkedIn pages already read).
