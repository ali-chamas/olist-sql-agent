# One image with the app and the database baked in, so the container needs no volume.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
COPY evals ./evals
RUN pip install -e ".[data]"

# Build the read-only database at image build time, verify it, and drop the source workbook.
RUN python scripts/build_db.py --download \
    && python -m sqlagent check-data \
    && rm -f data/*.xlsx

RUN useradd --create-home app && chown -R app /app
USER app

# The platform provides PORT; LLM_API_KEY is set as a secret, never baked in.
ENV LLM_PROVIDER=groq TRUST_PROXY=1 DAILY_TOKEN_BUDGET=150000
EXPOSE 8000
CMD ["python", "-m", "sqlagent", "serve", "--host", "0.0.0.0"]
