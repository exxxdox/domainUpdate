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
    # 本地开发也读 .env：容器里的凭据来自那里，两边行为不一致最容易踩坑。
    # set -a 让 source 进来的变量自动导出；文件不存在时跳过（凭据本身是可选项）。
    if [ -f .env ]; then
      set -a
      # shellcheck disable=SC1091
      . ./.env
      set +a
    fi
    # 统一启动器确保无人打开页面时定时任务也会在进程启动后恢复。
    uv run python launcher.py
    ;;
  build)
    # 镜像名必须显式传入：默认名会和同名旧镜像混在一起，推错仓库的代价高于多打几个字。
    if [ -z "$2" ]; then
      echo "用法: $0 build <镜像名:标签>"
      exit 1
    fi
    echo "正在构建镜像 $2..."
    docker build \
      --build-arg "http_proxy=${http_proxy:-}" \
      --build-arg "https_proxy=${https_proxy:-}" \
      --build-arg "no_proxy=${no_proxy:-}" \
      -t "$2" .
    ;;
  *)
    echo "用法: $0 {init | update | run | build}"
    exit 1
    ;;
esac
