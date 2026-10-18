FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first, so code changes do not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY transcription_bot ./transcription_bot
RUN uv sync --locked --no-dev

RUN useradd --uid 1000 --user-group --create-home bot && mkdir /data && chown bot:bot /data

ENV PATH=/app/.venv/bin:$PATH \
    DATA_DIR=/data

USER bot

CMD ["transcription-bot", "run"]
