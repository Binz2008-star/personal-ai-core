"""Core error hierarchy."""


class CoreError(Exception):
    """Base for every error raised by the Core."""


class ConfigError(CoreError):
    """Configuration is missing or invalid."""


class ModelNotFoundError(CoreError):
    """A model was requested that the registry does not hold."""


class ProviderError(CoreError):
    """A model provider failed to produce a response.

    The message is for the person at the terminal. What a durable record
    keeps is `kind`, one word from KINDS, and `status`, the HTTP status when
    there was one -- never the message, which can quote a host, a path, or
    whatever the server sent back (gap analysis P1-8).
    """

    KINDS = ("unreachable", "timeout", "http_status", "invalid_response",
             "invalid_request", "unclassified")

    def __init__(self, message: str = "", *, kind: str = "unclassified",
                 status: int | None = None) -> None:
        if kind not in self.KINDS:
            raise ValueError(f"unknown provider failure kind: {kind}")
        super().__init__(message)
        self.kind = kind
        self.status = status


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
    before any evidence; `context_window` is what the model has.

    `cause` says what to shorten (R1), because the advice differs and a new
    session helps with neither:
      - "message": this message alone does not fit beside the fixed reserves;
      - "fixed_reserves": the identity (the profile) and the other fixed
        reserves leave no room for any message;
      - "turn": anything else -- the history is windowed (P1-3), so this is
        a defensive check, not an expected outcome.
    The text is a sentence fragment a caller can print after a colon."""

    CAUSES = ("message", "fixed_reserves", "turn")

    def __init__(
        self,
        *,
        spoken_for: int,
        context_window: int,
        history_tokens: int,
        fixed_tokens: int | None = None,
        cause: str = "turn",
    ) -> None:
        if cause not in self.CAUSES:
            raise ValueError(f"unknown cause: {cause!r}")
        fixed = spoken_for - history_tokens if fixed_tokens is None else fixed_tokens
        if cause == "message":
            text = (
                f"your message is about {history_tokens} tokens; shorten it (the model's "
                f"window is {context_window} tokens, and the profile and fixed reserves "
                f"need about {fixed} of them)"
            )
        elif cause == "fixed_reserves":
            text = (
                f"the profile and fixed reserves need about {fixed} of the model's "
                f"{context_window} tokens; shorten the profile"
            )
        else:
            text = (
                f"this turn needs about {spoken_for} tokens and the model's window is "
                f"{context_window}"
            )
        super().__init__(text)
        self.spoken_for = spoken_for
        self.context_window = context_window
        self.history_tokens = history_tokens
        self.fixed_tokens = fixed
        self.cause = cause


class RollbackIncomplete(CoreError):
    """Some touched files could not be restored; `unrestored` names them.

    Raised by the agent's checkpoints after every other file was restored.
    The unrestored files keep their checkpoints, so a later rollback can try
    them again."""

    def __init__(self, restored: tuple[str, ...], unrestored: tuple[str, ...]) -> None:
        super().__init__(f"rollback incomplete; not restored: {', '.join(unrestored)}")
        self.restored = restored
        self.unrestored = unrestored

