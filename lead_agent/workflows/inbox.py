"""Inbox runner: drop client Excel/CSV files into  inbox/  and the agent processes them.

  python -m lead_agent inbox            # process every file in inbox/ once
  python -m lead_agent inbox --watch    # keep watching inbox/ (checks every 30 s)
  (or double-click run_inbox.bat)

For each file the main agent reads the columns, decides per row which agents to call, fills the empty
client cells (highlighted yellow) and writes  outbox/<file>-filled.xlsx .
The original file is moved to  inbox/done/  (or inbox/failed/ with an error note).
Optional: put a CRM export in  inbox/crm/  and every file is checked against it (agent_in_crm column).
"""
import argparse
import shutil
import time
import traceback
from pathlib import Path

from lead_agent.config.settings import INBOX_DIR, OUTBOX_DIR
from lead_agent.workflows.client_file import run

INBOX, OUTBOX = INBOX_DIR, OUTBOX_DIR
DONE, FAILED, CRM_DIR = INBOX / "done", INBOX / "failed", INBOX / "crm"
TYPES = {".xlsx", ".xls", ".csv"}


def pending() -> list[Path]:
    return sorted(p for p in INBOX.iterdir()
                  if p.is_file() and p.suffix.lower() in TYPES and not p.name.startswith(("~$", ".")))


def process_all(use_apify: bool = False) -> int:
    for d in (INBOX, OUTBOX, DONE, FAILED, CRM_DIR):
        d.mkdir(parents=True, exist_ok=True)
    crm = next((p for p in sorted(CRM_DIR.iterdir()) if p.suffix.lower() in TYPES), None)
    files = pending()
    for f in files:
        print(f"\n=== {f.name} ===")
        try:
            out = run(str(f), crm_path=str(crm) if crm else None, out_dir=str(OUTBOX), use_apify=use_apify)
            shutil.move(str(f), DONE / f"{time.strftime('%Y%m%d-%H%M')}-{f.name}")
            print(f"[inbox] OK -> {out}")
        except PermissionError:
            print(f"[inbox] {f.name} is open in Excel; close it and run again")
        except Exception as e:  # keep going with the other files
            shutil.move(str(f), FAILED / f.name)
            (FAILED / f"{f.stem}-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            print(f"[inbox] FAILED {f.name}: {e}")
    return len(files)


def main():
    ap = argparse.ArgumentParser(description="Process client files dropped into inbox/")
    ap.add_argument("--watch", action="store_true", help="keep watching the inbox folder")
    ap.add_argument("--apify", action="store_true", help="PAID fallback: Apify LinkedIn employees when free sources find none")
    a = ap.parse_args()
    n = process_all(a.apify)
    if not a.watch:
        print(f"\n[inbox] processed {n} file(s). Results in {OUTBOX}")
        return
    print(f"[inbox] watching {INBOX} (Ctrl+C to stop)")
    while True:
        time.sleep(30)
        if pending():
            process_all(a.apify)


if __name__ == "__main__":
    main()
