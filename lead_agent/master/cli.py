"""Command line for the master agent: companies | posts | learn | memory.

  python -m lead_agent companies --industry dental --city Austin --state TX --limit 30 --linkedin
  python -m lead_agent posts --keywords "dental practice growth" --limit 10 --lookup
  python -m lead_agent learn output/reviewed.csv      # teach memories from human review
  python -m lead_agent memory                         # main agent shows all memories
"""
import argparse
import time

from lead_agent.core.utils import US_STATES
from lead_agent.master.main_agent import MainAgent


def main():
    ap = argparse.ArgumentParser(description="Multi-agent lead system (US first)")
    sub = ap.add_subparsers(dest="mode", required=True)
    c = sub.add_parser("companies")
    c.add_argument("--industry", required=True)
    c.add_argument("--city", required=True)
    c.add_argument("--state", required=True)
    c.add_argument("--limit", type=int, default=30)
    c.add_argument("--linkedin", action="store_true", help="public LinkedIn: decision-maker search + company size")
    c.add_argument("--exclude", help="CRM / existing customers Excel or CSV: skip companies already in it")
    c.add_argument("--keep-crm", action="store_true", help="keep CRM matches in the output, marked in_crm")
    c.add_argument("--apify", action="store_true",
                   help="PAID fallback: Apify LinkedIn employees, only for companies where free sources found no decision-maker")
    c.add_argument("--apify-max", type=int, default=10, help="max decision-makers per company from Apify (default 10)")
    c.add_argument("--max-employees", type=int, default=None,
                   help="drop companies whose LinkedIn size lower bound is above N (unknown size is kept); turns --linkedin on")
    p = sub.add_parser("posts")
    p.add_argument("--keywords", required=True)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--lookup", action="store_true", help="look up each commenter's headline (slower)")
    sub.add_parser("learn").add_argument("file")
    sub.add_parser("memory")
    a = ap.parse_args()
    t0 = time.time()
    m = MainAgent()
    if a.mode == "companies":
        st = a.state.upper()
        if st not in US_STATES:
            ap.error("state must be a 2-letter US state code")
        m.companies(a.industry, a.city, st, a.limit, a.linkedin, a.exclude, a.keep_crm, a.apify, a.apify_max,
                    a.max_employees)
    elif a.mode == "posts":
        m.posts_mode(a.keywords, a.limit, a.lookup)
    elif a.mode == "learn":
        m.learn(a.file)
    else:
        m.show_memory()
    print(f"time {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
