# ===========================================================================
# pitching-hub — Multi-stage Dockerfile
# Node.js 18 + Python 3.13 in a single container
#
# Build:  docker compose build
# Run:    docker compose up -d
# ===========================================================================

# ── Stage 1: Build Python virtual environment ─────────────────────────────
FROM python:3.13-slim AS python-deps

# Build at the exact path the Node server expects (/app/agents/venv)
RUN python3 -m venv /app/agents/venv

COPY agents/requirements.txt /tmp/requirements.txt
RUN /app/agents/venv/bin/pip install --upgrade pip && \
    /app/agents/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt


# ── Stage 2: Install Node.js production dependencies ─────────────────────
FROM node:18-slim AS node-deps

WORKDIR /deps
COPY package.json package-lock.json ./
RUN npm ci --omit=dev


# ── Stage 3: Production runtime ──────────────────────────────────────────
FROM python:3.13-slim

# Install Node.js 18 from NodeSource + curl for healthcheck
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_18.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python venv (shebangs point to /app/agents/venv/bin/python3) ─────────
COPY --from=python-deps /app/agents/venv ./agents/venv/

# ── Node modules ─────────────────────────────────────────────────────────
COPY --from=node-deps /deps/node_modules ./node_modules/

# ── Application source ───────────────────────────────────────────────────
COPY package.json ./

# Server (all JS modules)
COPY server/ ./server/

# Client (static HTML)
COPY client/ ./client/

# Agents (Python source — no .env, no venv, no __pycache__)
COPY agents/main.py agents/config.py agents/batch_scraper.py ./agents/
COPY agents/requirements.txt ./agents/
COPY agents/tools/ ./agents/tools/

# ── Seed data (copied to /app/data-seed, entrypoint populates /app/data) ─
COPY data/school_registry.json data/users.json ./data-seed/
COPY data/teams/ ./data-seed/teams/

# ── Entrypoint ───────────────────────────────────────────────────────────
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# Pre-create the data mount point
RUN mkdir -p /app/data

EXPOSE 3001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:3001/api/health || exit 1

ENTRYPOINT ["./entrypoint.sh"]
CMD ["node", "server/index.js"]
