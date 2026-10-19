FROM python:3.11-slim

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install FFmpeg, Burmese Fonts, libgomp (for faster-whisper/ctranslate2), and required tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-noto-core \
    fonts-noto-cjk \
    fontconfig \
    libgomp1 \
    git \
    curl \
    ca-certificates \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency requirements
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p downloads recap_output

# Run the Telegram Recap Bot as background worker
CMD ["python", "bot.py"]
