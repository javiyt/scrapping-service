"""Crawler implementation with domain policies, robots.txt, and limits."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.core.config import Settings
from app.core.security import validate_url
from app.scraper.domain_policy import DomainRateLimiter
from app.scraper.errors import (
    CrawlCancelledError,
    CrawlLimitError,
    RobotsError,
    SitemapError,
)
from app.scraper.http_fetcher import HttpFetcher
from app.scraper.robots import RobotChecker, RobotRules, RobotsCache, parse_robots_txt
from app.scraper.sitemapper import fetch_sitemap


@dataclass
class CrawlConfig:
    """Configuration for a crawl job."""

    start_urls: list[str] = field(default_factory=list)
    sitemap_url: str | None = None
    max_pages: int = 100
    max_depth: int = 5
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    same_domain_only: bool = False
    include_subdomains: bool = False
    allowed_extensions: list[str] = field(default_factory=list)
    obey_robots_txt: bool = True
    timeout_seconds: int = 60
    global_concurrency: int = 10
    per_domain_concurrency: int = 2
    max_duration_seconds: int = 1800

    def validate(self, settings: Settings) -> None:
        """Validate config against hard server-side limits."""
        max_pages = getattr(settings, "crawler_max_pages", 1000)
        max_depth = getattr(settings, "crawler_max_depth", 10)
        max_duration = getattr(settings, "crawler_max_duration_seconds", 3600)

        if self.max_pages > max_pages:
            raise CrawlLimitError(f"max_pages exceeds limit: {self.max_pages} > {max_pages}")
        if self.max_depth > max_depth:
            raise CrawlLimitError(f"max_depth exceeds limit: {self.max_depth} > {max_depth}")
        if self.max_duration_seconds > max_duration:
            raise CrawlLimitError(
                f"max_duration_seconds exceeds limit: {self.max_duration_seconds} > {max_duration}"
            )


@dataclass
class CrawlSummary:
    """Summary of a crawl job."""

    fetched: int = 0
    skipped_robots: int = 0
    skipped_filter: int = 0
    failed: int = 0
    total_urls: int = 0


@dataclass
class CrawlState:
    """Persistent state for crawl resume."""

    frontier: list[str] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    results: list[dict] = field(default_factory=list)
    summary: CrawlSummary = field(default_factory=CrawlSummary)
    current_depth: int = 0
    start_time: float = field(default_factory=time.time)
    cancelled: bool = False

    def to_dict(self) -> dict:
        """Serialize state for persistence."""
        return {
            "frontier": list(self.frontier),
            "seen": list(self.seen),
            "results": self.results,
            "summary": {
                "fetched": self.summary.fetched,
                "skipped_robots": self.summary.skipped_robots,
                "skipped_filter": self.summary.skipped_filter,
                "failed": self.summary.failed,
                "total_urls": self.summary.total_urls,
            },
            "current_depth": self.current_depth,
            "start_time": self.start_time,
            "cancelled": self.cancelled,
        }

    @classmethod
    def from_dict(cls, data: dict) -> CrawlState:
        """Deserialize state from dict."""
        return cls(
            frontier=data.get("frontier", []),
            seen=set(data.get("seen", [])),
            results=data.get("results", []),
            summary=CrawlSummary(**data.get("summary", {})),
            current_depth=data.get("current_depth", 0),
            start_time=data.get("start_time", time.time()),
            cancelled=data.get("cancelled", False),
        )


class Crawler:
    """Crawler with robots.txt, domain policies, and limits."""

    def __init__(
        self,
        config: CrawlConfig,
        settings: Settings,
        fetcher: HttpFetcher,
        robots_cache: RobotsCache | None = None,
    ) -> None:
        self._config = config
        self._settings = settings
        self._fetcher = fetcher
        self._robots_cache = robots_cache or RobotsCache()

        # Per-domain rate limiters
        self._domain_limiters: dict[str, DomainRateLimiter] = {}

        # Crawl-specific rate limiter
        self._semaphore = asyncio.Semaphore(config.global_concurrency)
        self._domain_semaphores: dict[str, asyncio.Semaphore] = {}

        # State
        self._state: CrawlState | None = None
        self._start_url: str = ""
        self._base_domain: str = ""
        self._cancel_event = asyncio.Event()

    def set_cancel_event(self) -> None:
        """Signal that the crawl should be cancelled."""
        self._cancel_event.set()

    async def _get_domain_limiter(self, domain: str) -> DomainRateLimiter:
        """Get or create domain rate limiter."""
        if domain not in self._domain_limiters:
            policy = self._settings.domains.get(domain, {})
            self._domain_limiters[domain] = DomainRateLimiter.from_config(policy)
        return self._domain_limiters[domain]

    async def _get_domain_semaphore(self, domain: str) -> asyncio.Semaphore:
        """Get or create per-domain semaphore."""
        if domain not in self._domain_semaphores:
            per_domain = getattr(
                self._settings,
                "crawler_per_domain_concurrency",
                self._config.per_domain_concurrency,
            )
            self._domain_semaphores[domain] = asyncio.Semaphore(per_domain)
        return self._domain_semaphores[domain]

    async def _check_robots(self, url: str) -> RobotChecker:
        """Check robots.txt for URL, caching per domain."""
        from urllib.parse import urlparse

        parsed = urlparse(url)
        domain = parsed.hostname or ""

        rules = self._robots_cache.get(domain)
        if rules is None:
            # Fetch robots.txt
            robots_url = f"https://{domain}/robots.txt"
            try:
                async with self._semaphore:
                    result = await self._fetcher.fetch(robots_url)
                rules, global_sitemaps = parse_robots_txt(result.html)
                self._robots_cache.set(domain, rules)
            except Exception:
                # On robots.txt fetch failure, assume disallow all
                rules = RobotRules()
                self._robots_cache.set(domain, rules)

        return RobotChecker(rules)

    async def _crawl_one_url(
        self, url: str, depth: int, on_page: Callable[[dict], None] | None = None
    ) -> dict | None:
        """Crawl a single URL."""
        # Check cancellation
        if self._cancel_event.is_set():
            raise CrawlCancelledError("Crawl cancelled")

        # Check limits
        if depth > self._config.max_depth:
            return None

        # Validate URL
        valid, reason = validate_url(
            url,
            block_private_ips=self._settings.security_block_private_ips,
            block_localhost=self._settings.security_block_localhost,
            allowed_domains=self._settings.all_domain_names() or None,
        )
        if not valid:
            self._state.summary.skipped_filter += 1
            self._state.summary.total_urls += 1
            return {"url": url, "success": False, "error": reason}

        # Check robots
        if self._config.obey_robots_txt:
            try:
                checker = await self._check_robots(url)
                if not checker.is_allowed(url):
                    self._state.summary.skipped_robots += 1
                    self._state.summary.total_urls += 1
                    return {"url": url, "success": False, "error": "Disallowed by robots.txt"}
            except RobotsError as exc:
                self._state.summary.failed += 1
                self._state.summary.total_urls += 1
                return {"url": url, "success": False, "error": str(exc)}

        # Domain rate limiting
        domain = (urlparse(url)).hostname or ""
        domain_limiter = await self._get_domain_limiter(domain)
        domain_semaphore = await self._get_domain_semaphore(domain)

        await domain_semaphore.acquire()
        try:
            # Domain delay
            await domain_limiter.wait_if_needed(domain)
            domain_limiter.acquire(domain)

            try:
                # Fetch
                async with self._semaphore:
                    result = await self._fetcher.fetch(
                        url,
                        timeout_seconds=self._config.timeout_seconds,
                        domain=domain,
                        domain_limiter=domain_limiter,
                    )
                self._state.summary.fetched += 1
                self._state.summary.total_urls += 1

                page = {
                    "url": url,
                    "status_code": result.status_code,
                    "success": True,
                    "html": result.html,
                    "metadata": {"mode": "http", "elapsed_ms": result.elapsed_ms},
                }

                # Extract links
                links = self._extract_links(result.html, url)

                # Filter and enqueue
                new_links = self._filter_and_enqueue(links, depth)

                page["links_found"] = len(links)
                page["links_enqueued"] = new_links

                if on_page:
                    on_page(page)

                return page

            except Exception as exc:
                self._state.summary.failed += 1
                self._state.summary.total_urls += 1
                return {"url": url, "success": False, "error": str(exc)}

            finally:
                domain_limiter.release(domain)

        finally:
            domain_semaphore.release()

    def _extract_links(self, html: str, base_url: str) -> list[str]:
        """Extract links from HTML."""
        from app.scraper.link_extractor import extract_links, filter_links

        links = extract_links(html)
        return filter_links(
            links,
            base_url=base_url,
            allow=self._config.allow,
            deny=self._config.deny,
            same_domain_only=self._config.same_domain_only,
            include_subdomains=self._config.include_subdomains,
            allowed_extensions=self._config.allowed_extensions,
        )

    def _filter_and_enqueue(self, links: list[str], depth: int) -> int:
        """Filter links and enqueue unseen ones. Returns count of new links."""
        new_count = 0
        for link in links:
            if link not in self._state.seen:
                self._state.seen.add(link)
                if depth + 1 <= self._config.max_depth:
                    self._state.frontier.append(link)
                    new_count += 1
        return new_count

    async def run(self, on_page: Callable[[dict], None] | None = None) -> CrawlState:
        """Run the crawl.

        Args:
            on_page: Optional callback for each page result.

        Returns:
            Final crawl state.
        """
        if self._state is None:
            self._state = CrawlState()

        # Load start URLs into frontier if empty
        if not self._state.frontier:
            if self._config.start_urls:
                for url in self._config.start_urls:
                    if url not in self._state.seen:
                        self._state.seen.add(url)
                        self._state.frontier.append(url)
            elif self._config.sitemap_url:
                try:
                    result = fetch_sitemap(self._config.sitemap_url)
                    for url_info in result.urls:
                        if url_info.loc not in self._state.seen:
                            self._state.seen.add(url_info.loc)
                            self._state.frontier.append(url_info.loc)
                except SitemapError:
                    self._state.summary.failed += 1
                    raise

        # Set base domain
        if self._config.start_urls:
            from urllib.parse import urlparse

            parsed = urlparse(self._config.start_urls[0])
            self._base_domain = parsed.hostname or ""

        # Crawl loop
        while self._state.frontier and not self._cancel_event.is_set():
            # Check duration limit
            elapsed = time.time() - self._state.start_time
            if elapsed > self._config.max_duration_seconds:
                raise CrawlLimitError("Crawl duration exceeded")

            # Check page limit
            if self._state.summary.fetched >= self._config.max_pages:
                break

            url = self._state.frontier.pop(0)
            self._state.current_depth = self._count_depth(url)

            page = await self._crawl_one_url(url, self._state.current_depth, on_page)
            if page:
                self._state.results.append(page)

        return self._state

    def _count_depth(self, url: str) -> int:
        """Count depth based on URL path segments."""
        from urllib.parse import urlparse

        parsed = urlparse(url)
        path = parsed.path or "/"
        # Rough estimate: depth = number of path segments
        return len([seg for seg in path.split("/") if seg])


def create_crawler_from_state(config: CrawlConfig, state: CrawlState) -> Crawler:
    """Create a crawler instance from persisted state (for resume)."""
    # Create new crawler
    from app.scraper.http_fetcher import HttpFetcher
    from app.scraper.robots import RobotsCache

    fetcher = HttpFetcher()
    crawler = Crawler(config, Settings(), fetcher, RobotsCache())
    crawler._state = state
    crawler._cancel_event.clear()
    return crawler
