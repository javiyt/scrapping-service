"""Plugin CLI dispatcher and implementations.

Provides subcommands for plugin management:
- install: pip install plugins into current environment
- uninstall: pip uninstall plugins
- list: list discovered plugins with metadata
- check: check plugin status and report failures
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

from app.plugins.install import (
    build_pip_command,
    default_constraints_file,
)
from app.plugins.loader import load_plugins  # Import at module level for tests to patch


def _run_pip(cmd: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    """Run pip subprocess with sys.executable."""
    full_cmd = [sys.executable, "-m", "pip"] + cmd
    return subprocess.run(
        full_cmd,
        capture_output=capture_output,
        text=True,
        check=False,
    )


def cmd_install(specs: list[str], constraints_file: str | None, **kwargs: Any) -> int:
    """Install plugins via pip.

    Args:
        specs: pip requirement specifiers
        constraints_file: path to constraints file (None for default)
        kwargs: index options passed through to build_pip_command

    Returns:
        0 on success, non-zero on failure
    """
    # Resolve constraints file
    if constraints_file is None:
        constraints_file = default_constraints_file()
        if constraints_file is None:
            print(
                "WARNING: no constraints file found; plugins may override core dependencies. "
                "Consider setting SCRAPER_PLUGINS_CONSTRAINTS or --constraints.",
                file=sys.stderr,
            )

    cmd = build_pip_command(
        target_dir=None,  # no --target: install into current environment
        constraints_file=constraints_file or "",
        specs=specs,
        requirements_file=None,
        index_url=kwargs.get("index_url"),
        extra_index_url=kwargs.get("extra_index_url"),
        offline=kwargs.get("offline", False),
        wheelhouse_dir=kwargs.get("wheelhouse_dir") or "",
    )

    print(f"Installing plugins: {' '.join(specs)}", file=sys.stderr)
    if constraints_file:
        print(f"Using constraints: {constraints_file}", file=sys.stderr)

    result = _run_pip(cmd[3:], capture_output=False)  # cmd already has [python, -m, pip]
    if result.returncode != 0:
        print(f"ERROR: pip install failed with exit code {result.returncode}", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return 1
    if result.stdout:
        print(result.stdout, file=sys.stderr)
    return 0


def cmd_uninstall(names: list[str], *, assume_yes: bool = False) -> int:
    """Uninstall plugins via pip.

    Args:
        names: package names to uninstall
        assume_yes: skip confirmation prompt

    Returns:
        0 on success, non-zero on failure
    """
    cmd = (
        [sys.executable, "-m", "pip", "uninstall", "-y"]
        if assume_yes
        else [sys.executable, "-m", "pip", "uninstall"]
    )
    cmd.extend(names)

    print(f"Uninstalling plugins: {' '.join(names)}", file=sys.stderr)

    # Show what will be removed before proceeding
    if not assume_yes:
        confirm_result = _run_pip(["uninstall", "--dry-run"] + names, capture_output=True)
        if confirm_result.stdout:
            print("Packages to be uninstalled:", file=sys.stderr)
            print(confirm_result.stdout, file=sys.stderr)

        print("Proceed with uninstall? [y/N] ", end="", file=sys.stderr)
        sys.stderr.flush()
        response = input().strip().lower()
        if response not in ("y", "yes"):
            print("Uninstall cancelled.", file=sys.stderr)
            return 0

    result = _run_pip(cmd[3:], capture_output=False)
    if result.returncode != 0:
        print(f"ERROR: pip uninstall failed with exit code {result.returncode}", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return 1
    if result.stdout:
        print(result.stdout, file=sys.stderr)
    return 0


def cmd_list() -> int:
    """List all discovered plugins with metadata."""
    from app.core.config import Settings

    settings = Settings.load()
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
            extensions = entry.get("extensions", {})
            if isinstance(extensions, dict):
                if extensions.get("search_providers"):
                    print(f"  search_providers: {', '.join(extensions['search_providers'])}")
                if extensions.get("transcript_providers"):
                    print(
                        f"  transcript_providers: {', '.join(extensions['transcript_providers'])}"
                    )
                if extensions.get("browser_provider"):
                    print(f"  browser_provider: {extensions['browser_provider']}")
                if extensions.get("extractors"):
                    print(f"  extractors: {', '.join(extensions['extractors'])}")
    else:
        print("No plugins discovered")
    return 0


def cmd_check() -> int:
    """Check plugin status and report failures."""
    from importlib import metadata as importlib_metadata

    from app.core.config import Settings

    settings = Settings.load()
    try:
        result = load_plugins(settings)
    except Exception as exc:
        print(f"Error checking plugins: {exc}", file=sys.stderr)
        return 1

    # Get installed distributions for each plugin
    diagnostics: list[str] = []
    failures: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []

    # Get service package location
    try:
        dist = importlib_metadata.distribution("scraper-api")
        service_location = dist.locate_file("")
    except importlib_metadata.PackageNotFoundError:
        service_location = None

    for entry in result.metadata:
        name = entry.get("name", "unknown")
        status = entry.get("status", "unknown")
        reason = entry.get("reason")

        # Check if plugin is installed in a different environment
        if entry.get("extensions"):
            try:
                # Try to find the distribution from the entry point
                # The entry point's module is in the plugin package
                entry_point = next(
                    (
                        ep
                        for ep in importlib_metadata.entry_points(group="scrapping_service.plugins")
                        if ep.name == name
                    ),
                    None,
                )
                if entry_point:
                    try:
                        mod = entry_point.load()
                        # Get the module file location
                        mod_file = getattr(mod, "__file__", None)
                        if mod_file:
                            plugin_location = os.path.dirname(os.path.abspath(mod_file))
                            if service_location:
                                # Check if plugin location is outside service location
                                rel_path = os.path.relpath(plugin_location, service_location)
                                if rel_path.startswith(".."):
                                    msg = (
                                        f"WARNING: plugin '{name}' is installed in a different "
                                        f"environment than the service ({plugin_location} vs "
                                        f"{service_location}) — it may NOT be discoverable."
                                    )
                                    diagnostics.append(msg)
                    except Exception:
                        pass
            except Exception:
                pass

        if status == "failed":
            failures.append((name, reason or "unknown"))
        elif status == "disabled":
            skipped.append((name, reason or "unknown"))

    if failures:
        print("FAILED plugins:", file=sys.stderr)
        for name, reason in failures:
            print(f"  - {name}: {reason}", file=sys.stderr)

    if skipped:
        print("SKIPPED plugins:", file=sys.stderr)
        for name, reason in skipped:
            print(f"  - {name}: {reason}", file=sys.stderr)
            # Add enable hint
            print(
                f"    Note: Plugin installed but not enabled. Enable via "
                f"SCRAPER_PLUGINS_ENABLED={name} or plugin.allowlist: [{name}]",
                file=sys.stderr,
            )

    for diag in diagnostics:
        print(diag, file=sys.stderr)

    if failures or skipped or diagnostics:
        return 1
    print("All plugins loaded successfully")
    return 0


def cmd_help() -> int:
    """Print help message."""
    print(
        """scrapping-service plugin management commands

