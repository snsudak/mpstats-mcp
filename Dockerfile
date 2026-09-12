FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

RUN pip install --no-cache-dir "fastmcp>=2.12,<3" "httpx>=0.27" "uvicorn>=0.30"

COPY server.py .

EXPOSE 8000

CMD ["python", "server.py"]

