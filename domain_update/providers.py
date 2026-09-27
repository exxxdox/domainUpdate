"""DNS 服务商适配层。"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import requests
from alibabacloud_alidns20150109 import models as alidns_models
from alibabacloud_alidns20150109.client import Client as AlidnsClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models

from domain_update.config import AppConfig
from domain_update.models import DnsStatus, Result, UpdateAction


# Cloudflare 官方 SDK 为覆盖全部产品线自带约 62MB 生成代码，本项目只用到三个 DNS 调用，
# 因此直接调用同一套 REST API：行为等价，而镜像体积显著下降。
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
# 连接与读取超时分开设置；DNS 写入失败要尽快反馈到页面，不能长时间挂住界面。
CLOUDFLARE_TIMEOUT = (5, 15)
RECORD_TYPE = "AAAA"

logger = logging.getLogger(__name__)


def _http_status(error: Exception) -> int | None:
    """取 HTTP 状态码用于日志。只记状态码，不记 URL 与响应体。"""
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None)


def _optional_bool(value: Any) -> bool | None:
    """只接受真正的布尔值，避免把字符串 "false" 当成 True 回写进记录。"""
    return value if isinstance(value, bool) else None


def _optional_int(value: Any) -> int | None:
    """bool 是 int 的子类，必须排除，否则 True 会被当作 TTL 1 回写。"""
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


class DnsProvider(Protocol):
    name: str

    def get_status(self) -> Result[DnsStatus | None]: ...

    def set_ipv6(self, ipv6: str, current: DnsStatus | None) -> Result[UpdateAction]: ...


class CloudflareProvider:
    name = "cloudflare"

    def __init__(
        self, config: AppConfig, session: requests.Session | None = None
    ) -> None:
        self._config = config
        self._session = session if session is not None else requests.Session()
        # 凭据挂在实例级 Session 的默认头上，避免不同实例之间串用请求头。
        self._session.headers.update(
            {
                "Authorization": f"Bearer {config.cloudfare_token}",
                "Content-Type": "application/json",
            }
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """发请求并校验 Cloudflare 的业务成功标记，把两类失败归一成异常。

        调用方只需处理异常，不必各自判断 HTTP 状态码和响应体里的 success 字段。
        """
        response = self._session.request(
            method, f"{CLOUDFLARE_API_BASE}{path}", timeout=CLOUDFLARE_TIMEOUT, **kwargs
        )
        response.raise_for_status()
        payload = response.json()
        # Cloudflare 会在 HTTP 200 的同时返回 success=false，只看状态码会漏判。
        if not isinstance(payload, dict) or not payload.get("success"):
            raise ValueError("Cloudflare 返回 success=false")
        return payload.get("result")

    def get_status(self) -> Result[DnsStatus | None]:
        try:
            records = self._request(
                "GET",
                f"/zones/{self._config.cloudfare_zone_id}/dns_records",
                params={
                    "type": RECORD_TYPE,
                    "name": self._config.cloudfare_record_name,
                },
            )
        except (requests.RequestException, ValueError) as error:
            # 响应体可能包含账号与请求细节，界面只回固定文案；
            # 日志同样只记异常类型与状态码，不记 URL（含 Zone ID）与响应体。
            logger.warning(
                "Cloudflare 记录查询失败：类型=%s 状态码=%s 原因=%s",
                type(error).__name__,
                _http_status(error),
                error,
            )
            return Result.failure("Cloudflare DNS 查询失败，请检查凭据与 Zone ID")

        for record in records or []:
            if not isinstance(record, dict):
                continue
            # 服务端过滤不能当唯一防线：只接受名称与类型都与配置一致的记录。
            if (
                record.get("name") != self._config.cloudfare_record_name
                or record.get("type") != RECORD_TYPE
            ):
                continue
            return Result.success(
                "Cloudflare DNS 记录读取成功",
                DnsStatus(
                    provider="cloudflare",
                    record_name=str(record.get("name", "")),
                    record_type=RECORD_TYPE,
                    value=str(record.get("content", "")),
                    record_id=str(record.get("id", "")),
                    proxied=_optional_bool(record.get("proxied")),
                    ttl=_optional_int(record.get("ttl")),
                ),
            )
        # “记录不存在”不是请求失败，更新流程可据此安全地创建记录。
        return Result.success("Cloudflare 中未找到对应的 AAAA 记录", None)

    def set_ipv6(self, ipv6: str, current: DnsStatus | None) -> Result[UpdateAction]:
        zone_path = f"/zones/{self._config.cloudfare_zone_id}/dns_records"
        payload: dict[str, Any] = {
            "content": ipv6,
            # 必须使用配置中的完整记录名，避免旧实现硬编码 server 后改错记录。
            "name": self._config.cloudfare_record_name,
            "type": RECORD_TYPE,
        }
        try:
            if current is None:
                self._request(
                    "POST",
                    zone_path,
                    json={**payload, "proxied": False, "ttl": 60},
                )
                return Result.success("Cloudflare AAAA 记录创建成功", "created")
            if current.value == ipv6:
                # 值未变化就不发写请求，避免无意义的 API 调用与审计噪音。
                return Result.success("Cloudflare AAAA 记录无需更新", "unchanged")
            # 保留原记录的代理与 TTL，IPv6 变化不应改变暴露方式和缓存策略。
            if current.proxied is not None:
                payload["proxied"] = current.proxied
            if current.ttl is not None:
                payload["ttl"] = current.ttl
            self._request("PATCH", f"{zone_path}/{current.record_id}", json=payload)
            return Result.success("Cloudflare AAAA 记录更新成功", "updated")
        except (requests.RequestException, ValueError) as error:
            logger.warning(
                "Cloudflare 记录写入失败：类型=%s 状态码=%s 原因=%s",
                type(error).__name__,
                _http_status(error),
                error,
            )
            return Result.failure("Cloudflare DNS 写入失败，请检查凭据与记录配置")


class AlibabaProvider:
    name = "alibaba"

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        client_config = open_api_models.Config(
            access_key_id=config.alibaba_cloud_access_key_id,
            access_key_secret=config.alibaba_cloud_access_key_secret,
        )
        client_config.endpoint = "alidns.cn-hangzhou.aliyuncs.com"
        self._client = AlidnsClient(client_config)

    def get_status(self) -> Result[DnsStatus | None]:
        request = alidns_models.DescribeDomainRecordInfoRequest(
            record_id=self._config.alibaba_cloud_record_id
        )
        try:
            response = self._client.describe_domain_record_info_with_options(
                request, util_models.RuntimeOptions()
            )
            body = response.body
            value = body.value or ""
            rr = body.rr or self._config.alibaba_cloud_rr
            record_type = body.type or self._config.alibaba_cloud_ip_type
            status = DnsStatus(
                provider="alibaba",
                record_name=rr,
                record_type=record_type,
                value=value,
                record_id=self._config.alibaba_cloud_record_id,
            )
            return Result.success("阿里云 DNS 记录读取成功", status)
        except Exception as error:
            logger.warning(
                "阿里云记录查询失败：类型=%s 原因=%s", type(error).__name__, error
            )
            return Result.failure("阿里云 DNS 查询失败，请检查凭据与 Record ID")

    def set_ipv6(self, ipv6: str, current: DnsStatus | None) -> Result[UpdateAction]:
        if current is None:
            # 阿里云配置以 Record ID 为主键，缺失记录无法安全推导域名并新建。
            return Result.failure("阿里云记录不存在或不可访问，无法按 Record ID 更新")
        if current.value == ipv6:
            return Result.success("阿里云 AAAA 记录无需更新", "unchanged")
        request = alidns_models.UpdateDomainRecordRequest(
            record_id=self._config.alibaba_cloud_record_id,
            # Record ID 查询结果是事实来源，避免错误配置把已有记录重命名。
            rr=current.record_name,
            type=current.record_type.upper() or "AAAA",
            value=ipv6,
        )
        try:
            self._client.update_domain_record_with_options(
                request, util_models.RuntimeOptions()
            )
            return Result.success("阿里云 AAAA 记录更新成功", "updated")
        except Exception as error:
            logger.warning(
                "阿里云记录写入失败：类型=%s 原因=%s", type(error).__name__, error
            )
            return Result.failure("阿里云 DNS 写入失败，请检查凭据与记录配置")


def create_provider(config: AppConfig) -> DnsProvider:
    if config.provider == "cloudflare":
        return CloudflareProvider(config)
    return AlibabaProvider(config)
