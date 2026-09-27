from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from datetime import datetime
from typing import Any, Mapping

import streamlit as st

# 后端适配集中在这里：页面不依赖具体实现细节，便于服务层演进时只改一处。
from domain_update.config import AppConfig, ConfigStore
from domain_update.scheduler import get_scheduler
from domain_update.service import DomainUpdateService


PROVIDER_LABELS = {
    "cloudflare": "Cloudflare",
    "alibaba": "阿里云",
}

FIELD_NAMES = {
    "provider": ("provider", "dns_provider"),
    "cloudflare_token": ("cloudflare_token", "CLOUDFARE_TOKEN"),
    "cloudflare_zone_id": (
        "cloudfare_zone_id",
        "cloudflare_zone_id",
        "CLOUDFARE_ZONE_ID",
    ),
    "cloudflare_record_name": (
        "cloudfare_record_name",
        "cloudflare_record_name",
        "CLOUDFARE_RECORD_NAME",
    ),
    "alibaba_access_key_id": (
        "alibaba_cloud_access_key_id",
        "alibaba_access_key_id",
        "ALIBABA_CLOUD_ACCESS_KEY_ID",
    ),
    "alibaba_access_key_secret": (
        "alibaba_cloud_access_key_secret",
        "alibaba_access_key_secret",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
    ),
    "alibaba_record_id": (
        "alibaba_cloud_record_id",
        "alibaba_cloud_recordid",
        "alibaba_record_id",
        "ALIBABA_CLOUD_RECORDID",
    ),
    "alibaba_rr": ("alibaba_cloud_rr", "alibaba_rr", "ALIBABA_CLOUD_RR"),
    "alibaba_ip_type": (
        "alibaba_cloud_iptype",
        "alibaba_ip_type",
        "ALIBABA_CLOUD_IPTYPE",
    ),
    "gotify_address": ("gotify_address", "GOTIFY_ADDRESS"),
    "gotify_token": ("gotify_token", "GOTIFY_TOKEN"),
    "schedule_enabled": ("schedule_enabled", "scheduler_enabled"),
    "schedule_interval_minutes": (
        "check_interval_minutes",
        "schedule_interval_minutes",
        "interval_minutes",
        "scheduler_interval_minutes",
    ),
}

# 历史环境变量沿用 CLOUDFARE 拼写，配置模型也保留该字段名以避免迁移破坏。
FIELD_NAMES["cloudflare_token"] = (
    "cloudfare_token",
    "cloudflare_token",
    "CLOUDFARE_TOKEN",
)


def _read_value(source: Any, *names: str, default: Any = "") -> Any:
    """兼容 dataclass、普通对象和映射，避免 UI 与配置存储格式互相绑死。"""
    if source is None:
        return default
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return default


def _config_value(config: AppConfig | None, key: str, default: Any = "") -> Any:
    return _read_value(config, *FIELD_NAMES[key], default=default)


def _call_first(target: Any, names: tuple[str, ...], *args: Any) -> Any:
    for name in names:
        method = getattr(target, name, None)
        if callable(method):
            return method(*args)
    raise AttributeError(f"未找到可用方法：{', '.join(names)}")


def load_config() -> AppConfig:
    store = ConfigStore()
    return unwrap_result(_call_first(store, ("load", "get")))


def save_config(config: AppConfig) -> None:
    store = ConfigStore()
    unwrap_result(_call_first(store, ("save", "set"), config))


def validate_config(config: AppConfig) -> None:
    validator = getattr(config, "validate", None)
    if callable(validator):
        unwrap_result(validator())


def build_config(values: Mapping[str, Any], current: AppConfig | None) -> AppConfig:
    """仅传入 AppConfig 实际接受的字段，并让空白密钥保留原值。"""
    normalized: dict[str, Any] = {}
    for logical_name, aliases in FIELD_NAMES.items():
        value = values.get(logical_name)
        secret_clear_flags = {
            "cloudflare_token": "clear_cloudflare_token",
            "alibaba_access_key_secret": "clear_alibaba_access_key_secret",
            "gotify_token": "clear_gotify",
        }
        if logical_name in secret_clear_flags:
            if values.get(secret_clear_flags[logical_name], False):
                value = ""
            else:
                value = value or _config_value(current, logical_name)
        if logical_name == "gotify_address" and values.get("clear_gotify", False):
            value = ""
        for alias in aliases:
            normalized[alias] = value

    if current is not None and is_dataclass(current):
        valid = {field.name for field in fields(current)}
        updates = {key: value for key, value in normalized.items() if key in valid}
        return replace(current, **updates)

    if is_dataclass(AppConfig):
        valid = {field.name for field in fields(AppConfig)}
        return AppConfig(**{key: value for key, value in normalized.items() if key in valid})

    # 非 dataclass 配置通常仍接受关键字参数；优先使用规范的 snake_case 名称。
    canonical = {aliases[0]: values.get(key) for key, aliases in FIELD_NAMES.items()}
    return AppConfig(**canonical)


