"""Narrow JSON values at boundaries that require objects, arrays, or text."""

from __future__ import annotations

import json
from typing import cast

from .type_defs import JsonObject, JsonValue


def loads(value: str | bytes) -> JsonValue:
    # The stdlib decoder produces exactly this recursive value family.
    return cast(JsonValue, json.loads(value))


def object_value(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise RuntimeError("Expected a JSON object.")
    return value


def text_value(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise RuntimeError("Expected JSON text.")
    return value


def array_value(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise RuntimeError("Expected a JSON array.")
    return value
