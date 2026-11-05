FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml requirements.txt README.md ./
COPY src/ src/
RUN pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium
ENV PYTHONUNBUFFERED=1 HOMEFULL_DATA=/data LLM_PROVIDER=fake
VOLUME ["/data"]
CMD ["homefull"]
