# Company agent

**Role.** Takes one company and produces a verified website, the people, a decision-maker, an email and a score.

**Inputs.** A company dict (name, city, state, any known website/phone), `industry`, and flags `use_linkedin`,
`use_apify`, `apify_max`.

**Outputs.** The enriched company dict with website, decision-maker (name, title, source), email and status, and an
`agent_trace` of the steps that ran.

**Steps.** `find_website`, `crawl_site`, `name_from_site`, `read_site`, `name_rule`, `linkedin_people`,
`apify_people` (only with `--apify`), `pick_dm`, `ai_verify` (only when an LLM key is set), `email_on_site`,
`email_guess`.

**Files.** `agent.py` (steps), `site_finder.py` (website search, verification, name helpers),
`people_finder.py` (free decision-maker finder).

**Brands.** `brands/llm_brain.py` (Gemini / Groq / Ollama). Uses connectors `linkedin`, `apify`, `duckduckgo`.

**Memory store.** `data/memory/company.sqlite` (email patterns, rejected words, step statistics).
