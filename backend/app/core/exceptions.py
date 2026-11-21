"""Structured application exceptions mapped to HTTP errors in the API layer."""
from __future__ import annotations


class NexusError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details or {}


class AuthenticationError(NexusError):
    status_code, code = 401, "authentication_failed"


class PermissionDenied(NexusError):
    status_code, code = 403, "permission_denied"


class NotFound(NexusError):
    status_code, code = 404, "not_found"


class Conflict(NexusError):
    status_code, code = 409, "conflict"


class ValidationFailed(NexusError):
    status_code, code = 422, "validation_failed"


class UnsupportedFormat(ValidationFailed):
    code = "unsupported_format"


class IngestionError(NexusError):
    status_code, code = 500, "ingestion_failed"


class RetrievalError(NexusError):
    status_code, code = 500, "retrieval_failed"


class RateLimited(NexusError):
    status_code, code = 429, "rate_limited"


class ConfigurationError(NexusError):
    status_code, code = 500, "configuration_error"


class WorkflowFailed(NexusError):
    status_code, code = 502, "workflow_failed"


class BudgetExceeded(RateLimited):
    code = "budget_exceeded"


class LLMUnavailable(NexusError):
    status_code, code = 503, "llm_unavailable"
