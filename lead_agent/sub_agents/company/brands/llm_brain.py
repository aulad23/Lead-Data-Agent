"""Optional free LLM "brain". Picks the first available provider:
  1. GEMINI_API_KEY  (Google AI Studio free key, no card)      model: GEMINI_MODEL (default gemini-flash-latest)
  2. GROQ_API_KEY    (Groq free tier, no card)                 model: GROQ_MODEL   (default openai/gpt-oss-20b)
  3. Ollama on localhost:11434 (runs on this PC, fully free)   model: OLLAMA_MODEL (default llama3.1)
No provider -> available() is False and the agents keep using their rules.
Calls are throttled (free tiers allow ~10 requests/minute) and every answer is cached by the caller's memory.
"""
import json
import os
import re
import time

import requests

_last = 0.0
_provider: str | None = None


def _detect() -> str:
    global _provider
    if _provider is None:
        if os.getenv("GEMINI_API_KEY"):
            _provider = "gemini"
        elif os.getenv("GROQ_API_KEY"):
            _provider = "groq"
        else:
            try:
                requests.get("http://localhost:11434/api/tags", timeout=2).raise_for_status()
                _provider = "ollama"
            except requests.RequestException:
                _provider = ""
    return _provider


def available() -> bool:
    return bool(_detect())


def name() -> str:
    return _detect() or "rules only"


def ask_json(prompt: str, gap: float = 6.5) -> dict | None:
    """Send a prompt that asks for JSON; return the parsed dict or None on any failure."""
    global _last
    p = _detect()
    if not p:
        return None
    wait = _last + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    _last = time.time()
    try:
        if p == "gemini":
            model = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              params={"key": os.environ["GEMINI_API_KEY"]}, timeout=60,
                              json={"contents": [{"parts": [{"text": prompt}]}],
                                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0}})
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        elif p == "groq":
            r = requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=60,
                              headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
                              json={"model": os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"), "temperature": 0,
                                    "response_format": {"type": "json_object"},
                                    "messages": [{"role": "user", "content": prompt}]})
            text = r.json()["choices"][0]["message"]["content"]
        else:
            r = requests.post("http://localhost:11434/api/chat", timeout=180,
                              json={"model": os.getenv("OLLAMA_MODEL", "llama3.1"), "format": "json", "stream": False,
                                    "messages": [{"role": "user", "content": prompt}]})
            text = r.json()["message"]["content"]
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def verify_company(company: str, industry: str, page_text: str, candidates: list[dict]) -> dict | None:
    """One call per company: industry match + who is really the decision-maker + which 'names' are not people."""
    cands = "\n".join(f"- {p['name']} | {p.get('title', '')}" for p in candidates[:15]) or "(none)"
    prompt = f"""You check B2B lead data for a US company. Answer ONLY with JSON.
Company: {company}
Target industry: {industry or "unknown"}
Candidate people (name | title) extracted by rules, may contain mistakes:
{cands}
Website text (truncated):
\"\"\"{page_text[:6000]}\"\"\"

Return:
{{"industry_match": number 0-1 (does this company belong to the target industry),
 "industry_label": short industry of the company,
 "decision_maker": {{"name": "...", "title": "...", "seniority": "owner|c-level|director|manager|staff"}} or null,
 "not_people": [candidate names that are not real person names, e.g. "Criminal Defense"],
 "other_people": [{{"name": "...", "title": "..."}}]  (other real staff seen in the text, max 5)}}
Use only names that appear in the website text or candidate list. Prefer owner/founder/partner/director."""
    return ask_json(prompt)
