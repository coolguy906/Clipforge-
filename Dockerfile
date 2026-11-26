FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DENO_INSTALL=/usr/local

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Deno is the yt-dlp-recommended JavaScript runtime for YouTube EJS challenges.
RUN curl -fsSL https://deno.land/install.sh | sh \
    && deno --version

WORKDIR /app
COPY requirements.txt ./requirements.txt
# --pre selects yt-dlp's frequently updated nightly PyPI builds rather than stale stable.
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir --upgrade --pre "yt-dlp[default]" \
    && yt-dlp --version

COPY . .
EXPOSE 8787
CMD ["python", "clipforge.py"]
