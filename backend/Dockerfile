FROM registry.cn-hongkong.aliyuncs.com/jac0625/python:3.13-slim-arm64

WORKDIR /app
COPY pyproject.toml uv.lock* ./

# 国内构建：只换下载源、不重新解析。uv.lock 记的是 files.pythonhosted.org 的 URL，
# 而阿里云 PyPI 镜像的路径与它逐字一致（都是 /packages/<hash>/<file>），替换 host
# 就能保持版本不变地走镜像。两条走不通的路：--frozen 直连 pythonhosted 会超时；
# 按镜像重新 lock 会被镜像的同步滞后改掉版本（实测 regex 2026.9.10 → .3）。
ENV UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN sed -i 's|https://files.pythonhosted.org/|https://mirrors.aliyun.com/pypi/|g; s|https://pypi.org/simple|https://mirrors.aliyun.com/pypi/simple|g' uv.lock \
    && pip install --no-cache-dir uv \
    && uv sync --frozen

COPY app ./app
COPY workers ./workers
COPY alembic.ini ./
COPY alembic ./alembic

ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
