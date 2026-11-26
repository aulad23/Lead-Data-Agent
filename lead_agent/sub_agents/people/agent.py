"""PeopleAgent: who is this person?"""
from lead_agent.connectors import linkedin
from lead_agent.master.orchestrator import Step
from lead_agent.sub_agents.base import SubAgent


class PeopleAgent(SubAgent):
    """Who is this person? Memory first, then public search (exact profile match only)."""
    name = "people"

    def steps(self):
        def from_memory(ctx):
            ctx["info"] = self.mem.recall("person", ctx["key"])
            return ctx["info"] is not None

        def search(ctx):
            ctx["info"] = linkedin.lookup_person(ctx["name"], ctx["profile"], ctx.get("company", ""))
            self.mem.remember("person", ctx["key"], ctx["info"])
            return bool(ctx["info"])

        return [Step("from_memory", from_memory, stop=lambda ctx: ctx["info"] is not None),
                Step("search", search)]

    def run(self, name, profile="", company=""):
        key = profile or f"{name}|{company}"
        return self.orch.run({"name": name, "profile": profile, "company": company, "key": key, "info": None})["info"] or {}
