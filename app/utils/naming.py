from __future__ import annotations

import re
from typing import Any

# Naming convention policy:
# - Frontend uses camelCase
# - Backend uses snake_case internally
# - This module handles conversion at API boundaries:
#   - Incoming requests (camelCase) -> snake_case for backend processing
#   - Outgoing responses (snake_case) -> camelCase for frontend consumption


def to_camel_case(snake_str: str) -> str:
    """Convert snake_case string to camelCase."""
    components = snake_str.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


def to_snake_case(camel_str: str) -> str:
    """Convert camelCase string to snake_case."""
    result = re.sub(r"([A-Z])", r"_\1", camel_str)
    return result.lower().lstrip("_")


def dict_to_camel_case(obj: Any) -> Any:
    """
    Recursively convert all dict keys from snake_case to camelCase.
    Used for transforming backend responses to frontend format.
    """
    if isinstance(obj, dict):
        return {to_camel_case(k): dict_to_camel_case(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [dict_to_camel_case(item) for item in obj]
    return obj


def dict_to_snake_case(obj: Any) -> Any:
    """
    Recursively convert all dict keys from camelCase to snake_case.
    Used for transforming frontend payloads to backend format.
    """
    if isinstance(obj, dict):
        return {to_snake_case(k): dict_to_snake_case(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [dict_to_snake_case(item) for item in obj]
    return obj


def transform_frontend_to_canonical(obj: Any) -> Any:
    """Transform frontend payload (camelCase) to canonical snake_case format.

    - Recursively converts all dict keys from camelCase to snake_case.
    - Frontend sends camelCase, backend processes snake_case.
    - Returns the transformed object with all keys in snake_case.
    """
    return dict_to_snake_case(obj)
