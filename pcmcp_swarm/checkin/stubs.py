"""NOT IMPLEMENTED channel adapters, kept as clearly marked placeholders.

Each one only moves bytes. When implemented, it MUST NOT verify or trust anything itself: the raw
bytes go back through checkin/handler.py -> signing.verify(), exactly like the local inbox.
"""
from __future__ import annotations

from . import CheckInChannel


class _Stub(CheckInChannel):
    def fetch_instructions(self):
        raise NotImplementedError(f"check-in channel '{self.name}' is a stub and NOT IMPLEMENTED")

    def post_receipts(self, receipt, attachments=None):
        raise NotImplementedError(f"check-in channel '{self.name}' is a stub and NOT IMPLEMENTED")


class HttpsEndpoint(_Stub):
    """NOT IMPLEMENTED. PCMCP calls an authenticated HTTPS endpoint (GET batches, POST receipts).
    Planned config: checkin.https_endpoint = {"url": ..., "auth": {secret spec}}. TLS verification on."""
    name = "https_endpoint"


class McpTool(_Stub):
    """NOT IMPLEMENTED. An MCP tool (e.g. in pcmcp-mcp-server.js) an AI calls to drop a signed batch into
    the inbox and read receipts. It would write files exactly like an inbox drop; signature still required."""
    name = "mcp_tool"


class EmailAttachment(_Stub):
    """NOT IMPLEMENTED. Signed batch arrives as an email attachment. The email itself, its sender and its
    body confer nothing; only the attachment bytes are passed to signing.verify()."""
    name = "email_attachment"
