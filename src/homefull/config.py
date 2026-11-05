"""Standalone configuration: environment variables and HomeFull's own .env only."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_env(path):
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key.replace("_", "").isalnum() and not key[0].isdigit():
            os.environ.setdefault(key, value)


load_env(ROOT / ".env")
DEFAULT_DATA = ROOT / "data" if (ROOT / "pyproject.toml").exists() else Path.home() / ".homefull"
DATA = Path(os.environ.get("HOMEFULL_DATA", os.environ.get("RENTBOT_DATA", DEFAULT_DATA))).expanduser()
TIMEZONE = os.environ.get("HOMEFULL_TIMEZONE", "Europe/Kyiv")
