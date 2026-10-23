FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/app
RUN pip install --no-cache-dir uv==0.9.5
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home agent \
    && mkdir -p data && chown -R agent:agent /srv/app
USER agent
CMD [".venv/bin/python", "-m", "app.main"]
