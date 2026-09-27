"""运行时配置及安全持久化。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Mapping, cast

from dotenv import load_dotenv

from domain_update.models import ProviderName, Result


CONFIG_FILENAME = "config.json"
_CONFIG_LOCK = threading.RLock()


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


@dataclass(frozen=True)
class AppConfig:
    provider: ProviderName = "cloudflare"
    schedule_enabled: bool = False
    check_interval_minutes: int = 10
    alibaba_cloud_access_key_id: str = ""
    alibaba_cloud_access_key_secret: str = ""
    alibaba_cloud_record_id: str = ""
    alibaba_cloud_rr: str = ""
    alibaba_cloud_ip_type: str = "AAAA"
    cloudfare_token: str = ""
    cloudfare_zone_id: str = ""
    cloudfare_record_name: str = ""
    gotify_address: str = ""
    gotify_token: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AppConfig":
        source = os.environ if env is None else env
        provider_value = source.get("DOMAIN_UPDATE_PROVIDER", "cloudflare").lower()
        # 配置文件可能被手工修改，未知服务商安全回退，避免程序启动即崩溃。
        provider: ProviderName = (
            cast(ProviderName, provider_value)
            if provider_value in {"cloudflare", "alibaba"}
            else "cloudflare"
        )
        return cls(
            provider=provider,
            schedule_enabled=_env_bool(
                source.get("DOMAIN_UPDATE_SCHEDULE_ENABLED"), False
            ),
            check_interval_minutes=max(
                1, _env_int(source.get("DOMAIN_UPDATE_CHECK_INTERVAL_MINUTES"), 10)
            ),
            alibaba_cloud_access_key_id=source.get(
                "ALIBABA_CLOUD_ACCESS_KEY_ID", ""
            ),
            alibaba_cloud_access_key_secret=source.get(
                "ALIBABA_CLOUD_ACCESS_KEY_SECRET", ""
            ),
            alibaba_cloud_record_id=source.get("ALIBABA_CLOUD_RECORDID", ""),
            alibaba_cloud_rr=source.get("ALIBABA_CLOUD_RR", ""),
            alibaba_cloud_ip_type=source.get("ALIBABA_CLOUD_IPTYPE", "AAAA")
            or "AAAA",
            cloudfare_token=source.get("CLOUDFARE_TOKEN", ""),
            cloudfare_zone_id=source.get("CLOUDFARE_ZONE_ID", ""),
            cloudfare_record_name=source.get("CLOUDFARE_RECORD_NAME", ""),
            gotify_address=source.get("GOTIFY_ADDRESS", ""),
            gotify_token=source.get("GOTIFY_TOKEN", ""),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AppConfig":
        allowed = {field.name for field in fields(cls)}
        raw = {key: item for key, item in value.items() if key in allowed}
        provider_value = str(raw.get("provider", "cloudflare")).lower()
        if provider_value not in {"cloudflare", "alibaba"}:
            raise ValueError("服务商只能是 cloudflare 或 alibaba")
        raw["provider"] = provider_value
        raw["schedule_enabled"] = bool(raw.get("schedule_enabled", False))
        try:
            raw["check_interval_minutes"] = int(
                raw.get("check_interval_minutes", 10)
            )
        except (TypeError, ValueError) as error:
            raise ValueError("检查间隔必须是整数") from error
        return cls(**raw)

    def validate(self) -> Result["AppConfig"]:
        if self.check_interval_minutes < 1:
            return Result.failure("定时检查间隔必须至少为 1 分钟")
        if self.provider == "cloudflare":
            missing = [
                label
                for label, value in (
                    ("Cloudflare API Token", self.cloudfare_token),
                    ("Cloudflare Zone ID", self.cloudfare_zone_id),
                    ("Cloudflare 记录名称", self.cloudfare_record_name),
                )
                if not value.strip()
            ]
        else:
            missing = [
                label
                for label, value in (
                    ("阿里云 AccessKey ID", self.alibaba_cloud_access_key_id),
                    ("阿里云 AccessKey Secret", self.alibaba_cloud_access_key_secret),
                    ("阿里云 Record ID", self.alibaba_cloud_record_id),
                    ("阿里云主机记录 RR", self.alibaba_cloud_rr),
                )
                if not value.strip()
            ]
        if missing:
            return Result.failure(f"当前服务商缺少配置：{'、'.join(missing)}")
        if self.provider == "alibaba" and self.alibaba_cloud_ip_type.upper() != "AAAA":
            return Result.failure("IPv6 更新要求阿里云记录类型为 AAAA")
        return Result.success("配置有效", self)


class ConfigStore:
    """在进程内串行读写，并通过同目录替换避免半写入 JSON。"""

    def __init__(self, data_dir: Path | None = None) -> None:
        configured_dir = os.environ.get("DOMAIN_UPDATE_DATA_DIR", ".data")
        self.data_dir = data_dir or Path(configured_dir)
        self.path = self.data_dir / CONFIG_FILENAME
        # 所有 ConfigStore 实例共用锁，协调 Streamlit 多会话和后台线程。
        self._lock = _CONFIG_LOCK

    def load(self) -> Result[AppConfig]:
        with self._lock:
            if not self.path.exists():
                # 首次启动兼容旧部署的 .env，后续均以持久化配置为准。
                load_dotenv()
                config = AppConfig.from_env()
                saved = self.save(config)
                if not saved.ok:
                    return Result.failure(saved.message)
                return Result.success("已从环境变量初始化配置", config)
            try:
                with self.path.open("r", encoding="utf-8") as stream:
                    raw = json.load(stream)
                if not isinstance(raw, dict):
                    return Result.failure("配置文件格式错误：顶层必须是对象")
                config = AppConfig.from_dict(raw)
                return Result.success("配置读取成功", config)
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as error:
                # 错误只报告类型，不回显可能包含密钥的配置内容。
                return Result.failure(f"配置读取失败（{type(error).__name__}）")

    def save(self, config: AppConfig) -> Result[AppConfig]:
        with self._lock:
            temporary_path: Path | None = None
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                descriptor, name = tempfile.mkstemp(
                    prefix="config-", suffix=".tmp", dir=self.data_dir
                )
                temporary_path = Path(name)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(asdict(config), stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.chmod(temporary_path, 0o600)
                except OSError:
                    # Windows ACL 不等同 POSIX 权限，chmod 仅作尽力保护。
                    pass
                os.replace(temporary_path, self.path)
                temporary_path = None
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
                return Result.success("配置保存成功", config)
            except OSError as error:
                return Result.failure(f"配置保存失败（{type(error).__name__}）")
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError:
                        pass

