"""Single CLI:  python -m lead_agent <command> [args]  (each command forwards its args to the target main())."""
import importlib
import sys

COMMANDS = {
    "companies":  ("lead_agent.master.cli", "find companies + decision-makers for an industry / city"),
    "posts":      ("lead_agent.master.cli", "leads from LinkedIn post comments"),
    "learn":      ("lead_agent.master.cli", "learn from a client file (teach the agents memory)"),
    "memory":     ("lead_agent.master.cli", "show the agents' memory"),
    "excel":      ("lead_agent.workflows.client_file", "fill a client Excel/CSV file"),
    "inbox":      ("lead_agent.workflows.inbox", "process files dropped in inbox/ [--watch] [--apify]"),
    "influencer": ("lead_agent.sub_agents.influencer.agent", "track posts of LinkedIn influencers from a file"),
    "pipeline":   ("lead_agent.workflows.legacy_pipeline", "legacy single-pass pipeline (CSV output)"),
}
MASTER_CMDS = {"companies", "posts", "learn", "memory"}


def usage() -> str:
    lines = ["usage: python -m lead_agent <command> [args]   (python -m lead_agent <command> -h for command help)", "",
             "commands:"]
    lines += [f"  {name:<11} {desc}" for name, (_, desc) in COMMANDS.items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return
    cmd, rest = argv[0], argv[1:]
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd}\n\n{usage()}", file=sys.stderr)
        raise SystemExit(2)
    # master commands keep their own sub-command word (companies/posts/learn/memory) as first arg
    sys.argv = ["lead_agent", cmd, *rest] if cmd in MASTER_CMDS else [f"lead_agent {cmd}", *rest]
    importlib.import_module(COMMANDS[cmd][0]).main()


if __name__ == "__main__":
    main()
