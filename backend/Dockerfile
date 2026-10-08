FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml uv.lock* ./

# --no-dev：生产镜像不需要 pytest/ruff/mypy（约 27MB）。测试在宿主机跑。
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev

COPY app ./app
COPY workers ./workers
COPY alembic.ini ./
COPY alembic ./alembic

ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
