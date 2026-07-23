class DyroError(RuntimeError):
    """A user-actionable command error."""


class ValidationError(DyroError):
    """Configuration or manifest violates the public contract."""
