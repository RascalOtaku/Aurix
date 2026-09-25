# God's Eye View (https://github.com/bilawalsidhu/gods-eye-view, MIT) - the real 3D-globe OSINT viewer.
#
# Pinned to an exact commit: the project moves fast and its server middleware brokers API keys, so what runs here
# should be a version somebody looked at. Bump GEV_REF deliberately (and re-read SECURITY.md) to upgrade.
#
# This image is deliberately separate from AURIX: no AURIX secrets, its own network, no Linux capabilities.
# NOTE ON DATA: the MIT license covers the code only. Bundled datasets (e.g. TeleGeography submarine cables,
# CC BY-NC-SA) are non-commercial; fine for personal use, remove them before any commercial use.
FROM node:24-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG GEV_REF=0d41b6be5490db1f10a171f238be75db4d4ec3b4
WORKDIR /app
RUN git init -q . \
    && git remote add origin https://github.com/bilawalsidhu/gods-eye-view.git \
    && git fetch -q --depth 1 origin "$GEV_REF" \
    && git checkout -q FETCH_HEAD \
    && rm -rf .git docs/media

# Lockfile install: exact dependency versions from the repo.
RUN npm ci --no-audit --no-fund

RUN chown -R node:node /app
USER node
ENV HOST=0.0.0.0 PORT=4173
EXPOSE 4173
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:4173/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "4173"]
