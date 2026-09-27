"""公网 IPv6 探测。"""

from ipaddress import AddressValueError, IPv6Address

import requests

from domain_update.models import Result


IPIFY_IPV6_URL = "https://api6.ipify.org"
REQUEST_TIMEOUT = (5, 10)


def get_public_ipv6() -> Result[str]:
    try:
        with requests.Session() as session:
            # 等价于 curl --noproxy "*"，防止代理出口地址被误写入 DNS。
            session.trust_env = False
            response = session.get(IPIFY_IPV6_URL, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        address = IPv6Address(response.text.strip())
        if not address.is_global:
            return Result.failure("检测到的 IPv6 不是公网地址")
        return Result.success("公网 IPv6 获取成功", address.compressed)
    except (requests.RequestException, AddressValueError):
        # 网络异常可能含代理或请求细节，因此对 UI 返回稳定且不泄密的消息。
        return Result.failure("公网 IPv6 获取失败，请检查容器的 IPv6 连通性")

