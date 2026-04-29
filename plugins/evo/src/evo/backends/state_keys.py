from __future__ import annotations

import hashlib
import json
from typing import Any


def backend_state_key(name: str, config: dict[str, Any]) -> str:
    """Stable key for backend runtime state files."""
    payload = json.dumps(
        {"backend": name, "config": config or {}},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return digest
