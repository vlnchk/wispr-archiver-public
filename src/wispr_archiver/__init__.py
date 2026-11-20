"""wispr-archiver: append-only archive of Wispr Flow Notetaker meetings."""

__version__ = "0.1.0"

#: Official read-only MCP endpoint. All ingest commands use this address.
WISPR_MCP_URL = "https://api.wisprflow.ai/connect/mcp"

ARCHIVE_SCHEMA = "wispr-archive/v1"
ARCHIVE_SOURCE = "wispr-remote-mcp"
