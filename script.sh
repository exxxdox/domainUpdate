#!/bin/bash
case "$1" in
  init)
    echo "正在初始化服务..."
    # 锁文件保证开发机、服务器和容器安装完全一致的依赖版本。
    uv sync --frozen
    ;;
  update)
    echo "正在更新依赖..."
    # 依赖升级由 uv 统一解析并写入锁文件，不再维护 pip freeze 快照。
    uv lock --upgrade
    uv sync
    ;;
  run)
    echo "正在运行..."
    # 统一启动器确保无人打开页面时定时任务也会在进程启动后恢复。
    uv run python launcher.py
    ;;
  *)
    echo "用法: $0 {init | update | run}"
    exit 1
    ;;
esac
