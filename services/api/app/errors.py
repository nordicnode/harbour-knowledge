from __future__ import annotations

from uuid import UUID, uuid4


class AppError(Exception):
    """An error normalised across providers and internal failures."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        correlation_id: UUID | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.correlation_id = correlation_id or uuid4()


def validation(message: str) -> AppError:
    return AppError("validation", message, 422)


def not_found(message: str) -> AppError:
    return AppError("not_found", message, 404)


def permission(message: str) -> AppError:
    return AppError("permission", message, 403)


def provider_unavailable(message: str = "The AI provider is unavailable.") -> AppError:
    return AppError("provider_unavailable", message, 503)


def provider_rejected(message: str) -> AppError:
    return AppError("provider_rejected", message, 502)


def rate_limited(message: str = "Rate limited by the AI provider.") -> AppError:
    return AppError("rate_limited", message, 429)
