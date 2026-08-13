"""Agent-facing interface for the Kaggle Team control plane."""

from .http_client import ApiClient, ApiError
from .tools import ToolRegistry

__all__ = ["ApiClient", "ApiError", "ToolRegistry"]
__version__ = "0.1.0"
