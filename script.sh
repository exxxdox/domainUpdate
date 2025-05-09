#!/bin/bash
# 创建一个新的虚拟环境
case "$1" in
  init)
    echo "正在初始化服务..."
    python3 -m venv myenv
    . myenv/bin/activate
    pip install -r requirement.txt
    deactivate
    ;;
  update)
    echo "正在更新依赖..."
    . myenv/bin/activate
    pip3 install -r requirement.txt
    deactivate
    ;;
  run)
    echo "正在运行..."
    . myenv/bin/activate
    . /etc/profile.d/custom.sh
    python3 domain_update_linux.py
    ;;
  *)
    echo "用法: $0 {init | update | run}"
    exit 1
    ;;
esac
