"""PostAgent: LinkedIn public posts."""
from lead_agent.connectors import linkedin
from lead_agent.master.orchestrator import Step
from lead_agent.sub_agents.base import SubAgent, log


class PostAgent(SubAgent):
    """LinkedIn public posts. Tries more query forms (hashtag, 'owner') only when it has too few posts."""
    name = "posts"

    def steps(self):
        def query(form):
            def run(ctx):
                q = form(ctx["keywords"])
                new = [u for u in linkedin.find_posts(q, ctx["limit"]) if u not in ctx["urls"]]
                ctx["urls"] += new
                log(self.name, f"query '{q}': {len(new)} new post URLs")
                return bool(new)
            return run
        enough = lambda ctx: len(ctx["urls"]) >= ctx["limit"]

        def read_all(ctx):
            for u in ctx["urls"][: ctx["limit"]]:
                p = linkedin.read_post(u)
                if not p:
                    log(self.name, f"not readable logged-out: {u[:70]}")
                    continue
                self.mem.remember("post", u, {"date": p["date"], "author": p["author"], "comments": p["comment_count"]})
                ctx["posts"].append(p)
                log(self.name, f"{p['date']} {p['author'][:30]:30} likes={p['likes']} comments {p['comments_visible']}/{p['comment_count']}")
            return bool(ctx["posts"])

        return [
            Step("plain_query", query(lambda k: k), stop=enough, reorder=True),
            Step("hashtag_query", query(lambda k: "#" + k.replace(" ", "")), when=lambda c: not enough(c), stop=enough, reorder=True),
            Step("owner_query", query(lambda k: f"{k} owner"), when=lambda c: not enough(c), stop=enough, reorder=True),
            Step("read_posts", read_all),
        ]

    def run(self, keywords, limit):
        return self.orch.run({"keywords": keywords, "limit": limit, "urls": [], "posts": []})["posts"]
