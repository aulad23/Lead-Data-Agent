"""Central project paths. Never compute project paths from __file__ elsewhere; import them from here."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # project root (folder that contains lead_agent/)
DATA_DIR = ROOT / "data"
MEMORY_DIR = DATA_DIR / "memory"
CACHE_DB = DATA_DIR / "cache.sqlite"
OUTPUT_DIR = ROOT / "output"
INBOX_DIR = ROOT / "inbox"
OUTBOX_DIR = ROOT / "outbox"
SAMPLES_DIR = ROOT / "samples"
