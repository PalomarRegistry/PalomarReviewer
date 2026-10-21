"""Shared application error surfaced as a concise CLI diagnostic."""


class ReviewerError(RuntimeError):
    """An expected reviewer input, trust, or operational failure."""

    render_request_finished = False


class DeterministicRegistrationError(ReviewerError):
    """A registration failure that unchanged inputs cannot fix by retrying."""


class RegistrationDeferred(ReviewerError):
    """A registration is safe but another durable allocation must finish first."""


class RenderPending(ReviewerError):
    """A durable render request is still running or awaiting dispatch discovery."""


class RenderRequestError(ReviewerError):
    """A rendering request itself cannot be resumed safely."""

    render_request_finished = True


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
