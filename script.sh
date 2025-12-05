#!/bin/bash
venv_name="myenv"
# 创建一个新的虚拟环境
case "$1" in
  init)
    echo "正在初始化服务..."
    python3 -m venv ${venv_name}
    . ${venv_name}/bin/activate
    pip install -r requirement.txt
    deactivate
    ;;
  update)
    echo "正在更新依赖..."
    . ${venv_name}/bin/activate
    pip install -r requirement.txt
    pip freeze > requirement.txt
    deactivate
    ;;
  run)
    echo "正在运行..."
    . ${venv_name}/bin/activate
    python3 main.py
    ;;
  *)
    echo "用法: $0 {init | update | run}"
    exit 1
    ;;
esac
