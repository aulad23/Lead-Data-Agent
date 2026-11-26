"""DiscoveryAgent: finds companies."""
from lead_agent.master.orchestrator import Step
from lead_agent.sub_agents.base import SubAgent, log
from lead_agent.sub_agents.company.site_finder import merge
from lead_agent.sub_agents.discovery.brands.npi_registry import npi
from lead_agent.sub_agents.discovery.brands.openstreetmap import osm
from lead_agent.sub_agents.discovery.brands.web_search import web_search


class DiscoveryAgent(SubAgent):
    """Finds companies. Sources are reorderable steps; stops once it has enough candidates."""
    name = "discovery"
    source_errors: dict = {}  # per run: {"osm": failed queries, "web": empty searches, ...}; read by the master for metrics

    def steps(self):
        def src(key, fn):
            def run(ctx):
                try:
                    rows = fn(ctx)
                except Exception as e:  # a crashing source must not end the run, and must not be silent
                    rows = []
                    ctx["errors"][key] = ctx["errors"].get(key, 0) + 1
                    log(self.name, f"{key} FAILED ({type(e).__name__}: {str(e)[:80]})")
                ctx["per"][key] = rows
                ctx["found"] += rows
                log(self.name, f"{key}: {len(rows)} companies" + (f" ({ctx['errors'][key]} errors)" if ctx["errors"].get(key) else ""))
                return bool(rows)
            def enough(ctx):  # enough candidates AND enough with a website (NPI rows have none; OSM/web add them)
                m = merge(list(ctx["found"]))
                return len(m) >= ctx["limit"] * 5 and sum(bool(c.get("website")) for c in m) >= ctx["limit"] * 2
            return Step(key, run, stop=enough, reorder=True)
        return [
            src("npi", lambda c: npi(c["industry"], c["city"], c["state"], c["limit"] * 2)),
            src("osm", lambda c: osm(c["industry"], f'{c["city"]}, {c["state"]}', c["limit"] * 2, c["errors"])),
            src("web", lambda c: web_search(c["industry"], f'{c["city"]}, {c["state"]}', c["limit"], errors=c["errors"])),  # already yields limit*4 candidates
        ]

    def run(self, industry, city, state, limit):
        ctx = self.orch.run({"industry": industry, "city": city, "state": state, "limit": limit, "found": [], "per": {}, "errors": {}},
                            scope=industry)
        self.source_errors = dict(ctx["errors"])
        for key in self.source_errors:  # a failed source is not a useless one: do not let it be skipped as "learned"
            st = self.orch._stat(industry, key)
            self.mem.remember("step", f"{industry}|{key}", {"tries": max(0, st["tries"] - 1), "useful": st["useful"]})
        for t in ctx.get("trace", []):
            if "skipped" in t:
                log(self.name, f"source skipped by learned stats: {t}")
        if self.source_errors:
            log(self.name, f"source errors: {self.source_errors}")
        return merge(ctx["found"]), ctx["per"]

    def record(self, industry, per, rows):
        """Learn which source gave companies that ended up with a decision-maker."""
        for s, found in per.items():
            names = {f["company"].lower() for f in found}
            self.mem.add_yield(industry, s, len(found), sum(1 for r in rows if r["company"].lower() in names and r["contact_name"]))
