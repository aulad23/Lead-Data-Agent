"""Lead agent package.

On Windows, keys saved with `setx NAME value` are only visible to programs started afterwards.
So that the agent works without restarting VS Code / the terminal, missing keys are read from the
user's saved environment (HKCU\\Environment) at import time.
"""
import os
import sys

KEYS = ("APIFY_TOKEN", "GEMINI_API_KEY", "GEMINI_MODEL", "GROQ_API_KEY", "GROQ_MODEL", "OLLAMA_MODEL")

if sys.platform == "win32":
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as _k:
            for _name in KEYS:
                if not os.environ.get(_name):
                    try:
                        _val = str(winreg.QueryValueEx(_k, _name)[0]).strip()
                        if _val:
                            os.environ[_name] = _val
                    except FileNotFoundError:
                        pass
    except OSError:
        pass
