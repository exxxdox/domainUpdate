"""先恢复后台任务，再在同一进程启动 Streamlit。"""

from __future__ import annotations

import sys

from streamlit.web import cli as streamlit_cli

from domain_update.config import ConfigStore
from domain_update.scheduler import get_scheduler


def main() -> int:
    config_result = ConfigStore().load()
    if config_result.ok and config_result.data is not None:
        # 容器启动即恢复调度，不依赖用户先打开网页触发 Streamlit 脚本。
        get_scheduler().configure(config_result.data)

    sys.argv = [
        "streamlit",
        "run",
        "streamlit_app.py",
        "--server.address=0.0.0.0",
        "--server.port=8501",
        # 容器启动必须非交互，避免 Streamlit 首次运行询问邮箱而阻塞。
        "--server.headless=true",
        # 仅展示本地访问地址，避免启动日志探测并打印宿主公网 IP。
        "--browser.serverAddress=localhost",
        "--browser.gatherUsageStats=false",
    ]
    return streamlit_cli.main()


if __name__ == "__main__":
    raise SystemExit(main())
