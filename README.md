# Universal Lead Data Agent (POC, US first)

Free proof of concept. No LinkedIn login, no paid APIs.

## Project layout

```
lead_agent/
  __main__.py      single CLI: python -m lead_agent <command>
  config/          central paths (settings.py)
  core/            shared, brand-free building blocks (utils, enrichment, crm, schema)
  connectors/      shared external clients used by 2+ agents (duckduckgo, linkedin, apify)
  master/          Master Agent: main_agent, orchestrator, memory, cli
  sub_agents/      discovery, company, company_profile, posts, people, qc, influencer
                   (each with its own README; agent-only providers live in <agent>/brands/)
  workflows/       user-facing pipelines: client_file (excel), inbox, legacy_pipeline
docs/              ARCHITECTURE.md, presentations/
data/              cache.sqlite, memory/<agent>.sqlite, apify_cache.json (generated)
samples/           example client / CRM / influencer files
inbox/  outbox/  output/   working folders (client files in, results out)
reference/         old example repo, not used by code
```

Commands: `python -m lead_agent <companies|posts|learn|memory|excel|inbox|influencer|pipeline> ...`
(run `python -m lead_agent` with no arguments to list them). Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

```
pip install -r requirements.txt
python -m lead_agent pipeline --industry dental --city Austin --state TX --limit 30
python -m lead_agent pipeline --industry law --city Dallas --state TX --limit 30 --linkedin-search
```

### Multi-agent mode (recommended)
```
python -m lead_agent companies --industry dental --city Austin --state TX --limit 30 --linkedin
python -m lead_agent posts --keywords "dental practice growth" --limit 10 --lookup
python -m lead_agent learn output/<reviewed>.csv    # add a 'verdict' column: right / wrong
python -m lead_agent memory                          # show every agent's memory
python -m lead_agent influencer <file.xlsx>          # track public posts of LinkedIn profiles listed in a file (e.g. samples/Influencer List.xlsx)
```
- `--max-employees N` (small-company filter, e.g. `--industry manufacturing --state CA --max-employees 100`): drops companies whose
  public LinkedIn size lower bound is above N (counted as `dropped_size`); unknown size is kept with a "size unknown" note.
  It turns `--linkedin` on automatically and keeps processing candidates until `--limit` rows survive. Manufacturing has its own OSM tags and verification keywords.
- **Main agent** (`MainAgent`) decides which sub-agent to call: Discovery, Company (website + people + email),
  CompanyProfile (LinkedIn size), Posts (LinkedIn public posts + comments), People (commenter headline), QC (lead score).
- **Each sub-agent has its own mini-orchestrator** (`master/orchestrator.py`): its job is split into steps; it runs a step only
  when needed (`when`), stops when done (`stop`), reorders alternative steps by learned success rate, and skips a step
  that never helped in 8+ tries (re-checks it 1 time in 10). Every step result goes to that agent's own memory.
  The `agent_trace` column shows what each agent decided, e.g. `crawl_site:ok read_site:ok pick_dm:ok email_guess:ok`.
  `python -m lead_agent memory` prints every agent's memory and step statistics.
- **Memory, one private store per agent** (`data/memory/<agent>.sqlite`): each sub-agent gets only its own store
  (discovery: source yield; company: email patterns + rejected words; company-profile: LinkedIn pages; people: looked-up
  profiles; posts: seen posts; qc; main: run history). Only the main agent holds the `MemoryHub`, so only it can read
  all stores, copy lessons between agents (`hub.share`) and write human-review feedback into them.
- **Posts mode output**: `posts-*.csv` (author, profile, date, content, likes, comment count) and
  `posts-*-comments.csv` (commenter, profile, headline, comment, date, likes, lead score, decision-maker flag).
- LinkedIn is read **logged out only**: public posts show a preview of comments (often 2-10 of N). Full threads need a vendor (Apify) after approval.

Output: `output/<industry>-<city>-<state>-<date>.csv`, sorted by priority, plus POC metrics in the console.

## Pipeline
1. **Plan**: industry + US city/state from the command line.
2. **Discover**: NPI Registry (US healthcare, includes the authorized official), OpenStreetMap, DuckDuckGo (US region, directories filtered).
3. **Verify**: industry keyword score from the company's own website (registry/map category counts as verified).
4. **People**: team/about pages (names checked against US first-name list), company-name rule ("Law Office of X"), optional public LinkedIn profiles found via search engine.
5. **Enrich**: emails on site, pattern guess + MX check (no SMTP probing), US phone format, LinkedIn company page, tech stack, hiring signal.
6. **QC**: dedupe across sources, `needs_review` flag for weak rows.
7. **Output**: CSV with a priority score.

