"""abrasio-mcp — MCP server that exposes Abrasio as an agentic browser."""

from .browser import AbrasioBrowserAgent
from .server import mcp

__version__ = "0.1.0"
__all__ = ["AbrasioBrowserAgent", "mcp"]
