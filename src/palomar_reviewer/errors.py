"""Shared application error surfaced as a concise CLI diagnostic."""


class ReviewerError(RuntimeError):
    """An expected reviewer input, trust, or operational failure."""


class DeterministicRegistrationError(ReviewerError):
    """A registration failure that unchanged inputs cannot fix by retrying."""
