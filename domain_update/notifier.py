"""Gotify 通知适配。"""

from urllib.parse import urlparse

import requests

from domain_update.config import AppConfig
from domain_update.models import Result


def send_gotify(config: AppConfig, title: str, message: str) -> Result[bool]:
    if not config.gotify_address.strip() or not config.gotify_token.strip():
        return Result.success("未配置 Gotify，已跳过通知", False)
    address = config.gotify_address.strip().rstrip("/")
    if not urlparse(address).scheme:
        address = f"https://{address}"
    try:
        # Token 放在参数中但任何异常均不原样回显，避免 URL 泄露凭据。
        response = requests.post(
            f"{address}/message",
            params={"token": config.gotify_token},
            data={"title": title, "message": message, "priority": 0},
            timeout=(5, 10),
        )
        response.raise_for_status()
        return Result.success("Gotify 通知发送成功", True)
    except requests.RequestException:
        return Result.failure("DNS 已更新，但 Gotify 通知发送失败")

