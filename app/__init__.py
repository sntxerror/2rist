"""2rist — знаковые места вдоль маршрута."""

import os
from pathlib import Path

# Ключи держим в .env рядом с проектом (в .gitignore), чтобы сервис
# запускался одинаково из любого лаунчера, а не только из шелла с export.
_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ENV.exists():
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
