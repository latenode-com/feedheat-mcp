# MCP-сервер администратора FeedHeat Crowd.
# Публичная поверхность пакета: клиент API + собранный MCP-сервер.
from feedheat_mcp.client import AdminClient, ApiError, ConfigError

__version__ = '0.1.0'

__all__ = ['AdminClient', 'ApiError', 'ConfigError', '__version__']
