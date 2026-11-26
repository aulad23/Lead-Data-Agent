"""DuckDuckGo meta-search connector (cached 7 days, retried across backends)."""
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from ddgs import DDGS

from lead_agent.core.utils import _db


_POOL = ThreadPoolExecutor(max_workers=4)
DDG_TIMEOUT = 25  # seconds; ddgs can hang forever on a dead connection


def _ddg_once(q: str, n: int, backend: str) -> list[dict]:
    fut = _POOL.submit(lambda: DDGS(timeout=10).text(q, region="us-en", max_results=n, backend=backend))
    try:
        return fut.result(timeout=DDG_TIMEOUT) or []
    except FutureTimeout:
        fut.cancel()
        raise TimeoutError(f"ddg {backend} timed out")


def _ddg(q: str, n: int) -> list[dict]:
    """DuckDuckGo meta-search with a retry; the free endpoint is flaky. Results cached 7 days."""
    key = hashlib.sha1(f"ddg|{q}|{n}".encode()).hexdigest()
    row = _db.execute("SELECT body, ts FROM http WHERE k=?", (key,)).fetchone()
    if row and time.time() - row[1] < 7 * 86400:
        return json.loads(row[0])
    best = []
    for attempt, backend in enumerate(("auto", "bing", "brave", "mojeek")):
        try:
            hits = _ddg_once(q, n, backend)
            if len(hits) >= max(2, n // 3):
                best = hits
                break
            best = max(best, hits, key=len)
        except Exception:
            time.sleep(1 + attempt * 2)
    if best:
        _db.execute("REPLACE INTO http VALUES (?,?,?,?)", (key, 200, json.dumps(best), time.time()))
        _db.commit()
    return best
