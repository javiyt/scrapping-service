"""CLI interface for plugin management.

Usage:
    python -m app.plugins list    # list all discovered plugins with metadata
    python -m app.plugins check   # check plugin status and report failures
"""

import sys

from app.core.config import Settings
from app.plugins.loader import load_plugins

_settings: Settings | None = None


def _get_settings() -> Settings:
    """Return the application settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


def _print_extensions(extensions: dict | None) -> None:
    """Print registered provider extensions of a plugin entry."""
    if not extensions:
        return
    for kind, providers in extensions.items():
        if isinstance(providers, str) and providers:
            print(f"  - {kind}: {providers}")
        elif providers:
            print(f"  - {kind}: {', '.join(providers)}")


def cmd_list(settings: Settings) -> int:
    """List all discovered plugins with metadata."""
    try:
        result = load_plugins(settings)
    except Exception as exc:
        print(f"Error loading plugins: {exc}", file=sys.stderr)
        return 1

    if result.metadata:
        for entry in result.metadata:
            name = entry.get("name", "unknown")
            version = entry.get("version", "unknown")
            status = entry.get("status", "unknown")
            api_version = entry.get("api_version", "unknown")
            print(f"{name} v{version} [{status}] (api={api_version})")
            if entry.get("reason"):
                print(f"  reason: {entry['reason']}")
            _print_extensions(entry.get("extensions"))
    else:
        print("No plugins discovered")
    return 0


def cmd_check(settings: Settings) -> int:
    """Check plugin status and report failures."""
    try:
        result = load_plugins(settings)
    except Exception as exc:
        print(f"Error checking plugins: {exc}", file=sys.stderr)
        return 1

    failures = [
        (entry.get("name", "unknown"), entry.get("reason") or "unknown")
        for entry in result.metadata
        if entry.get("status") == "failed"
    ]
    disabled = [
        (entry.get("name", "unknown"), entry.get("reason") or "unknown")
        for entry in result.metadata
        if entry.get("status") == "disabled"
    ]

    if failures:
        print("FAILED plugins:")
        for name, reason in failures:
            print(f"  - {name}: {reason}")
        print()

    if disabled:
        print("DISABLED plugins:")
        for name, reason in disabled:
            print(f"  - {name}: {reason}")
        print()

    if failures or disabled:
        print(f"Summary: {len(failures)} failed, {len(disabled)} disabled")
        return 1
    print("All plugins loaded successfully")
    return 0


def main(args: list[str] | None = None) -> int:
    """CLI entry point."""
    if args is None:
        args = sys.argv[1:]

    if not args:
        print("Usage: python -m app.plugins <list|check>")
        print()
        print("Commands:")
        print("  list   List all discovered plugins with metadata")
        print("  check  Check plugin status and report failures")
        return 1

    settings = _get_settings()

    if args[0] == "list":
        return cmd_list(settings)
    if args[0] == "check":
        return cmd_check(settings)
    print(f"Unknown command: {args[0]}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
