"""运行时配置及安全持久化。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Mapping

from domain_update.models import ProviderName, Result


CONFIG_FILENAME = "config.json"
_CONFIG_LOCK = threading.RLock()

logger = logging.getLogger(__name__)


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
                # 配置只此一处来源：首次启动直接落一份默认配置，
                # 用户打开页面填入凭据即可，不再有"文件没写但环境变量生效"这种中间态。
                config = AppConfig()
                saved = self.save(config)
                if not saved.ok:
                    return Result.failure(saved.message)
                return Result.success("已创建默认配置，请在页面中完成设置", config)
            try:
                with self.path.open("r", encoding="utf-8") as stream:
                    raw = json.load(stream)
                if not isinstance(raw, dict):
                    logger.error("配置读取失败：%s 顶层不是 JSON 对象", self.path)
                    return Result.failure("配置文件格式错误：顶层必须是对象")
                config = AppConfig.from_dict(raw)
                return Result.success("配置读取成功", config)
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as error:
                # 只记录路径与原因，绝不记录文件内容：配置里含明文密钥。
                logger.error(
                    "配置读取失败：路径=%s 原因=%s errno=%s",
                    self.path,
                    type(error).__name__,
                    getattr(error, "errno", None),
                )
                return Result.failure(f"配置读取失败（{type(error).__name__}）")

    def save(self, config: AppConfig) -> Result[AppConfig]:
        with self._lock:
            temporary_path: Path | None = None
            # 记录当前步骤：失败时日志能直接指出卡在哪一步，而不是只给一个异常类名。
            step = "创建数据目录"
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                step = "创建临时文件"
                descriptor, name = tempfile.mkstemp(
                    prefix="config-", suffix=".tmp", dir=self.data_dir
                )
                temporary_path = Path(name)
                step = "写入临时文件"
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(asdict(config), stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                step = "设置临时文件权限"
                try:
                    os.chmod(temporary_path, 0o600)
                except OSError:
                    # Windows ACL 不等同 POSIX 权限，chmod 仅作尽力保护。
                    pass
                step = "替换配置文件"
                os.replace(temporary_path, self.path)
                temporary_path = None
                step = "设置配置文件权限"
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
                logger.info("配置已保存：%s", self.path)
                return Result.success("配置保存成功", config)
            except OSError as error:
                # 只回异常类名无法定位问题：路径、errno、失败步骤全部丢失，
                # 在容器里只能看到"PermissionError"却不知道是目录不可写还是文件被锁。
                logger.error(
                    "配置保存失败：步骤=%s 数据目录=%s 目标文件=%s errno=%s strerror=%s",
                    step,
                    self.data_dir,
                    self.path,
                    error.errno,
                    error.strerror,
                )
                if error.filename:
                    logger.error("配置保存失败：内核报告的文件=%s", error.filename)
                logger.exception("配置保存失败堆栈")
                return Result.failure(
                    f"配置保存失败（{type(error).__name__}：{error.strerror or error}）"
                )
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError:
                        pass

