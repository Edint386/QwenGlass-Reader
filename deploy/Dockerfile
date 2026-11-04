# glassreader 的镜像。构建上下文是仓库根目录（compose.yaml 里的 context: ..）。
# 排版要中文字体：容器里没有 macOS 系统字体，装 fonts-noto-cjk（glassreader/pager.py 的 NOTO_SANS）。
# 另外三个包是后备字体（pager.py 的 FALLBACK_FONTS）：颜文字里的外文字母和符号，中文字体没有。
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk fonts-noto-core fonts-dejavu-core fonts-unifont \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.11.32

WORKDIR /app
# 依赖单独一层，只改代码时不重装。字节码在这里编译好：容器根文件系统只读，运行时写不了 .pyc，每次启动都要现编译
ENV UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY glassreader ./glassreader
RUN uv sync --frozen --no-dev --no-editable

USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8780
CMD ["/app/.venv/bin/glassreader", "serve"]
