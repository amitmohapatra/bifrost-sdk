"""Namespaces for the gateway's management API (``/api/*``)."""

from bifrost_sdk.resources.governance import Governance, VirtualKeys
from bifrost_sdk.resources.mcp import MCP
from bifrost_sdk.resources.prompts import Prompts
from bifrost_sdk.resources.routing import Routing
from bifrost_sdk.resources.skills import Skills

__all__ = ["MCP", "Governance", "Prompts", "Routing", "Skills", "VirtualKeys"]
