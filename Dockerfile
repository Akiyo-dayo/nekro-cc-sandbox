# syntax=docker/dockerfile:1

# ============================================
# nekro-cc-sandbox: Claude Code Workspace Agent
# ============================================
#
# Akiyo-dayo fork: every tool that used to float on "latest" is pinned below,
# so rebuilding the same commit gives the same Claude Code / browser stack.
# To upgrade, change the ARG default, rebuild, and publish a new image tag.
#   CLAUDE_CODE_VERSION   Claude Code CLI (npm @anthropic-ai/claude-code)
#   AGENT_BROWSER_VERSION agent-browser CLI
#   PLAYWRIGHT_VERSION    playwright (decides the bundled Chromium build)
#   PNPM_VERSION          pnpm used only to build the frontend
#   OPENSSH_CLIENT_VERSION / RSYNC_VERSION / GIT_VERSION
#                         exact Debian trixie package versions (apt "pkg=version")
# The release workflow reads CLAUDE_CODE_VERSION from this file to name the tag.
#
# Debian package pins: the base image is pinned to the trixie release, and the three
# tools an SSH gateway relies on are pinned to exact versions. Debian only keeps the newest
# version of a package in each suite, so after a Debian security update the old version
# disappears and the build fails on purpose: look up the new version (e.g.
# https://qa.debian.org/madison.php?package=rsync&s=trixie,trixie-security), update the ARG,
# and publish a new image tag.

# ============================================
# Build stage - Frontend
# ============================================
FROM node:22-alpine AS frontend-builder

ARG PNPM_VERSION=10.33.0

WORKDIR /app/frontend
ENV CI=true

# Copy frontend dependency manifests (for better layer caching)
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN corepack enable && corepack prepare pnpm@${PNPM_VERSION} --activate

COPY frontend/ ./
RUN pnpm install --frozen-lockfile && pnpm run build

# ============================================
# Build stage - Dependencies
# ============================================
FROM ghcr.io/astral-sh/uv:0.5 AS uv-bin

FROM python:3.13-slim-trixie AS deps-builder

WORKDIR /app

# Install uv (binary only)
COPY --from=uv-bin /uv /usr/local/bin/uv
ENV UV_SYSTEM_PYTHON=1
ENV UV_LINK_MODE=copy

# Copy dependency manifests and install dependencies (frozen by lockfile)
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project

# Copy source and install with dev dependencies
COPY src/ ./src/
RUN uv sync --no-dev --frozen

# ============================================
# Runtime stage
# ============================================
FROM python:3.13-slim-trixie AS runtime

# Install uv
COPY --from=uv-bin /uv /usr/local/bin/uv
ENV UV_SYSTEM_PYTHON=1
ENV UV_LINK_MODE=copy

# Install base system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    ca-certificates \
    curl \
    gnupg \
    jq \
    procps \
    unzip \
    util-linux \
    wget \
    zip \
    && rm -rf /var/lib/apt/lists/*

# ssh / rsync / git at exact versions (see the note at the top about Debian pins).
# Installed right after the base layer so nothing later pulls in an unpinned copy.
ARG OPENSSH_CLIENT_VERSION=1:10.0p1-7+deb13u4
ARG RSYNC_VERSION=3.5.0+ds1-0+deb13u1
ARG GIT_VERSION=1:2.47.3-0+deb13u1
RUN apt-get update && apt-get install -y --no-install-recommends \
    "openssh-client=${OPENSSH_CLIENT_VERSION}" \
    "rsync=${RSYNC_VERSION}" \
    "git=${GIT_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

# Let the host application drop SSH client config (e.g. a gateway) into the per-workspace
# ~/.claude volume. Debian's /etc/ssh/ssh_config includes ssh_config.d/*.conf; an Include
# whose target does not exist is ignored by ssh, so this is a no-op until the file appears.
RUN grep -q '^Include /etc/ssh/ssh_config.d/\*.conf' /etc/ssh/ssh_config \
    && printf '%s\n' 'Include /home/appuser/.claude/nk-ssh/config' > /etc/ssh/ssh_config.d/50-nekro.conf \
    && chmod 644 /etc/ssh/ssh_config.d/50-nekro.conf

# Install Node.js 22.x (Claude Code >= 2.1.198 declares engines node >= 22)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version | grep -q '^v22\.'

# Install GitHub CLI (gh)
RUN mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Install a pinned Claude Code via npm (avoids bun install.sh which fails on arm64 without package.json).
# The build fails if the installed CLI does not report exactly this version.
ARG CLAUDE_CODE_VERSION=2.1.311
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && installed="$(claude --version | awk '{print $1}')" \
    && echo "claude --version: ${installed}" \
    && test "${installed}" = "${CLAUDE_CODE_VERSION}"

# The CLI must never replace itself at runtime: the image tag is the version.
ENV DISABLE_AUTOUPDATER=1

# Make claude available on PATH for all users (npm global bin is already in PATH,
# but keep the fallback copy in case of non-standard install paths)
RUN if [ ! -x /usr/local/bin/claude ] && [ -x /root/.local/bin/claude ]; then \
        cp /root/.local/bin/claude /usr/local/bin/claude; \
    fi

# Set shared Playwright browsers path (accessible by all users)
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers

# Install pinned agent-browser / playwright globally and pre-cache Playwright Chromium to shared path
ARG AGENT_BROWSER_VERSION=0.23.4
ARG PLAYWRIGHT_VERSION=1.58.2
RUN npm install -g "agent-browser@${AGENT_BROWSER_VERSION}" "playwright@${PLAYWRIGHT_VERSION}" \
    && mkdir -p /opt/playwright-browsers \
    && playwright install chromium \
    && playwright install-deps chromium \
    && chmod -R 755 /opt/playwright-browsers

# Create non-root user
RUN useradd -m -s /bin/bash appuser

# Set working directory
WORKDIR /home/appuser

# Copy Python source
COPY --chown=appuser:appuser pyproject.toml uv.lock ./
COPY --chown=appuser:appuser src/ ./src/

# Install Python dependencies
RUN uv sync --no-dev --frozen

# Copy built frontend
COPY --from=frontend-builder --chown=appuser:appuser /app/frontend/dist ./frontend/dist

# Create workspace directory and pre-create .claude skill directories
# (ensures appuser has write access even if root created .claude during build)
RUN mkdir -p /home/appuser/workspaces \
    && mkdir -p /home/appuser/.claude/skills/dynamic \
    && chown -R appuser:appuser /home/appuser

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Environment variables
ENV PYTHONPATH=/home/appuser
ENV HOST=0.0.0.0
ENV PORT=7021
ENV WORKSPACE_ROOT=/home/appuser/workspaces
ENV SKIP_PERMISSIONS=false
ENV DEBUG=false

# Image metadata (the source label also links the GHCR package to this repository)
LABEL org.opencontainers.image.source="https://github.com/Akiyo-dayo/nekro-cc-sandbox" \
      org.opencontainers.image.description="nekro-cc-sandbox (Akiyo-dayo fork) with a pinned Claude Code CLI" \
      org.opencontainers.image.licenses="AGPL-3.0" \
      io.github.akiyo-dayo.claude-code.version="${CLAUDE_CODE_VERSION}"

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7021/health')" || exit 1

# Expose port
EXPOSE 7021

# Entrypoint
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
