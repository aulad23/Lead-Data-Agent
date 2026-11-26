"""CompanyProfileAgent: LinkedIn public company page."""
from lead_agent.connectors import linkedin
from lead_agent.master.orchestrator import Step
from lead_agent.sub_agents.base import SubAgent


class CompanyProfileAgent(SubAgent):
    """LinkedIn public company page: find URL if missing, then size / employees / latest post."""
    name = "company-profile"

    def steps(self):
        def from_memory(ctx):
            ctx["info"] = self.mem.recall("company_profile", ctx["url"]) if ctx["url"] else None
            return ctx["info"] is not None

        def find_url(ctx):
            ctx["url"] = linkedin.find_company(ctx["company"], ctx["city"])
            return bool(ctx["url"])

        def read_page(ctx):
            ctx["info"] = linkedin.read_company(ctx["url"])
            self.mem.remember("company_profile", ctx["url"], ctx["info"])
            return bool(ctx["info"].get("company_size"))

        return [
            Step("find_url", find_url, when=lambda ctx: not ctx["url"]),
            Step("from_memory", from_memory, when=lambda ctx: bool(ctx["url"]), stop=lambda ctx: ctx["info"] is not None),
            Step("read_page", read_page, when=lambda ctx: bool(ctx["url"])),
        ]

    def run(self, company, city, url):
        ctx = self.orch.run({"company": company, "city": city, "url": url, "info": None})
        return ctx["url"], ctx["info"] or {}