Usage:
  scrapping-service plugins <command> [options]

Commands:
  install <spec>...  Install plugins from PyPI, VCS URLs, or local wheels
  uninstall <name>... Uninstall plugins by name
  list              List all discovered plugins with metadata
  check             Check plugin status and report failures
  help              Show this help message

Options for 'install':
  -c, --constraints FILE  Path to constraints file (default: try to find requirements.txt)
  --index-url URL         Base URL of the Python Package Index
  --extra-index-url URL   Extra URL of package index
  --offline               Install from wheelhouse only (no index access)
  --wheelhouse DIR        Path to wheelhouse directory for offline mode

Examples:
  scrapping-service plugins install my-plugin==1.0.0
  scrapping-service plugins install git+https://github.com/user/repo@v1.0
  scrapping-service plugins list
  scrapping-service plugins check
  scrapping-service plugins uninstall my-plugin
"""
    )
    return 0


def run_plugins_command(argv: list[str]) -> int:
    """Dispatch CLI command to appropriate handler.

    Args:
        argv: command-line arguments (excluding program name)

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    if not argv:
        return cmd_help()

    command = argv[0].lower()
    args = argv[1:]

    if command == "install":
        # Parse positional specs and options
        specs: list[str] = []
        constraints_file: str | None = None
        index_url: str | None = None
        extra_index_url: str | None = None
        offline = False
        wheelhouse_dir: str | None = None

        i = 0
        while i < len(args):
            arg = args[i]
            if arg in ("-c", "--constraints"):
                if i + 1 >= len(args):
                    print("ERROR: --constraints requires a file path", file=sys.stderr)
                    return 1
                constraints_file = args[i + 1]
                i += 2
            elif arg == "--index-url":
                if i + 1 >= len(args):
                    print("ERROR: --index-url requires a URL", file=sys.stderr)
                    return 1
                index_url = args[i + 1]
                i += 2
            elif arg == "--extra-index-url":
                if i + 1 >= len(args):
                    print("ERROR: --extra-index-url requires a URL", file=sys.stderr)
                    return 1
                extra_index_url = args[i + 1]
                i += 2
            elif arg == "--offline":
                offline = True
                i += 1
            elif arg == "--wheelhouse":
                if i + 1 >= len(args):
                    print("ERROR: --wheelhouse requires a directory", file=sys.stderr)
                    return 1
                wheelhouse_dir = args[i + 1]
                i += 2
            elif arg.startswith("-"):
                print(f"ERROR: Unknown option: {arg}", file=sys.stderr)
                return 1
            else:
                # Parse spec (could be package spec or path)
                specs.append(arg)
                i += 1

        if not specs:
            print("ERROR: install requires at least one package spec", file=sys.stderr)
            return 1

        return cmd_install(
            specs,
            constraints_file,
            index_url=index_url,
            extra_index_url=extra_index_url,
            offline=offline,
            wheelhouse_dir=wheelhouse_dir,
        )

    elif command == "uninstall":
        if not args:
            print("ERROR: uninstall requires at least one package name", file=sys.stderr)
            return 1
        assume_yes = "--yes" in args
        names = [a for a in args if a != "--yes"]
        return cmd_uninstall(names, assume_yes=assume_yes)

    elif command == "list":
        return cmd_list()

    elif command == "check":
        return cmd_check()

    elif command in ("help", "--help", "-h"):
        return cmd_help()

    else:
        print(f"ERROR: Unknown command: {command}", file=sys.stderr)
        print("Run 'scrapping-service plugins help' for usage information.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run_plugins_command(sys.argv[1:]))
