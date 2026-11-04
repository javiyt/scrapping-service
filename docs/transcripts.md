# YouTube Transcripts

The transcript API retrieves YouTube video captions (transcripts) through a
provider-agnostic stack.  One endpoint, a normalized response, and an
ordered fallback chain of backends.

## The Endpoint

```
GET /v1/transcript?url=<YouTube URL or video ID>&lang=es,en
```

| Parameter | Description |
| --- | --- |
| `url` | Any YouTube URL form (watch, shorts, youtu.be, embed, live) or a bare 11-character video ID. Required (with `video_id`). |
| `video_id` | A bare 11-character video ID (alternative to `url`). |
| `lang` | Preferred caption languages, comma-separated in priority order (e.g. `es,en`). Default `en`. |
| `provider` | An explicit provider name, or `auto` (default) for the ordered fallback chain. |
| `cache_ttl_seconds` | Per-request cache TTL override (60 s – 30 d). |
| `force_refresh` | `true` bypasses the result cache. |

The response is the service-owned normalized transcript: video ID, title,
language, `is_generated` (auto-generated captions flag), the provider that
produced it, timed caption segments, the full text, word count, and
metadata (providers attempted, whether a fallback occurred, cache state,
warnings).  A raw provider payload never appears.

```json
{
  "video_id": "dQw4w9WgXcQ",
  "title": "...",
  "language": "en",
  "is_generated": false,
  "provider": "camofox",
  "segments": [{ "start": 0.0, "duration": 1.0, "text": "hello" }],
  "text": "hello world",
  "total_words": 2,
  "metadata": { "providers_attempted": ["camofox"], "fallback_occurred": false }
}
```

## Providers and the Fallback Chain

Transcript providers are configured in the top-level `transcript:` block
(see [Configuration](configuration.md#transcript)).  In `auto` mode the
router walks `provider_order` (configured, restricted to registered
providers, then registration order):

1. Each candidate is health-probed (bounded by `probe_timeout_seconds`);
   unhealthy candidates are skipped.
2. The first healthy provider serves the request.
3. A *retryable* provider failure (503/504, IP block 403, unusable payload
   502) falls back to the next healthy candidate.
4. If every eligible provider fails retryably, `503
   no_transcript_provider_available` is returned with the attempted list.

An *explicit* `provider=` never falls back: its failures propagate
unchanged.

The current default chain (and the providers in this service):

| Order | Provider | Backend | Health signal |
| --- | --- | --- | --- |
| 1 | `camofox` | External Camofox service, `POST /youtube/transcript` (server-side yt-dlp or browser) | Cheap Camofox `/health` probe — no YouTube hit |
| 2 | `youtube-transcript-api` | `youtube-transcript-api` Python library (when added, #146) | In-process library import check — no YouTube hit |
| 3 | `yt-dlp` | `yt-dlp` (when added, #147) | In-process `yt-dlp` import check — no YouTube hit |

The health probes deliberately do **not** hit YouTube: they check service
reachability / library importability only, so health checks and routing stay
cheap and side-effect-free.

## Error Semantics

Errors are classified so the router can decide whether to fall back:

| HTTP | Error type | Meaning | Fallback? |
| --- | --- | --- | --- |
| 400 | `validation_error` | Missing/bad URL, non-YouTube video, bad languages, unknown provider | No — the request is wrong |
| 403 | `transcript_provider_blocked` | Upstream IP/geo block by the provider | Yes (auto) |
| 404 | `no_transcript_available` | The video has no captions | **No — definitive video fact** |
| 404 | `transcript_language_unavailable` | No captions in a requested language | **No — definitive** |
| 404 | `video_unavailable` | Private/deleted/unlisted video | **No — definitive** |
| 502 | `transcript_provider_invalid_response` | Provider returned an unusable payload | Yes (auto) |
| 503 | `transcript_provider_unavailable` | Provider unreachable/down | Yes (auto) |
| 503 | `no_transcript_provider_available` | Every provider failed, or none configured | Terminal |
| 504 | `transcript_provider_timeout` | Provider timed out | Yes (auto) |
| 503 | `service_unavailable` | The transcript subsystem is not initialised | — |

Definitive errors describe the *video*, not the provider: every other
provider would hit the same wall, so the chain stops instead of burning
time and connections.  When the result cache is enabled with
`stale_if_error`, an expired cached entry may be served (flagged
`metadata.stale`) instead of a *retryable* provider failure.

## Limitations

Transcripts come from YouTube itself; the service cannot create captions
that do not exist.

- **No captions** — some videos have no captions at all; those return `404
  no_transcript_available`. (Whisper-based transcription for such videos is
  future work, not part of this stack.)
- **Language** — a video may only offer captions in some languages; an
  unserved request returns `404 transcript_language_unavailable` with the
  available languages in `details`.
- **Age-restricted / login-restricted videos** — captions behind a login
  are not retrievable; expect a definitive unavailability error.
- **IP / geo blocking** — providers are rate-limited or blocked by IP
  (datacenter IPs are commonly hit).  A blocked provider falls back in auto
  mode; if *all* providers are blocked the request fails with
  `no_transcript_provider_available`.  Self-hosting the Camofox service
  behind a residential/rotating egress is the usual mitigation.
- **Slow upstream** — transcript generation can take well over a minute on
  long videos; the provider's `timeout_seconds` bounds each attempt.
- **Generated captions** — auto-generated captions carry recognition
  errors; `is_generated: true` marks them, so clients can decide.

## Observability

- **Metrics** (`GET /metrics`) — `transcript_requests_total`,
  `transcript_success_total`, `transcript_error_total`,
  `transcript_fallbacks_total`, the cache counters
  (`transcript_cache_hits_total` / `_misses_total` / `_stale_hits_total`
  / `_errors_total`), `transcript_duration_ms_sum` (latency), and the
  per-provider `transcript_provider_attempts_total` /
  `transcript_provider_success_total` / `transcript_provider_error_total`
  (labelled only by the bounded, configured provider name — user input
  never appears in labels).
- **Deep health** (`GET /health/deep`) — `checks.transcript` reports the
  result-cache policy, the enabled providers in routing order, and
  per-provider availability via the (YouTube-free) health probes, plus
  provider diagnostics.  Down providers are *reported, never fatal*: the
  container restart policy stays decoupled from transcript availability.

## Tests

- `tests/test_transcript_api_contract.py` — the full chain through the
  real app with fake providers (schema-leak guard, fallback chain, cache,
  validation).
- `tests/test_transcript_observability.py` — metrics, label hygiene, and
  health contracts.
- `tests/test_transcript_live.py` — opt-in real-YouTube smoke test;
  skipped by default, enable with
  `SCRAPER_TEST_TRANSCRIPT_LIVE=true`
  (`SCRAPER_TEST_TRANSCRIPT_VIDEO` overrides the target video).
