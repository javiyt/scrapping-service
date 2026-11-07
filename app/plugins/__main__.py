"""CLI interface for plugin management.

Usage:
    python -m app.plugins list    # list all discovered plugins with metadata
    python -m app.plugins check   # check plugin status and report failures
"""

import sys

from app.plugins.cli import run_plugins_command

if __name__ == "__main__":
    sys.exit(run_plugins_command(sys.argv[1:]))