Polite by design: robots.txt respected, per-host throttle, everything cached in `data/cache.sqlite` (14 days).

## Known limits (POC)
- Person extraction is rule-based; wrong names are possible, so check `needs_review` rows. An LLM check is the next step.
- NPI returns results alphabetically; large cities need paging.
- Free DuckDuckGo search is sometimes empty; the agent retries.

## Client Excel / CSV mode
```
python -m lead_agent excel client.xlsx --linkedin [--industry dental] [--map "Practice Name=company"]
```
- The main agent detects each column from its header **and** its values (emails, URLs, US phones/states, ZIPs,
  LinkedIn links, company-like names, person names). Detected mappings are remembered for the next client file;
  `--map` fixes a wrong one (and is remembered too).
- Per row it plans which agents to call from what is filled vs missing (`agent_plan` column), e.g.
  `company_from_email > company_agent > people_lookup`.
- Client cells are never overwritten; new values go into `agent_*` columns.
  Output: `output/<file>-enriched.xlsx` with sheets `enriched` and `field_report`.
- Person lookups accept a LinkedIn match only when the company also matches (no same-name mix-ups).

## AI brain (optional, free)
Set ONE of these and the Company agent adds an `ai_verify` step (industry match, real decision-maker + seniority,
removes fake "names"; the words it rejects are learned into the Company agent's memory, so the rules improve too):
- `GEMINI_API_KEY` from aistudio.google.com (free, no card) — optional `GEMINI_MODEL`
- `GROQ_API_KEY` from console.groq.com (free, no card) — optional `GROQ_MODEL`
- or a local Ollama (`ollama pull llama3.1`) — fully offline
No key = rules only (the log shows `brain: rules only`). One call per company, cached per domain.

## Apify fallback (paid, OFF by default)
Free sources always run first. Only with `--apify`, and only for a company where they found **no decision-maker**,
the agent asks Apify (`harvestapi/linkedin-company-employees`, no LinkedIn login) for that company's
Owner / Partner / CXO / VP / Director / Manager profiles (name, title, LinkedIn URL, location).
```
python -m lead_agent companies --industry law --city Houston --state TX --limit 30 --apify [--apify-max 10]
python -m lead_agent excel client.xlsx --apify
python -m lead_agent inbox --apify
```
- Token: `setx APIFY_TOKEN "apify_api_..."` (read from the saved Windows setting, no restart needed).
- Cost (free plan, Nov 2026): $0.02 per company run + $0.003 per profile -> about $0.05 for 10 people.
- Never paid twice: every company is cached in `data/apify_cache.json`.
- Monthly cap: `APIFY_MONTHLY_USD` (default 4.50, under the free $5); when reached the step is skipped.
- `agent_trace` shows `apify_people:ok` where it was used; the run summary shows `apify_used` and the month's spend.

## CRM dedupe
```
python -m lead_agent companies ... --exclude crm_export.xlsx        # skip known companies
python -m lead_agent companies ... --exclude crm_export.xlsx --keep-crm   # keep, mark in_crm
python -m lead_agent excel client.xlsx --exclude crm_export.xlsx     # adds agent_in_crm
```
Matches on domain, email, email domain, normalised company name or phone; the CRM file's columns are auto-detected.

## Inbox folder (simplest way)
1. Put the client's Excel/CSV in **`inbox/`** (any column names; empty columns = what the client wants filled).
2. Run `python -m lead_agent inbox` (or double-click **`run_inbox.bat`**; `--watch` keeps watching the folder).
3. Get **`outbox/<file>-filled.xlsx`**: client cells the agent filled are **yellow**; `agent_plan` (which agents it
   called), `agent_filled` (column <- which agent), `agent_not_found` (what it could not get and why).
   Sheet `field_report` shows how every column was understood. The original goes to `inbox/done/`.
- The main agent decides per row: empty client column -> which agent can fill it -> does the row have that agent's
  inputs? (e.g. Website empty + Email given -> email domain; Owner empty -> Company agent's decision-maker;
  Title/LinkedIn empty + person given -> People agent; Company size empty -> Company-profile agent).
- LinkedIn agents switch on automatically only when the client file has LinkedIn / title / size columns to fill.
- Optional: put a CRM export in `inbox/crm/` and every file gets `agent_in_crm`.
