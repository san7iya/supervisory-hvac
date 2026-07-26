"""Minimal .env loader. Avoids adding python-dotenv as a dependency for a
handful of KEY=VALUE lines (GROQ_API_KEY currently)."""
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def load_dotenv(path: Path = None) -> None:
    path = path or (_REPO_ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())