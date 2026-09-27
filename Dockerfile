# 使用包含 Python 3.12 的 uv 官方固定版本镜像，避免构建依赖多个镜像注册表。
FROM ghcr.io/astral-sh/uv:0.12.19-python3.12-trixie-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    DOMAIN_UPDATE_DATA_DIR=/app/data \
    PATH="/app/.venv/bin:${PATH}"

# 先复制依赖声明以复用 Docker 层缓存；项目没有 build-system，因此不安装项目本身。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 使用固定 UID 的非 root 用户，避免 Web UI 或持久化配置以 root 身份运行。
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/data \
    && chown appuser:appuser /app/data

COPY --chown=appuser:appuser . .

USER appuser

EXPOSE 8501

# 使用 Python 标准库检查 Streamlit 内置健康端点，避免额外安装 curl。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).read()"]

CMD ["python", "launcher.py"]
