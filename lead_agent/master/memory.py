"""Per-agent long-term memory.

Every sub-agent owns ONE private store: data/memory/<agent>.sqlite.
A sub-agent is handed only its own AgentMemory object, so it cannot read or write another agent's memory.
Only the MainAgent holds the MemoryHub (all stores), so only it can look across agents, copy lessons
between them, and write review feedback into them.
"""
import json
import sqlite3
import time

from lead_agent.config.settings import MEMORY_DIR

ROOT = MEMORY_DIR
ROOT.mkdir(parents=True, exist_ok=True)

PATTERNS = {"first": "{f}", "first.last": "{f}.{l}", "flast": "{fi}{l}", "firstlast": "{f}{l}", "first_last": "{f}_{l}",
            "last": "{l}", "firstl": "{f}{li}"}


class AgentMemory:
    """Private key-value + event memory of a single agent."""

    def __init__(self, agent: str):
        self.agent = agent
        self.db = sqlite3.connect(ROOT / f"{agent}.sqlite")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS facts (kind TEXT, key TEXT, value TEXT, n INT DEFAULT 1, ts REAL, PRIMARY KEY (kind, key));
        CREATE TABLE IF NOT EXISTS events (ts REAL, kind TEXT, data TEXT);
        """)

    def remember(self, kind: str, key: str, value) -> None:
        self.db.execute("""INSERT INTO facts VALUES (?,?,?,1,?) ON CONFLICT(kind,key)
                           DO UPDATE SET value=excluded.value, n=n+1, ts=excluded.ts""",
                        (kind, key.lower(), json.dumps(value), time.time()))
        self.db.commit()

    def recall(self, kind: str, key: str, default=None):
        row = self.db.execute("SELECT value FROM facts WHERE kind=? AND key=?", (kind, key.lower())).fetchone()
        return json.loads(row[0]) if row else default

    def all_of(self, kind: str) -> dict:
        return {k: json.loads(v) for k, v in self.db.execute("SELECT key, value FROM facts WHERE kind=?", (kind,))}

    def log(self, kind: str, data: dict) -> None:
        self.db.execute("INSERT INTO events VALUES (?,?,?)", (time.time(), kind, json.dumps(data)))
        self.db.commit()

    def stats(self) -> dict:
        kinds = dict(self.db.execute("SELECT kind, COUNT(*) FROM facts GROUP BY kind").fetchall())
        kinds["events"] = self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return kinds

    # ---- discovery: source yield ----
    def add_yield(self, industry: str, source: str, companies: int, with_people: int) -> None:
        k = f"{industry}|{source}"
        y = self.recall("yield", k, {"runs": 0, "companies": 0, "people": 0})
        self.remember("yield", k, {"runs": y["runs"] + 1, "companies": y["companies"] + companies,
                                   "people": y["people"] + with_people})

    def source_order(self, industry: str, sources: list[str]) -> list[str]:
        """Best-yield sources first; a source that returned nothing in 3+ runs is skipped."""
        scored = []
        for s in sources:
            y = self.recall("yield", f"{industry}|{s}")
            if y and y["runs"] >= 3 and y["companies"] == 0:
                continue
            scored.append((-(y["companies"] + 2 * y["people"]) / y["runs"] if y else -999, s))
        return [s for _, s in sorted(scored)]

    # ---- company agent: email patterns ----
    def learn_email(self, email: str, first: str, last: str) -> str | None:
        local, domain = email.lower().split("@")
        f, l = first.lower(), last.lower()
        for name, p in PATTERNS.items():
            if p.format(f=f, l=l, fi=f[:1], li=l[:1]) == local:
                self.remember("email_pattern", domain, name)
                self.remember("email_pattern_global", name, self.recall("email_pattern_global", name, 0) + 1)
                return name
        return None

    def email_guess(self, first: str, last: str, domain: str) -> tuple[str, str]:
        p, src = self.recall("email_pattern", domain), "learned for domain"
        if not p:
            glob = self.all_of("email_pattern_global")
            p = max(glob, key=glob.get) if glob else "first.last"
            src = "most common learned" if glob else "default"
        f, l = first.lower(), last.lower()
        return PATTERNS[p].format(f=f, l=l, fi=f[:1], li=l[:1]) + "@" + domain, src

    def bad_words(self) -> set[str]:
        return set(self.all_of("bad_word"))


class MemoryHub:
    """Held only by MainAgent: access to every agent's memory."""

    def __init__(self, agents: list[str]):
        self.stores = {a: AgentMemory(a) for a in agents}

    def of(self, agent: str) -> AgentMemory:
        return self.stores[agent]

    def summary(self) -> str:
        return "; ".join(f"{a}: {m.stats() or 'empty'}" for a, m in self.stores.items())

    def share(self, src: str, dst: str, kind: str) -> int:
        """Main agent copies a lesson from one agent's memory into another's."""
        items = self.stores[src].all_of(kind)
        for k, v in items.items():
            self.stores[dst].remember(kind, k, v)
        return len(items)
