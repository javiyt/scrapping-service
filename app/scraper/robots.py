"""Robots.txt compliance checker."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field


@dataclass
class RobotRules:
    """Parsed robots.txt rules for a user agent."""

    disallow: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    crawl_delay: float | None = None
    sitemaps: list[str] = field(default_factory=list)


@dataclass
class CachedRule:
    """Cached robots.txt rules."""

    rules: RobotRules
    fetched_at: float = field(default_factory=time.time)
    ttl_seconds: float = 86400.0  # 24 hours

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.fetched_at) > self.ttl_seconds


class RobotsCache:
    """Per-domain robots.txt cache."""

    def __init__(self, default_ttl_seconds: float = 86400.0) -> None:
        self._cache: dict[str, CachedRule] = {}
        self._default_ttl = default_ttl_seconds

    def get(self, domain: str) -> RobotRules | None:
        """Get cached rules for domain, or None if not cached or expired."""
        cached = self._cache.get(domain)
        if cached is None or cached.is_expired:
            return None
        return cached.rules

    def set(self, domain: str, rules: RobotRules, ttl_seconds: float | None = None) -> None:
        """Cache rules for domain."""
        ttl = ttl_seconds or self._default_ttl
        self._cache[domain] = CachedRule(rules=rules, ttl_seconds=ttl)

    def invalidate(self, domain: str) -> None:
        """Remove cached rules for domain."""
        self._cache.pop(domain, None)


class RobotChecker:
    """Check if a URL is allowed per robots.txt rules."""

    def __init__(self, rules: RobotRules) -> None:
        self._rules = rules

    def is_allowed(self, url: str) -> bool:
        """Check if URL is allowed.

        Uses longest-match rule for paths.
        """
        path = _url_to_path(url)

        # Check Allow rules (most specific first)
        for pattern in sorted(self._rules.allow, key=len, reverse=True):
            if _path_matches_pattern(path, pattern):
                return True

        # Check Disallow rules
        for pattern in sorted(self._rules.disallow, key=len, reverse=True):
            if _path_matches_pattern(path, pattern):
                return False

        return True

    def get_crawl_delay(self) -> float | None:
        """Get the crawl delay for this domain."""
        return self._rules.crawl_delay

    def get_sitemaps(self) -> list[str]:
        """Get sitemap URLs from robots.txt."""
        return list(self._rules.sitemaps)


def _url_to_path(url: str) -> str:
    """Extract path from URL, preserving trailing slash on root."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    path = parsed.path or "/"
    if not path.endswith("/") and parsed.query:
        # Path with query, keep trailing slash
        pass
    return path


def _path_matches_pattern(path: str, pattern: str) -> bool:
    """Check if path matches a robots.txt pattern.

    Patterns are case-sensitive, and * matches any characters.
    """
    # Normalize: ensure pattern starts with /
    if not pattern.startswith("/"):
        pattern = "/" + pattern

    # Convert robots.txt glob to regex
    regex_pattern = "^" + re.escape(pattern).replace(r"\*", "[^/]*") + "$"
    return re.match(regex_pattern, path) is not None


def parse_robots_txt(robots_content: str, user_agent: str = "*") -> tuple[RobotRules, list[str]]:
    """Parse robots.txt content for a specific user agent.

    Returns:
        Tuple of (RobotRules for user_agent, global_sitemaps)
    """
    rules = RobotRules()
    sitemaps: list[str] = []
    current_agent: str | None = None

    lines = robots_content.splitlines()
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue

        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()

        if key == "user-agent":
            current_agent = value

        elif key == "disallow" and _agent_matches(current_agent, user_agent):
            if value:
                rules.disallow.append(value)

        elif key == "allow" and _agent_matches(current_agent, user_agent):
            if value:
                rules.allow.append(value)

        elif key == "crawl-delay" and _agent_matches(current_agent, user_agent):
            try:
                rules.crawl_delay = float(value)
            except ValueError:
                pass

        elif key == "sitemap" and current_agent is None:
            if value:
                sitemaps.append(value)

    return rules, sitemaps


def _agent_matches(agent: str | None, target_agent: str) -> bool:
    """Check if agent line matches target agent."""
    if agent is None:
        return True  # Global directive
    if agent == "*":
        return True  # Wildcard matches all
    return agent.lower() == target_agent.lower()
