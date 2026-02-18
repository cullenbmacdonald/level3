FROM python:3.14-slim

RUN pip install uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
RUN uv sync --frozen --no-dev
COPY schema.sql schema.sql
COPY static/ static/
COPY run.sh run.sh
RUN chmod +x run.sh

EXPOSE 8000

ENTRYPOINT ["./run.sh"]
