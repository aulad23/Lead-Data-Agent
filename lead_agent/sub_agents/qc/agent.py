"""QCAgent: scores commenters as leads and flags decision-makers."""
import re

from lead_agent.core.schema import DECISION_RX
from lead_agent.master.orchestrator import Step
from lead_agent.sub_agents.base import SubAgent

INTENT_RX = re.compile(r"interested|looking for|need (?:a|help)|how much|price|quote|recommend|dm me|message me|"
                       r"contact|struggl|problem|challenge|anyone know|we use|switching", re.I)


class QCAgent(SubAgent):
    """Scores commenters as leads and flags decision-makers."""
    name = "qc"

    def steps(self):
        def score(ctx):
            c = ctx["c"]
            c["is_decision_maker"] = "yes" if DECISION_RX.search(c.get("headline", "")) else ""
            c["lead_score"] = ((40 if c["is_decision_maker"] else 0) + (30 if INTENT_RX.search(c.get("comment", "")) else 0)
                               + (15 if c.get("commenter_profile") else 0) + min(15, c.get("likes", 0) * 3))
            return c["lead_score"] >= 50

        return [Step("score", score)]

    def run(self, c):
        return self.orch.run({"c": c})["c"]
