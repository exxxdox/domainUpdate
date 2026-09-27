"""应用层可直接消费的结构化结果模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar


ProviderName = Literal["cloudflare", "alibaba"]
UpdateAction = Literal["created", "updated", "unchanged", "failed"]

T = TypeVar("T")


@dataclass(frozen=True)
class Result(Generic[T]):
    """统一封装成功数据与适合直接展示的中文消息。"""

    ok: bool
    message: str
    data: T | None = None

    @classmethod
    def success(cls, message: str, data: T) -> "Result[T]":
        return cls(ok=True, message=message, data=data)

    @classmethod
    def failure(cls, message: str) -> "Result[T]":
        return cls(ok=False, message=message)


@dataclass(frozen=True)
class DnsStatus:
    provider: ProviderName
    record_name: str
    record_type: str
    value: str
    record_id: str
    proxied: bool | None = None
    ttl: int | None = None


@dataclass(frozen=True)
class UpdateStatus:
    provider: ProviderName
    action: UpdateAction
    ipv6: str
    previous_value: str | None
    current_value: str

