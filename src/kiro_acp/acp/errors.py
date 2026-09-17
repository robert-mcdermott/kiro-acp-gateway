"""Exception hierarchy for the ACP client."""

from __future__ import annotations

from typing import Any


class ACPError(Exception):
    """Base class for all ACP client errors."""


class ACPProcessError(ACPError):
    """The agent subprocess could not be started, exited unexpectedly, or closed its pipes."""

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class ACPRemoteError(ACPError):
    """The agent answered a request with a JSON-RPC error object."""

    def __init__(
        self, code: int, message: str, data: Any = None, *, method: str | None = None
    ) -> None:
        detail = message
        if data not in (None, ""):
            detail = (
                f"{message}: {data}"
                if not isinstance(data, dict | list)
                else f"{message}: {data!r}"
            )
        detail = f"{method} failed ({code}): {detail}" if method else f"({code}) {detail}"
        super().__init__(detail)
        self.code = code
        self.message = message
        self.data = data
        self.method = method

    @property
    def is_method_not_found(self) -> bool:
        return self.code == JSONRPC_METHOD_NOT_FOUND


class ACPTimeoutError(ACPError):
    """A request did not receive a response within its timeout."""


class ACPProtocolError(ACPError):
    """The agent sent a message that does not conform to ACP / JSON-RPC 2.0."""


# JSON-RPC 2.0 and ACP error codes.
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
JSONRPC_REQUEST_CANCELLED = -32800
ACP_AUTH_REQUIRED = -32000
ACP_RESOURCE_NOT_FOUND = -32002
