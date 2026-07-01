"""Per-domain scraping policies and a simple rate limiter."""

import asyncio
import time
from dataclasses import dataclass

from app.core.config import Settings


@dataclass
class DomainPolicy:
    """Scraping policy for a single domain."""

    allowed: bool = True
    min_delay_seconds: float = 5.0
    max_concurrent_requests: int = 1
    default_ttl_seconds: int | None = None
    default_mode: str | None = None

    @classmethod
    def from_config(cls, config: dict | None) -> "DomainPolicy":
        if not config:
            return cls()
        return cls(
            allowed=config.get("allowed", True),
            min_delay_seconds=config.get("min_delay_seconds", 5.0),
            max_concurrent_requests=config.get("max_concurrent_requests", 1),
            default_ttl_seconds=config.get("default_ttl_seconds"),
            default_mode=config.get("default_mode"),
        )


class DomainRateLimiter:
    """Simple per-domain rate limiter.

    Tracks last-request timestamps and active request counts per domain.
    Not persisted across restarts.
    """

    def __init__(self, settings: Settings) -> None:
        self._policies: dict[str, DomainPolicy] = {
            domain: DomainPolicy.from_config(cfg) for domain, cfg in settings.domains.items()
        }
        self._last_request: dict[str, float] = {}
        self._active: dict[str, int] = {}

    def get_policy(self, domain: str) -> DomainPolicy:
        """Return the policy for *domain*, or a default blank policy."""
        return self._policies.get(domain, DomainPolicy())

    def set_policy(self, domain: str, policy: DomainPolicy) -> None:
        """Override the policy for *domain* at runtime."""
        self._policies[domain] = policy

    @property
    def default_policy(self) -> DomainPolicy:
        return DomainPolicy()

    def can_proceed(self, domain: str) -> bool:
        """Check whether a new request to *domain* is allowed right now."""
        policy = self.get_policy(domain)

        if not policy.allowed:
            return False

        # Max concurrency check
        active = self._active.get(domain, 0)
        if active >= policy.max_concurrent_requests:
            return False

        # Min delay check
        last = self._last_request.get(domain, 0.0)
        if last > 0 and (time.monotonic() - last) < policy.min_delay_seconds:
            return False

        return True

    async def wait_if_needed(self, domain: str) -> None:
        """Block until a request to *domain* is allowed, respecting rate limits."""
        policy = self.get_policy(domain)

        while True:
            if not policy.allowed:
                raise PermissionError(f"Domain '{domain}' is not allowed")

            active = self._active.get(domain, 0)
            if active >= policy.max_concurrent_requests:
                await asyncio.sleep(0.5)
                continue

            last = self._last_request.get(domain, 0.0)
            if last > 0:
                wait = policy.min_delay_seconds - (time.monotonic() - last)
                if wait > 0:
                    await asyncio.sleep(wait)

            break

    def acquire(self, domain: str) -> None:
        """Mark a request as in-flight for *domain*."""
        self._active[domain] = self._active.get(domain, 0) + 1

    def release(self, domain: str) -> None:
        """Mark an in-flight request as completed for *domain*."""
        self._active[domain] = max(0, self._active.get(domain, 0) - 1)
        self._last_request[domain] = time.monotonic()
