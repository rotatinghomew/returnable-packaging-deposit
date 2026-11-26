"""Errors raised by the shared turnover-box service."""


class TurnBoxError(Exception):
    """Base class for all deliberate service errors."""


class ValidationError(TurnBoxError):
    """A record or command does not satisfy the exchange contract."""


class NotFoundError(TurnBoxError):
    """A referenced business object does not exist."""


class ConflictError(TurnBoxError):
    """The request conflicts with an existing record or business state."""


class BusinessRuleError(TurnBoxError):
    """The request is understood, but a business rule rejects it."""
