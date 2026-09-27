"""Gotify 通知适配。"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests

from domain_update.config import AppConfig
from domain_update.models import Result


logger = logging.getLogger(__name__)

# 异常文本里可能带着完整请求 URL，而 URL 的查询参数足以装下一个凭据。
# Token 现已改走请求头，这里是防回归的兜底：真有人把它挪回查询参数，
# 日志里也只会留下 token=***。
_TOKEN_IN_TEXT = re.compile(r"(token=)[^&\s]*", re.IGNORECASE)


def _redact(text: str) -> str:
    return _TOKEN_IN_TEXT.sub(r"\1***", text)


def send_gotify(config: AppConfig, title: str, message: str) -> Result[bool]:
    if not config.gotify_address.strip() or not config.gotify_token.strip():
        return Result.success("未配置 Gotify，已跳过通知", False)
    address = config.gotify_address.strip().rstrip("/")
    if not urlparse(address).scheme:
        address = f"https://{address}"
    try:
        # Token 放请求头而不是查询参数：URL 会进入 requests 的异常文本、
        # 反向代理日志和访问日志，放查询参数等于把凭据散播到这几处。
        # strip 后再传：粘贴来的 Token 常带尾随空白，含非法字符会让 requests 直接抛错。
        response = requests.post(
            f"{address}/message",
            headers={"X-Gotify-Key": config.gotify_token.strip()},
            data={"title": title, "message": message, "priority": 0},
            timeout=(5, 10),
        )
        response.raise_for_status()
        return Result.success("Gotify 通知发送成功", True)
    except requests.RequestException as error:
        # 原因仍然记录（排查连通性与 401 都靠它），但先脱敏。
        logger.warning(
            "Gotify 通知发送失败：类型=%s 状态码=%s 原因=%s",
            type(error).__name__,
            getattr(getattr(error, "response", None), "status_code", None),
            _redact(str(error)),
        )
        return Result.failure("DNS 已更新，但 Gotify 通知发送失败")

