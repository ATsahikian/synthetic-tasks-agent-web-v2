from __future__ import annotations

from typing import Self


class EngineError(Exception):
    """An error that can be returned by the REST interface."""

    def __init__(
        self: Self, message: str, *, status: int = 400, code: str = "invalid_request"
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class CorruptLogError(EngineError):
    def __init__(self: Self, message: str) -> None:
        super().__init__(message, status=503, code="corrupt_event_log")
