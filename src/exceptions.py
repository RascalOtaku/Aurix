# src/exceptions.py
"""Custom exceptions for the application."""

class SessionNotFoundError(Exception):
    """Raised when a requested session is not found."""
    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"Session '{session_id}' not found")

class InvalidFileUploadError(Exception):
    """Raised when a file upload fails validation."""
    def __init__(self, message: str, filename: str = None):
        self.filename = filename
        self.message = message
        super().__init__(message)

class LLMServiceError(Exception):
    """Raised when there is an error communicating with the LLM service."""
    def __init__(self, message: str, endpoint: str = None):
        self.endpoint = endpoint
        self.message = message
        super().__init__(message)

class WebSearchError(Exception):
    """Raised when there is an error with web search functionality."""
    def __init__(self, message: str, query: str = None):
        self.query = query
        self.message = message
        super().__init__(message)

class McpToolDisabledError(Exception):
    """Raised when code tries to execute an MCP tool the user has disabled.

    Disabling a tool hides it from listings AND blocks execution — a disabled
    tool must never reach the underlying MCP session.
    """
    def __init__(self, server_id: str, tool_name: str):
        self.server_id = server_id
        self.tool_name = tool_name
        super().__init__(f"MCP tool '{tool_name}' on server '{server_id}' is disabled")
