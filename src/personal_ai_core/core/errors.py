"""Core error hierarchy."""


class CoreError(Exception):
    """Base for every error raised by the Core."""


class ConfigError(CoreError):
    """Configuration is missing or invalid."""


class ModelNotFoundError(CoreError):
    """A model was requested that the registry does not hold."""


class ProviderError(CoreError):
    """A model provider failed to produce a response."""


class InvariantViolation(CoreError):
    """A Core architectural invariant was violated.

    Raised rather than logged: an invariant that can be broken quietly is not
    an invariant.
    """


class PhaseNotImplementedError(CoreError):
    """A subsystem deliberately not built yet in this phase.

    Distinct from NotImplementedError so that a sealed-by-design boundary is
    never mistaken for an incomplete implementation.
    """


class EmbeddingError(CoreError):
    """An embedding provider failed to produce usable vectors."""


class IndexError_(CoreError):
    """An index operation failed."""


class RetrievalError(CoreError):
    """Retrieval failed."""
