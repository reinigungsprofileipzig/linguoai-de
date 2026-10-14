FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/var/lib/linguoai-web \
    HF_HOME=/var/lib/linguoai-web/cache/huggingface \
    XDG_CACHE_HOME=/var/lib/linguoai-web/cache \
    LINGUOAI_WEB_CONFIG=/etc/linguoai-web/server.json \
    LINGUOAI_WEB_STATE=/var/lib/linguoai-web

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 linguoai-web \
    && useradd --uid 10001 --gid 10001 --home-dir /var/lib/linguoai-web linguoai-web \
    && mkdir -p /var/lib/linguoai-web/tmp /var/lib/linguoai-web/cache /etc/linguoai-web \
    && chown -R 10001:10001 /var/lib/linguoai-web

WORKDIR /app
COPY pyproject.toml README.md ./
COPY linguoai ./linguoai
COPY website/web.html ./website/web.html
COPY website/assets ./website/assets
RUN python -m pip install --no-cache-dir ".[web,drive,parakeet]"

USER 10001:10001
EXPOSE 8765
CMD ["python", "-m", "linguoai.web_api", "--host", "0.0.0.0", "--port", "8765"]
