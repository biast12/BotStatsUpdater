FROM python:3.12-slim

WORKDIR /app

# Install deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source (config.json is mounted at runtime, not baked in)
COPY main.py logger.py ./

# Unbuffered so logs reach `docker logs` immediately
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
