from __future__ import annotations

import re
from typing import Any, List

# Strict single-case policy: snake_case only across API, backend, and DB.
# This module validates payloads and rejects any non-snake_case keys.

_SNAKE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def is_snake_case_key(key: str) -> bool:
    """Return True if key is snake_case (lowercase letters, digits, underscores; must start with a letter)."""
    return bool(_SNAKE_KEY_RE.fullmatch(key))


def _collect_invalid_keys(obj: Any, path: str = "") -> List[str]:
    """Recursively collect dotted paths of keys that are not snake_case."""
    invalid: List[str] = []

    if isinstance(obj, dict):
        for k, v in obj.items():
            # Only validate string keys
            if isinstance(k, str) and not is_snake_case_key(k):
                invalid.append(f"{path}.{k}" if path else k)
            # Recurse
            invalid.extend(_collect_invalid_keys(v, f"{path}.{k}" if path else k))
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            invalid.extend(
                _collect_invalid_keys(v, f"{path}[{idx}]" if path else f"[{idx}]")
            )

    return invalid


def transform_frontend_to_canonical(obj: Any) -> Any:
    """Enforce snake_case-only payloads.

    - Validates recursively that all dict keys are snake_case.
    - If any invalid keys are found, raises ValueError with a clear message listing offending keys.
    - Returns the object unchanged when valid (canonical form is snake_case already).
    """
    invalid = _collect_invalid_keys(obj)
    if invalid:
        raise ValueError(
            "Payload keys must be snake_case. Invalid keys: " + ", ".join(invalid)
        )
    return obj
