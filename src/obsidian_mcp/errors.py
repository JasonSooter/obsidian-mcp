"""Errors meant to be read by the model calling the tool.

Messages should say what went wrong *and* what to do about it, since the caller
is an LLM that will try to recover on its own.
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError as _SDKToolError


class ToolError(_SDKToolError):
    """An anticipated failure, reported to the client verbatim.

    Subclassing the SDK's ToolError is load-bearing, not decoration: the SDK
    treats that type as an *anticipated* failure and passes the message through
    to the client, while any other exception is treated as a crash and reported
    as a bare "Error executing tool <name>". Raising a plain Exception here
    would silently throw away every message in this package.
    """


class PathRejected(ToolError):
    """A path escaped the vault, or is of a type we refuse to touch.

    Deliberately vague about *why* beyond the category. This server is on the
    public internet; a probing caller should not be able to use error text to
    map the filesystem outside the vault.
    """


class VaultUnknown(ToolError):
    """A tool was called with a vault name this process was not configured with."""


class StaleWrite(ToolError):
    """The file changed between the read and the write.

    Raised only when the caller supplied expected_mtime_ns. The message tells
    the model to re-read, which is the only correct recovery -- Dropbox may have
    delivered an edit from another device mid-call.
    """
