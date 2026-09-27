"""Web 控制台：自带 HTTP 服务与 JSON API，替代原 Streamlit 前端。"""

from domain_update.web.api import ApiResponse, WebApi
from domain_update.web.server import serve

__all__ = ["ApiResponse", "WebApi", "serve"]
