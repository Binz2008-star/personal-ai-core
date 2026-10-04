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


class ContextOverflowError(CoreError):
    """A turn's context does not fit the model's window, so it was not sent.

    ADR-005: no silent overflow. Sending it anyway lets the server drop
    whatever it chooses without saying so. `spoken_for` is what the turn needs
    before any evidence; `context_window` is what the model has."""

    def __init__(self, *, spoken_for: int, context_window: int, history_tokens: int) -> None:
        super().__init__(
            f"the conversation needs about {spoken_for} tokens and the model's window is "
            f"{context_window}: the turn was not sent"
        )
        self.spoken_for = spoken_for
        self.context_window = context_window
        self.history_tokens = history_tokens

