"""Provider-agnostic transcript provider contract.

A :class:`TranscriptProvider` adapts a concrete YouTube transcript backend
(Camofox, the ``youtube-transcript-api`` library, ``yt-dlp``, ...) to the
stable domain model defined in :mod:`app.transcript.models`.  Clients and
the service only ever interact with the domain types, never with
provider-specific request/response objects.

Implementations must be simple to fake in unit tests: the protocol is
structural, so any object with a ``name`` property plus ``health()`` and
``fetch()`` methods satisfies it.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.transcript.models import TranscriptResponse


class TranscriptHealth(BaseModel):
    """Current availability/health of one transcript provider.

    ``available`` is the routing signal the router probes before selecting
    a provider; ``reason`` is a short, credential-safe explanation for
    health reports and warnings.
    """

    model_config = ConfigDict(extra="forbid")

    available: bool = Field(
        default=True,
        description=(
            "``True`` when the provider can serve transcript requests right "
            "now (e.g. the backing service is reachable / the library is "
            "importable)."
        ),
    )
    reason: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Short, safe explanation when unavailable (never contains "
            "credentials, service URLs with user-info, or upstream text)."
        ),
    )


@runtime_checkable
class TranscriptProvider(Protocol):
    """Structural contract every transcript provider must satisfy."""

    @property
    def name(self) -> str:
        """Stable, unique provider identifier (e.g. ``"camofox"``)."""
        ...

    async def health(self) -> TranscriptHealth:
        """Return this provider's current :class:`TranscriptHealth`.

        Implementations may cache or probe, but the result must reflect
        what the provider can do *now*.  A probe that cannot complete
        within the router's probe timeout is treated by the router as an
        unavailable provider.
        """
        ...

    async def fetch(self, video_id: str, languages: list[str]) -> TranscriptResponse:
        """Fetch the transcript for ``video_id`` and return the normalized response.

        ``languages`` is the ordered list of preferred caption languages
        (BCP-47, lowercased).

        Implementations must:

        - Set ``metadata.provider`` to their own ``name``.
        - Map the backend payload into :class:`~app.transcript.models.
          TranscriptSegment` fields; raw backend dictionaries must never
          be exposed.
        - Report ``language`` / ``is_generated`` only when the backend
          provides them (``None`` otherwise — never invent data).
        - Raise one of the project transcript errors from
          :mod:`app.core.errors` on failure (no captions, language
          unavailable, video unavailable/private, provider blocked,
          provider unavailable) instead of returning broken results.
        """
        ...
