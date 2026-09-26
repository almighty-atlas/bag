FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY apps/api apps/api
RUN uv sync --frozen --no-dev --no-editable
RUN useradd --system --uid 10001 bag
RUN mkdir -p /data/storage && chown bag /data/storage
USER bag
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "bag.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