def configure_scheduler(config: AppConfig) -> None:
    scheduler = get_scheduler()
    result = _call_first(scheduler, ("configure",), config)
    if result is not None:
        unwrap_result(result)


def scheduler_snapshot() -> Any:
    try:
        scheduler = get_scheduler()
        for name in ("status", "get_status", "snapshot"):
            value = getattr(scheduler, name, None)
            if callable(value):
                return value()
            if value is not None:
                return value
        return scheduler
    except Exception:
        # 调度器状态不应阻止用户配置或手动更新 DNS。
        return None


def invoke_service(method_name: str, config: AppConfig) -> Any:
    # 服务从同一持久化配置读取，手动操作与定时任务始终使用一致参数。
    result = getattr(DomainUpdateService(), method_name)()
    message = result_text(result, "message", default="")
    if message:
        st.session_state.last_service_message = message
    return unwrap_result(result)


def unwrap_result(result: Any) -> Any:
    """统一处理应用层 Result，同时兼容直接返回数据的轻量实现。"""
    if not hasattr(result, "ok"):
        return result
    if not bool(result.ok):
        raise RuntimeError(result_text(result, "message", default="操作失败"))
    data = getattr(result, "data", None)
    if data is None:
        raise RuntimeError("操作成功但未返回数据")
    return data


def result_text(result: Any, *names: str, default: str = "—") -> str:
    value = _read_value(result, *names, default=default)
    return default if value in (None, "") else str(value)


def format_time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return str(value) if value else "尚未执行"


