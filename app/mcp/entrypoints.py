"""Console entry points for the scrapping-service MCP adapter (ticket #154).

Installed as:

    pip install -e ".[mcp]"

giving ``scrapping-service-mcp`` (stdio) and ``scrapping-service-mcp-http``
(streamable HTTP on 8090).  The stdio script depends only on the stdlib +
the ``mcp`` SDK; the http script additionally needs ``uvicorn`` (already a
service dependency).
"""

from app.mcp.server import main, main_http

__all__ = ["main", "main_http"]

if __name__ == "__main__":
    main()
