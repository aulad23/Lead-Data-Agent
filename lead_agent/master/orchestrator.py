"""Mini-orchestrator used INSIDE every sub-agent.

A sub-agent's job is split into steps. Its own orchestrator decides, step by step:
  - run the step?          -> the step's `when(ctx)` condition (e.g. "no website yet")
  - stop early?            -> the step's `stop(ctx)` condition (e.g. "decision-maker found")
  - skip a useless step?   -> learned from THIS agent's memory: a step tried 8+ times in this context
                              that never produced anything is skipped (and retried 1 time in 10, to re-check)
  - in what order?         -> steps marked `reorder=True` are sorted by their learned success rate
Every step's result (useful or not) is written to the agent's own memory, so the agent gets sharper each run.
"""
import random
from dataclasses import dataclass, field
from typing import Callable

from lead_agent.master.memory import AgentMemory


@dataclass
class Step:
    name: str
    fn: Callable[[dict], bool | None]          # returns True if it produced something useful
    when: Callable[[dict], bool] = lambda ctx: True
    stop: Callable[[dict], bool] = lambda ctx: False  # checked after the step: True ends the plan
    reorder: bool = False                       # may be moved by learned success rate


@dataclass
class MiniOrchestrator:
    agent: str
    mem: AgentMemory
    steps: list[Step] = field(default_factory=list)
    min_tries: int = 8

    def _stat(self, scope: str, step: str) -> dict:
        return self.mem.recall("step", f"{scope}|{step}", {"tries": 0, "useful": 0})

    def _plan(self, scope: str) -> list[Step]:
        fixed = [s for s in self.steps if not s.reorder]
        movable = [s for s in self.steps if s.reorder]

        def rate(s):
            st = self._stat(scope, s.name)
            return st["useful"] / st["tries"] if st["tries"] else 1.0  # unknown steps get tried first
        movable.sort(key=rate, reverse=True)
        # movable steps keep the slot of the first movable step
        if not movable:
            return fixed
        i = self.steps.index(next(s for s in self.steps if s.reorder))
        return fixed[:i] + movable + fixed[i:]

    def run(self, ctx: dict, scope: str = "all") -> dict:
        ctx.setdefault("trace", [])
        group_done = False  # a reorderable step's stop() ends only the reorderable group (alternatives)
        for s in self._plan(scope):
            if (s.reorder and group_done) or not s.when(ctx):
                continue
            st = self._stat(scope, s.name)
            if st["tries"] >= self.min_tries and st["useful"] == 0 and random.random() > 0.1:
                ctx["trace"].append(f"{s.name}:skipped(learned)")
                continue
            useful = bool(s.fn(ctx))
            self.mem.remember("step", f"{scope}|{s.name}",
                              {"tries": st["tries"] + 1, "useful": st["useful"] + int(useful)})
            ctx["trace"].append(f"{s.name}:{'ok' if useful else '-'}")
            if s.stop(ctx):
                if s.reorder:
                    group_done = True
                else:
                    break
        return ctx
