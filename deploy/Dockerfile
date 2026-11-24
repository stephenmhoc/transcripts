FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN pip install --no-cache-dir uv==0.9.28 && uv sync --frozen --no-dev --no-editable
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --uid 1000 --create-home transcripts
USER 1000:1000
CMD ["transcripts", "serve", "--host", "0.0.0.0", "--port", "8765"]
