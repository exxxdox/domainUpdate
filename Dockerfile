# 构建阶段：uv 官方镜像，tag 里把 uv 版本、Python 版本、Alpine 版本全部写死。
# 不要退回浮动的 ...-python3.12-alpine：那条 tag 会随上游推进 Alpine 主版本，于是构建阶段
# 与运行阶段的 musl 分叉，musllinux wheel 可能链接到运行阶段不存在的 soname。
# 实测 0.12.19-python3.12-alpine3.23 与 python:3.12-alpine3.23 的 CPython 完全同源
# （libpython3.12.so.1.0 的 sha256 一致），venv 可原样复制到运行阶段。
# 拆成独立阶段的目的：uv 二进制（约 57MB）只服务于依赖安装，不应进入最终镜像。
# 注意：在后续层 rm 基础镜像里的文件并不会缩小体积（只产生 whiteout，字节仍留在原层），
# 因此必须靠换基础镜像来去掉 uv，而不是靠 RUN rm。
FROM ghcr.io/astral-sh/uv:0.12.19-python3.12-alpine3.23 AS builder

# UV_COMPILE_BYTECODE 预编译 .pyc，配合运行期的 PYTHONDONTWRITEBYTECODE 可免去每次启动的重新编译；
# UV_LINK_MODE=copy 避免依赖跨设备硬链接，保证 venv 内是真实文件、可被 COPY 搬运。
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 先复制依赖声明以复用 Docker 层缓存；项目没有 build-system，因此不安装项目本身。
COPY pyproject.toml uv.lock ./

# cache mount 把 uv 的下载/解包缓存放在构建缓存里而不是镜像层里。这是体积优化的关键：
# 原先缓存随层提交，单是 /root/.cache/uv/archive-v0 就有 437MB，而它运行期完全用不到。
# 之后的三步清理只针对运行期用不到的内容：
#   tests/include —— 依赖自带的测试用例与 C 头文件，合计约 60MB；
#   strip —— 剥掉 .so 的符号表，venv 由 40MB 降到 31MB（cryptography 的 Rust 扩展占大头）。
# binutils 只装在本阶段，随构建阶段一起丢弃，不会进入最终镜像。
RUN --mount=type=cache,target=/root/.cache/uv \
    apk add --no-cache binutils \
    && uv sync --frozen --no-dev --no-install-project \
    && find /app/.venv -type d \( -name tests -o -name test \) -prune -exec rm -rf {} + \
    && find /app/.venv -type d -name include -prune -exec rm -rf {} + \
    && find /app/.venv -name '*.so' -exec strip --strip-unneeded {} +


# 运行阶段：Alpine 版 Python。根文件系统比 debian slim 小约 100MB（磁盘占用）
# 与 26MB（分层压缩后的实际下载量），代价是 musl libc：
# musl 默认线程栈只有 1MiB（glibc 8MiB），深嵌套 JSON 会触发 SIGSEGV 而不是
# RecursionError，因此 launcher 启动时统一把线程栈设回 8MiB，见 domain_update/thread_stack.py。
FROM python:3.12-alpine3.23

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOMAIN_UPDATE_DATA_DIR=/app/data \
    PATH="/app/.venv/bin:${PATH}"

# 使用固定 UID 的非 root 用户，避免 Web UI 或持久化配置以 root 身份运行。
# Alpine 只有 busybox 的 adduser，没有 useradd。
# 额外补一条指向 UTC 的 /etc/localtime：debian 的 tzdata 包会自动建立该链接，apk 包不会，
# 缺失时 tzlocal 每次启动都会打一条 "Can not find any timezone configuration" 警告。
# 设 TZ 环境变量仍可覆盖成其他时区，与 debian 版行为一致。
RUN adduser -D -u 10001 -s /sbin/nologin -h /home/appuser appuser \
    && mkdir -p /app/data \
    && chown appuser:appuser /app/data \
    && ln -sf /usr/share/zoneinfo/Etc/UTC /etc/localtime

# 只搬运构建产物。两个阶段同为 Alpine 3.23.6 + CPython 3.12.14，解释器都装在 /usr/local，
# 因此 venv 内的 pyvenv.cfg、bin/python 软链和脚本 shebang 指向的 /usr/local/bin/python3.12
# 在运行阶段就是同一个文件，可直接运行。
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv

# 显式列出运行期文件，不用 COPY . .：后者会把 uv.lock(352KB)、pyproject.toml、
# compose.yaml、README 等只服务于开发与部署的文件一并带进镜像。
COPY --chown=appuser:appuser domain_update ./domain_update
COPY --chown=appuser:appuser launcher.py main.py ./

USER appuser

# 仅作说明，取默认端口；实际端口由 WEB_PORT 决定。
EXPOSE 8501

# 使用 Python 标准库检查控制台健康端点，避免额外安装 curl。
# 端口必须跟 WEB_PORT 走，否则改了端口后容器会被自己的探活判成不健康。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; port = os.environ.get('WEB_PORT', '8501'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3).read()"]

CMD ["python", "launcher.py"]
