FROM python:3.10-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=7860

# Install ffmpeg and curl for video processing and health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set up user with UID 1000 per Hugging Face Spaces requirements
RUN useradd -m -u 1000 user

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Ensure app directory and write directories are owned by user 1000
RUN mkdir -p jellyfin_library storyboards archive videos_to_sync && \
    chown -R user:user /app

USER user

EXPOSE 7860

CMD ["python", "stream_proxy.py", "--port", "7860"]
