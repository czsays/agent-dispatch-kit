FROM python:3.11-slim

WORKDIR /app

# Install runtime deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + the commit-safe persona registry.
COPY app/ ./app/
COPY personas.yaml ./personas.yaml

# Secrets are injected at runtime via env (see .env.example), never baked in.
EXPOSE 8000

# Run as a non-root user.
RUN useradd --create-home appuser
USER appuser

CMD ["uvicorn", "app.listener:app", "--host", "0.0.0.0", "--port", "8000"]
