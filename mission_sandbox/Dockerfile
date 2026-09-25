# AURIX mission sandbox: tools baked in at build time (this image has internet only at BUILD
# time; at run time it sits on an internal network with no internet, no LAN, no secrets).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils ffmpeg jq git curl ca-certificates tini \
        libgl1 libglu1-mesa libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# OPTIONAL coding-agent layer (OpenHands SDK). Never allowed to fail the build, and never allowed to
# change what the mission tools above were built against: every already-installed package is pinned by
# a constraints file, so a dependency conflict makes THIS layer fail (missions fall back to the built-in
# agent) instead of silently upgrading/downgrading numpy, scipy, trimesh... under the CT/ASR tools.
COPY requirements-openhands.txt /tmp/requirements-openhands.txt
RUN pip freeze > /tmp/constraints.txt \
    && pip install --no-cache-dir -c /tmp/constraints.txt -r /tmp/requirements-openhands.txt \
    || echo "OpenHands SDK layer not installed (optional; likely a dependency conflict with the mission tools): missions fall back to the built-in agent"

# Bake a speech model so transcription works offline. Non-fatal if the download fails.
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8', download_root='/models')" \
    || echo "whisper model not baked (transcription will need one added)"

RUN useradd --uid 1000 --create-home --shell /bin/bash sbx && mkdir -p /app/data/workspace && chown -R 1000:1000 /app
COPY server.py /opt/server.py
COPY tools /opt/tools
USER sbx
ENV SANDBOX_WORKROOT=/app/data/workspace SANDBOX_HOME=/tmp/home
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=2)" || exit 1
# tini as PID 1: found live 2026-09-24 that server.py running bare as PID 1 never reaps grandchildren
# orphaned after a timeout killpg() or a backgrounded command inside sandboxed code - they pile up as
# permanent zombies (351 seen after ~4 days, against a pids_limit of 512; this is what was actually
# behind the recurring "BlockingIOError: Resource temporarily unavailable" on fork). tini's whole job is
# reaping orphans reparented to PID 1 - it never touches server.py's own direct Popen children, which
# stay server.py's until it explicitly wait()s on them via communicate(), same as before.
ENTRYPOINT ["tini", "-g", "--"]
CMD ["python", "/opt/server.py"]