st.set_page_config(
    page_title="IPv6 域名控制台",
    page_icon="⌁",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    :root {
        --canvas: #08131f;
        --panel: #0d1c2a;
        --panel-strong: #102536;
        --line: #203c50;
        --link: #38c8e8;
        --link-soft: #95e9f7;
        --text: #edf8fb;
        --muted: #8eabb9;
        --warning: #f6a640;
    }
    .stApp {
        background:
            linear-gradient(rgba(56, 200, 232, .028) 1px, transparent 1px),
            linear-gradient(90deg, rgba(56, 200, 232, .028) 1px, transparent 1px),
            var(--canvas);
        background-size: 38px 38px;
        color: var(--text);
    }
    [data-testid="stSidebar"] {
        background: #091724;
        border-right: 1px solid var(--line);
    }
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
    [data-testid="stSidebar"] label { color: #c5dce5; }
    .block-container {
        max-width: 1320px;
        padding-top: 2.2rem;
        padding-bottom: 4rem;
    }
    h1, h2, h3 { color: var(--text) !important; letter-spacing: -.035em; }
    h1 { font-size: clamp(2.15rem, 5vw, 4.2rem) !important; font-weight: 520 !important; }
    h2 { font-size: 1.3rem !important; font-weight: 590 !important; }
    p, label, [data-testid="stCaptionContainer"] { color: var(--muted); }
    .console-kicker {
        color: var(--link-soft);
        font-size: .85rem;
        margin-bottom: .55rem;
    }
    .console-lead {
        max-width: 660px;
        color: #abc5d0;
        font-size: 1rem;
        line-height: 1.7;
        margin: -.55rem 0 1.65rem;
    }
    .ip-focus {
        position: relative;
        overflow: hidden;
        min-height: 180px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        padding: clamp(1.3rem, 4vw, 2.3rem);
        border: 1px solid #2a5770;
        border-left: 4px solid var(--link);
        background: linear-gradient(105deg, #0d2232 0%, #0a1825 72%);
    }
    .ip-focus::after {
        content: "";
        position: absolute;
        width: 240px;
        height: 240px;
        right: -100px;
        top: -110px;
        border: 1px solid rgba(56, 200, 232, .22);
        border-radius: 50%;
        box-shadow: 0 0 0 34px rgba(56, 200, 232, .035), 0 0 0 70px rgba(56, 200, 232, .025);
    }
    .ip-label { color: #89adbc; font-size: .82rem; margin-bottom: .55rem; }
    .ip-value {
        position: relative;
        z-index: 1;
        color: #eafcff;
        font: 500 clamp(1.15rem, 3vw, 2.25rem)/1.25 "Cascadia Code", "SFMono-Regular", Consolas, monospace;
        overflow-wrap: anywhere;
    }
    .ip-meta { color: #78cfe1; font-size: .8rem; margin-top: .75rem; }
    div[data-testid="stMetric"] {
        padding: 1rem 0 1rem 1.15rem;
        border-left: 1px solid var(--line);
        background: transparent;
    }
    div[data-testid="stMetric"] label { color: #86a5b3; }
    div[data-testid="stMetricValue"] { color: var(--text); font-size: 1.18rem; }
    div[data-testid="stButton"] button, div[data-testid="stFormSubmitButton"] button {
        min-height: 2.8rem;
        border-radius: 4px;
        border: 1px solid #2e637b;
        background: #102a3b;
        color: #e7faff;
        font-weight: 600;
    }
    div[data-testid="stButton"] button:hover, div[data-testid="stFormSubmitButton"] button:hover {
        border-color: var(--link);
        color: white;
        background: #13364a;
    }
    div[data-testid="stButton"] button:focus-visible,
    div[data-testid="stFormSubmitButton"] button:focus-visible,
    input:focus-visible, textarea:focus-visible {
        outline: 3px solid rgba(56, 200, 232, .35) !important;
        outline-offset: 2px;
    }
    div[data-testid="stForm"] {
        border: 1px solid var(--line);
        border-radius: 6px;
        padding: 1.35rem;
        background: rgba(13, 28, 42, .72);
    }
    [data-testid="stExpander"] { border-color: var(--line) !important; background: #0b1a27; }
    div[data-baseweb="input"] > div, div[data-baseweb="select"] > div {
        border-color: #29495b !important;
        background-color: #0a1824 !important;
    }
    .status-note {
        border-left: 3px solid var(--warning);
        padding: .65rem .9rem;
        color: #d7c19f;
        background: rgba(246, 166, 64, .07);
        font-size: .86rem;
    }
    hr { border-color: var(--line) !important; }
    @media (max-width: 700px) {
        .block-container { padding: 1.2rem 1rem 3rem; }
        .ip-focus { min-height: 150px; }
    }
    @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; animation: none !important; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def bootstrap_state() -> None:
    defaults = {
        "config": None,
        "config_error": None,
        "public_ipv6": None,
        "ip_checked_at": None,
        "dns_status": None,
        "dns_checked_at": None,
        "update_result": None,
        "last_service_message": None,
        "scheduler_initialized": False,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    try:
        # 每次 rerun 刷新共享配置，防止其他浏览器会话切换服务商后展示旧状态。
        st.session_state.config = load_config()
        st.session_state.config_error = None
    except Exception as exc:  # 配置缺失时仍允许进入页面完成首次设置。
        st.session_state.config_error = str(exc)


bootstrap_state()
config: AppConfig | None = st.session_state.config
if config is not None and not st.session_state.scheduler_initialized:
    try:
        configure_scheduler(config)
        st.session_state.scheduler_initialized = True
    except Exception as exc:
        st.session_state.config_error = f"定时检查初始化失败：{exc}"

with st.sidebar:
    st.markdown("## 连接设置")
    st.caption("密钥留空会保留当前值，页面不会回显已保存的密钥。")

    provider_keys = list(PROVIDER_LABELS)
    current_provider = str(_config_value(config, "provider", "cloudflare")).lower()
    if current_provider in {"aliyun", "alicloud"}:
        current_provider = "alibaba"
    # 服务商置于 form 外，使切换后立即显示对应字段，但不会触发任何网络请求。
    provider = st.radio(
        "DNS 服务商",
        provider_keys,
        index=provider_keys.index(current_provider) if current_provider in provider_keys else 0,
        format_func=lambda key: PROVIDER_LABELS[key],
        horizontal=True,
    )

    with st.form("settings_form", clear_on_submit=False):
        if provider == "cloudflare":
            cloudflare_token = st.text_input(
                "API Token",
                type="password",
                placeholder="留空以保留当前值",
            )
            cloudflare_zone_id = st.text_input(
                "Zone ID",
                value=str(_config_value(config, "cloudflare_zone_id")),
            )
            cloudflare_record_name = st.text_input(
                "完整域名",
                value=str(_config_value(config, "cloudflare_record_name")),
                placeholder="home.example.com",
            )
            alibaba_access_key_id = str(_config_value(config, "alibaba_access_key_id"))
            alibaba_access_key_secret = ""
            alibaba_record_id = str(_config_value(config, "alibaba_record_id"))
            alibaba_rr = str(_config_value(config, "alibaba_rr"))
            alibaba_ip_type = str(_config_value(config, "alibaba_ip_type", "AAAA"))
        else:
            alibaba_access_key_id = st.text_input(
                "AccessKey ID",
                value=str(_config_value(config, "alibaba_access_key_id")),
            )
            alibaba_access_key_secret = st.text_input(
                "AccessKey Secret",
                type="password",
                placeholder="留空以保留当前值",
            )
            alibaba_record_id = st.text_input(
                "解析记录 ID",
                value=str(_config_value(config, "alibaba_record_id")),
            )
            alibaba_rr = st.text_input(
                "主机记录",
                value=str(_config_value(config, "alibaba_rr")),
                placeholder="home",
            )
            alibaba_ip_type = st.selectbox(
                "记录类型",
                ["AAAA"],
                index=0,
            )
            cloudflare_token = ""
            cloudflare_zone_id = str(_config_value(config, "cloudflare_zone_id"))
            cloudflare_record_name = str(_config_value(config, "cloudflare_record_name"))

        with st.expander("已保存凭据"):
            st.caption("当前服务商必须保留有效凭据；切换后可以清除另一家的密钥。")
            clear_cloudflare_token = st.checkbox(
                "清除 Cloudflare API Token",
                disabled=provider == "cloudflare",
            )
            clear_alibaba_access_key_secret = st.checkbox(
                "清除阿里云 AccessKey Secret",
                disabled=provider == "alibaba",
            )

        with st.expander("更新通知"):
            gotify_address = st.text_input(
                "Gotify 地址",
                value=str(_config_value(config, "gotify_address")),
                placeholder="notify.example.com",
            )
            gotify_token = st.text_input(
                "Gotify Token",
                type="password",
                placeholder="留空以保留当前值",
            )
            clear_gotify = st.checkbox("清除已保存的 Gotify 配置")

        st.markdown("### 定时检查")
        schedule_enabled = st.toggle(
            "启用定时检查",
            value=bool(_config_value(config, "schedule_enabled", False)),
        )
        schedule_interval_minutes = st.number_input(
            "检查间隔（分钟）",
            min_value=1,
            max_value=10_080,
            value=max(1, int(_config_value(config, "schedule_interval_minutes", 10) or 10)),
            disabled=not schedule_enabled,
        )

        save_clicked = st.form_submit_button("保存设置", use_container_width=True)

    if save_clicked:
        submitted = {
            "provider": provider,
            "cloudflare_token": cloudflare_token,
            "clear_cloudflare_token": clear_cloudflare_token,
            "cloudflare_zone_id": cloudflare_zone_id.strip(),
            "cloudflare_record_name": cloudflare_record_name.strip(),
            "alibaba_access_key_id": alibaba_access_key_id.strip(),
            "alibaba_access_key_secret": alibaba_access_key_secret,
            "clear_alibaba_access_key_secret": clear_alibaba_access_key_secret,
            "alibaba_record_id": alibaba_record_id.strip(),
            "alibaba_rr": alibaba_rr.strip(),
            "alibaba_ip_type": alibaba_ip_type,
            "gotify_address": gotify_address.strip(),
            "gotify_token": gotify_token,
            "clear_gotify": clear_gotify,
            "schedule_enabled": schedule_enabled,
            "schedule_interval_minutes": int(schedule_interval_minutes),
        }
        try:
            saved_config = build_config(submitted, config)
            validate_config(saved_config)
            save_config(saved_config)
            configure_scheduler(saved_config)
            st.session_state.config = saved_config
            st.session_state.config_error = None
            config = saved_config
            st.success("设置已保存，定时检查状态已同步。")
        except Exception as exc:
            st.error(f"保存失败：{exc}")


st.markdown('<div class="console-kicker">公网链路状态</div>', unsafe_allow_html=True)
st.title("IPv6 域名控制台")
st.markdown(
    '<div class="console-lead">查看公网地址与 DNS 指向，在地址变化时立即同步解析记录。</div>',
    unsafe_allow_html=True,
)

ipv6_value = st.session_state.public_ipv6 or "等待检测"
ip_checked_at = format_time(st.session_state.ip_checked_at)
st.markdown(
    f"""
    <section class="ip-focus" aria-label="当前公网 IPv6">
        <div class="ip-label">当前公网 IPv6</div>
        <div class="ip-value">{ipv6_value}</div>
        <div class="ip-meta">最近检测：{ip_checked_at}</div>
    </section>
    """,
    unsafe_allow_html=True,
)

action_left, action_middle, action_right = st.columns([1, 1, 1.25])
with action_left:
    detect_clicked = st.button("检测公网 IPv6", use_container_width=True)
with action_middle:
    dns_clicked = st.button("查询 DNS 记录", use_container_width=True)
with action_right:
    update_clicked = st.button("检查并更新", type="primary", use_container_width=True)

if config is None:
    st.markdown(
        '<div class="status-note">请先在左侧保存连接设置，再执行网络操作。</div>',
        unsafe_allow_html=True,
    )
elif detect_clicked:
    try:
        with st.spinner("正在检测公网 IPv6…"):
            result = invoke_service("get_public_ipv6", config)
        st.session_state.public_ipv6 = result_text(result, "address", "ipv6", "ip", default=str(result))
        st.session_state.ip_checked_at = datetime.now().astimezone()
        st.rerun()
    except Exception as exc:
        st.error(f"IPv6 检测失败：{exc}")
elif dns_clicked:
    try:
        with st.spinner("正在查询 DNS 记录…"):
            st.session_state.dns_status = invoke_service("get_dns_status", config)
        st.session_state.dns_checked_at = datetime.now().astimezone()
        st.rerun()
    except Exception as exc:
        st.error(f"DNS 查询失败：{exc}")
elif update_clicked:
    try:
        with st.spinner("正在检查并同步解析记录…"):
            result = invoke_service("check_and_update", config)
        st.session_state.update_result = result
        detected_ip = _read_value(result, "ipv6", "address", "current_ipv6", default=None)
        if detected_ip:
            st.session_state.public_ipv6 = str(detected_ip)
            st.session_state.ip_checked_at = datetime.now().astimezone()
        st.session_state.dns_status = result
        st.session_state.dns_checked_at = datetime.now().astimezone()
        st.rerun()
    except Exception as exc:
        st.error(f"更新失败：{exc}")

st.divider()

status = st.session_state.dns_status
provider_name = PROVIDER_LABELS.get(
    str(_config_value(config, "provider", "cloudflare")).lower(),
    str(_config_value(config, "provider", "Cloudflare")),
)
record_name = result_text(
    status,
    "record_name",
    "name",
    "domain",
    default=str(
        _config_value(config, "cloudflare_record_name")
        if str(_config_value(config, "provider", "cloudflare")).lower() == "cloudflare"
        else _config_value(config, "alibaba_rr")
    )
    or "—",
)
record_value = result_text(
    status,
    "content",
    "value",
    "current_value",
    "ipv6",
    "address",
)

st.markdown("## DNS 状态")
metric_provider, metric_record, metric_value = st.columns([.8, 1.15, 1.6])
metric_provider.metric("当前服务商", provider_name)
metric_record.metric("解析记录", record_name)
metric_value.metric("记录值", record_value)
st.caption(f"最近查询：{format_time(st.session_state.dns_checked_at)}")

if st.session_state.update_result is not None:
    update_result = st.session_state.update_result
    success = bool(_read_value(update_result, "success", "ok", default=True))
    action = result_text(update_result, "action", default="unchanged")
    changed = bool(
        _read_value(update_result, "changed", "updated", default=False)
    ) or action in {"created", "updated"}
    message = result_text(
        {"message": st.session_state.last_service_message},
        "message",
        default="DNS 记录已更新。" if changed else "地址未变化，无需更新。",
    )
    (st.success if success else st.error)(message)

st.markdown("## 定时检查")
schedule_state = scheduler_snapshot()
schedule_active = bool(
    _read_value(
        schedule_state,
        "running",
        "enabled",
        default=_config_value(config, "schedule_enabled", False),
    )
)
schedule_interval = _read_value(
    schedule_state,
    "interval_minutes",
    "interval",
    default=_config_value(config, "schedule_interval_minutes", 10),
)
next_run = _read_value(schedule_state, "next_run", "next_run_at", default=None)
schedule_col, interval_col, next_col = st.columns(3)
schedule_col.metric("运行状态", "已启用" if schedule_active else "已关闭")
interval_col.metric("检查间隔", f"{schedule_interval} 分钟" if schedule_active else "—")
next_col.metric("下次检查", format_time(next_run) if schedule_active else "—")

if st.session_state.config_error:
    st.info("尚未读取到已保存的设置，请在左侧完成首次配置。")
