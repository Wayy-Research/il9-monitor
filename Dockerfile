FROM python:3.13-slim

# cryptography needs build essentials
RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/')" || exit 1

EXPOSE 8080
ENV PORT=8080

CMD ["uvicorn", "web:app", "--host", "0.0.0.0", "--port", "8080"]
