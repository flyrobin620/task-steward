"""Unified error codes (DN006 §5.2).

Plugin interfaces in the design are written as returning ``Result[...]``. The implementation
signals failure by raising :class:`TswError` carrying one of these codes instead; success
returns the value directly.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    UNAVAILABLE = "UNAVAILABLE"  # retryable
    REJECTED = "REJECTED"  # explicit refusal (guard failed, conflict, invalid input)
    UNKNOWN = "UNKNOWN"  # outcome unknown, reconcile before retrying
    UNSUPPORTED = "UNSUPPORTED"  # capability not supported
    AUTH = "AUTH"  # credential problem
    QUIESCED = "QUIESCED"  # data directory is quiesced


EXIT_CODES: dict[ErrorCode, int] = {
    ErrorCode.REJECTED: 3,
    ErrorCode.QUIESCED: 4,
    ErrorCode.UNAVAILABLE: 5,
    ErrorCode.UNKNOWN: 6,
    ErrorCode.UNSUPPORTED: 7,
    ErrorCode.AUTH: 8,
}


class TswError(Exception):
    """An error with a unified code, a short message and machine-readable details."""

    def __init__(self, code: ErrorCode, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = details

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "code": self.code.value, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


def rejected(message: str, **details: Any) -> TswError:
    return TswError(ErrorCode.REJECTED, message, **details)
