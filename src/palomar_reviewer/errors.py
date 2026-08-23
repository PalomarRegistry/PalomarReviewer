"""Shared application error surfaced as a concise CLI diagnostic."""


class ReviewerError(RuntimeError):
    """An expected reviewer input, trust, or operational failure."""


class DeterministicRegistrationError(ReviewerError):
    """A registration failure that unchanged inputs cannot fix by retrying."""


class SubmitterRenderabilityError(DeterministicRegistrationError):
    """The accepted source cannot render every compared declaration anchor."""

    def __init__(
        self,
        message: str,
        *,
        diagnostics: list[dict[str, object]],
        run_id: int,
        run_url: str,
    ) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics
        self.run_id = run_id
        self.run_url = run_url
