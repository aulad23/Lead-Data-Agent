# Master Agent

**Role.** Top-level orchestrator. Decides which sub-agent to call, in what order, and for which row; the only
component that holds every agent's memory (`MemoryHub`) and can copy lessons between agents or write human-review
feedback into them.

**Inputs.** Command-line arguments (industry, city, state, keywords, client file, flags such as `--linkedin`,
`--apify`, `--exclude`) via `python -m lead_agent companies | posts | learn | memory`.

**Outputs.** CSV files in `output/` (`<industry>-<city>-<state>-<date>.csv`, `posts-*.csv`, `posts-*-comments.csv`)
and a run summary in the console.

**Files.**
- `main_agent.py`: `MainAgent` with `companies()`, `posts_mode()`, `learn()`, `show_memory()`.
- `orchestrator.py`: `Step` and `MiniOrchestrator`, used inside every sub-agent (`when`, `stop`, reorder by learned
  success rate, skip steps that never helped).
- `memory.py`: `AgentMemory` (one SQLite store per agent) and `MemoryHub`.
- `cli.py`: argument parsing for the four master commands.

**Sub-agents driven.** discovery, company, company-profile, posts, people, qc.

**Memory store.** `data/memory/main.sqlite` (run history); plus read access to all other stores through the hub.
