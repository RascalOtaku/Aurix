# AURIX mission sandbox: tools baked in at build time (this image has internet only at BUILD
# time; at run time it sits on an internal network with no internet, no LAN, no secrets).
FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils ffmpeg jq git curl ca-certificates tini \
        libgl1 libglu1-mesa libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Debian trixie's own `apt nodejs` package is 20.x - too old for OpenClaw (needs >=24.16.0, confirmed
# live 2026-09-25: "this OpenClaw release requires Node >=24.16.0 <25 || >=26.1.0. detected Node 20.19.2").
# NodeSource's setup script installs a real current major version instead. Non-fatal: if NodeSource is
# unreachable at build time, Node stays absent and the OpenClaw layer below cleanly no-ops too.
RUN (curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
     && apt-get install -y --no-install-recommends nodejs && rm -rf /var/lib/apt/lists/*) \
    || echo "Node.js 24.x not installed (optional; needed only for the OpenClaw layer below)"

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

# OPTIONAL second coding-agent layer: OpenClaw (github.com/openclaw/openclaw), a Node.js personal-assistant
# agent runtime used here ONLY as a task executor - see src/foundation/openclaw.py for why it never gets its
# own authority. Never allowed to fail the build (Node/npm churns; a `latest` global install can break).
RUN npm install -g openclaw@latest --allow-scripts=openclaw \
    || echo "OpenClaw layer not installed (optional): missions with executor=openclaw fall back to the built-in agent"

# OPTIONAL CAD + slicing layer (the fabrication lane: describe a part -> build123d model -> STL -> G-code).
# build123d makes the solid; PrusaSlicer's command line turns it into G-code with the owner's own printer profile
# (an exported .ini in data/fabricate/). Neither ever fails the build: without them the lane stops at the STL and
# says so.
COPY requirements-cad.txt /tmp/requirements-cad.txt
RUN pip freeze > /tmp/constraints-cad.txt \
    && pip install --no-cache-dir -c /tmp/constraints-cad.txt -r /tmp/requirements-cad.txt \
    || echo "CAD layer not installed (optional): the fabrication lane will report it"
# PrusaSlicer adds a few hundred MB (GUI libraries), so it is opt-in: AURIX_SANDBOX_SLICER=1 in .env, then
# `docker compose build sandbox`. Its command line slices headless (no display needed).
ARG WITH_SLICER=0
RUN if [ "$WITH_SLICER" = "1" ]; then \
      (apt-get update && apt-get install -y --no-install-recommends prusa-slicer && rm -rf /var/lib/apt/lists/*) \
      || echo "PrusaSlicer not installed (optional): parts stop at STL"; \
    fi

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
