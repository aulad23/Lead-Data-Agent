"""Shared sub-agent base class and helpers."""
import re

from lead_agent.core.schema import DECISION_RX
from lead_agent.master.memory import AgentMemory
from lead_agent.master.orchestrator import MiniOrchestrator, Step


def log(agent: str, msg: str) -> None:
    print(f"[{agent}] {msg}")


def size_lower_bound(size) -> int | None:
    """LinkedIn size like '11-50', '501-1,000', '10,001+' or a plain count -> lower bound; None if unknown."""
    m = re.match(r"\s*([\d,]+)", str(size or ""))
    return int(m.group(1).replace(",", "")) if m and m.group(1).replace(",", "") else None


def has_dm(ctx) -> bool:
    return any(DECISION_RX.search(p.get("title", "")) for p in ctx["people"])


class SubAgent:
    """A sub-agent sees ONLY its own memory (self.mem) and runs its own MiniOrchestrator (self.orch)."""
    name = "sub"

    def __init__(self, mem: AgentMemory):
        self.mem = mem
        self.orch = MiniOrchestrator(self.name, mem, self.steps())

    def steps(self) -> list[Step]:
        return []
