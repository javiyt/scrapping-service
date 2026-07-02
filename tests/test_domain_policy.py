"""Tests for domain policy and rate limiter."""

import pytest

from app.core.config import Settings
from app.scraper.domain_policy import DomainPolicy, DomainRateLimiter


class TestDomainPolicy:
    def test_default_policy(self):
        policy = DomainPolicy()
        assert policy.allowed is True
        assert policy.min_delay_seconds == 5.0
        assert policy.max_concurrent_requests == 1
        assert policy.default_ttl_seconds is None
        assert policy.default_mode is None

    def test_from_config_with_none(self):
        policy = DomainPolicy.from_config(None)
        assert policy.allowed is True

    def test_from_config_with_dict(self):
        policy = DomainPolicy.from_config(
            {
                "allowed": False,
                "min_delay_seconds": 10.0,
                "max_concurrent_requests": 3,
                "default_ttl_seconds": 7200,
                "default_mode": "http",
            }
        )
        assert policy.allowed is False
        assert policy.min_delay_seconds == 10.0
        assert policy.max_concurrent_requests == 3
        assert policy.default_ttl_seconds == 7200
        assert policy.default_mode == "http"

    def test_from_config_partial(self):
        policy = DomainPolicy.from_config({"allowed": False})
        assert policy.allowed is False
        # Other fields should use defaults
        assert policy.min_delay_seconds == 5.0


class TestDomainRateLimiter:
    def test_get_policy_returns_default_for_unknown(self):
        limiter = DomainRateLimiter(Settings(_env_file=None))
        policy = limiter.get_policy("unknown.com")
        assert policy.allowed is True

    def test_set_policy_overrides(self):
        limiter = DomainRateLimiter(Settings(_env_file=None))
        policy = DomainPolicy(allowed=False)
        limiter.set_policy("test.com", policy)
        assert limiter.get_policy("test.com").allowed is False

    def test_can_proceed_returns_true_for_unknown(self):
        limiter = DomainRateLimiter(Settings(_env_file=None))
        assert limiter.can_proceed("fresh.com") is True

    def test_can_proceed_disallowed_domain(self):
        settings = Settings(
            domains={"blocked.com": {"allowed": False}},
            _env_file=None,
        )
        limiter = DomainRateLimiter(settings)
        assert limiter.can_proceed("blocked.com") is False

    def test_acquire_and_release(self):
        limiter = DomainRateLimiter(Settings(_env_file=None))
        # Set a policy with no min delay so release doesn't trigger rate limit.
        from app.scraper.domain_policy import DomainPolicy

        limiter.set_policy("example.com", DomainPolicy(min_delay_seconds=0))
        limiter.acquire("example.com")
        # One active request, max_concurrency=1, so can_proceed should be False
        assert limiter.can_proceed("example.com") is False
        limiter.release("example.com")
        assert limiter.can_proceed("example.com") is True

    @pytest.mark.asyncio
    async def test_wait_if_needed_disallowed_raises(self):
        settings = Settings(
            domains={"evil.com": {"allowed": False}},
            _env_file=None,
        )
        limiter = DomainRateLimiter(settings)
        with pytest.raises(PermissionError, match="not allowed"):
            await limiter.wait_if_needed("evil.com")

    def test_default_policy_property(self):
        limiter = DomainRateLimiter(Settings(_env_file=None))
        policy = limiter.default_policy
        assert policy.allowed is True
