# 构建阶段：沿用 uv 官方固定版本镜像。它与 python:3.12-slim-trixie 使用同一份 CPython 构建
# （版本、安装路径 /usr/local 完全一致），因此这里产出的 venv 可原样复制到运行阶段。
# 拆成独立阶段的目的：uv 二进制（约 57MB）只服务于依赖安装，不应进入最终镜像，
# 且 uv 镜像比运行镜像大 83MB，不适合作运行基础。
# 注意：在后续层 rm 基础镜像里的文件并不会缩小体积（只产生 whiteout，字节仍留在原层），
# 因此必须靠换基础镜像来去掉 uv，而不是靠 RUN rm。
FROM ghcr.io/astral-sh/uv:0.12.19-python3.12-trixie-slim AS builder

# UV_COMPILE_BYTECODE 预编译 .pyc，配合运行期的 PYTHONDONTWRITEBYTECODE 可免去每次启动的重新编译；
# UV_LINK_MODE=copy 避免依赖跨设备硬链接，保证 venv 内是真实文件、可被 COPY 搬运。
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 先复制依赖声明以复用 Docker 层缓存；项目没有 build-system，因此不安装项目本身。
COPY pyproject.toml uv.lock ./

# cache mount 把 uv 的下载/解包缓存放在构建缓存里而不是镜像层里。这是本次体积优化的关键：
# 原先缓存随层提交，单是 /root/.cache/uv/archive-v0 就有 437MB，而它运行期完全用不到。
# 后面的 find 清理针对依赖自带的测试用例与 C 头文件，运行期不会被导入，合计约 60MB。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project \
    && find /app/.venv -type d \( -name tests -o -name test \) -prune -exec rm -rf {} + \
    && find /app/.venv -type d -name include -prune -exec rm -rf {} +


# 运行阶段：官方 python slim 镜像，不含 uv，比 uv 镜像小约 83MB。
FROM python:3.12-slim-trixie

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOMAIN_UPDATE_DATA_DIR=/app/data \
    PATH="/app/.venv/bin:${PATH}"

# 使用固定 UID 的非 root 用户，避免 Web UI 或持久化配置以 root 身份运行。
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/data \
    && chown appuser:appuser /app/data

# 只搬运构建产物。两个阶段是同一份 CPython 构建，venv 内的 pyvenv.cfg、解释器软链和
# 脚本 shebang 都指向 /usr/local/bin/python3.12 与 /app/.venv，路径一致可直接运行。
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv

COPY --chown=appuser:appuser . .

USER appuser

EXPOSE 8501

# 使用 Python 标准库检查控制台健康端点，避免额外安装 curl。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/healthz', timeout=3).read()"]

CMD ["python", "launcher.py"]
