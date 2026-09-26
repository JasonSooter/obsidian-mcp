"""Auth scope definition.

Authentication is OAuth 2.1 end to end -- see the ``oauth`` package. It is the
only scheme, because it reaches every client: Claude Code, Claude Desktop,
claude.ai and mobile.
"""

from __future__ import annotations

# Scope required of every caller. The OAuth provider issues and validates it;
# the MCP SDK enforces it before a request reaches any tool.
OBSIDIAN_SCOPE = "obsidian"
