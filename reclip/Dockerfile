# ReClip (https://github.com/averygan/reclip, MIT) - self-hosted video/audio downloader (Flask + yt-dlp).
#
# Pinned to an exact commit, like gods_eye/: bump RECLIP_REF deliberately and re-check the patch below.
#
# Hardening applied at build time (the build FAILS if the upstream code no longer matches):
#   * upstream passes the user's URL to yt-dlp as a bare positional argument, so a "URL" such as
#     `--exec ...` would be read as an option (command execution). Every call now ends options with `--`.
#   * only http(s) URLs are accepted.
# It has no login of its own: compose publishes it on localhost (or your Tailscale IP) only, on its own
# network, with no AURIX secrets and no Linux capabilities.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG RECLIP_REF=1d161d15a4fe93d9b3371377f0a421dc3e965b10
WORKDIR /app
RUN git init -q . \
    && git remote add origin https://github.com/averygan/reclip.git \
    && git fetch -q --depth 1 origin "$RECLIP_REF" \
    && git checkout -q FETCH_HEAD \
    && rm -rf .git

RUN sed -i \
        -e 's/^    cmd.append(url)$/    cmd += ["--", url]/' \
        -e 's/"--no-playlist", "-j", url\]/"--no-playlist", "-j", "--", url]/' \
        -e 's/"--flat-playlist", "-J", url\]/"--flat-playlist", "-J", "--", url]/' \
        -e 's/^\(    \)url = data.get("url", "").strip()$/&\n\1if url and not url.lower().startswith(("http:\/\/", "https:\/\/")):\n\1    return jsonify({"error": "Only http(s) URLs are supported"}), 400/' \
        app.py \
    && test "$(grep -c '"--", url' app.py)" = 3 \
    && test "$(grep -c 'Only http(s) URLs are supported' app.py)" = 3 \
    && python -m py_compile app.py

RUN pip install --no-cache-dir -r requirements.txt gunicorn \
    && useradd -m -u 1000 reclip && mkdir -p /app/downloads && chown -R reclip:reclip /app
USER reclip
ENV PATH=/home/reclip/.local/bin:$PATH
EXPOSE 8899
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8899/',timeout=3)" || exit 1
# The entrypoint refreshes yt-dlp on start (extractors break often); set RECLIP_NO_UPDATE=1 to skip.
ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
CMD ["gunicorn", "-b", "0.0.0.0:8899", "-w", "1", "--threads", "4", "--timeout", "600", "--access-logfile", "-", "app:app"]
