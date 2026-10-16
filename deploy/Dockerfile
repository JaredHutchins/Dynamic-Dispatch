# syntax=docker/dockerfile:1
# DD in a container, for ARM64 and Intel/AMD64. Build from the repository root;
# see "Docker" in deploy/README.md.
# Node's official images are Debian, multi-architecture, and recent enough for every agent's tool.
FROM node:22-bookworm-slim

# The agents the image includes, separated by spaces: any of claude, codex and gemini.
# Gemini alone is what the image included before agents could be chosen.
ARG DD_AGENTS=gemini
# Marks the container for the doctor and the guide; DD_AGENTS tells the start step and
# the doctor which agents were built in.
ENV DD_CONTAINER=1 PYTHONUNBUFFERED=1 DD_AGENTS="${DD_AGENTS}"
# Named even when the base image has them: deploy/dd-install needs groupadd, useradd
# and usermod (passwd) and visudo (sudo); the start step needs runuser (util-linux).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 git sudo passwd util-linux ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# The image installs the agent tools; deploy/dd-install never does. Each is pinned at the
# version whose JSON output DD reads, so a rebuild can't change what builds stories.
RUN set -eu; packages=; \
    for agent in $DD_AGENTS; do \
        case "$agent" in \
            claude) packages="$packages @anthropic-ai/claude-code@2.1.283" ;; \
            codex) packages="$packages @openai/codex@0.158.0" ;; \
            gemini) packages="$packages @google/gemini-cli@0.59.0" ;; \
            *) echo "DD_AGENTS: unknown agent $agent; choose from claude, codex and gemini" >&2; exit 1 ;; \
        esac; \
    done; \
    [ -n "$packages" ] || { echo 'DD_AGENTS must name at least one agent' >&2; exit 1; }; \
    npm install --global $packages && npm cache clean --force
# Fixed IDs keep volumes' owners stable across rebuilds, whichever agents are chosen.
# The node image already uses 1000.
RUN set -eu; useradd --create-home --uid 1001 --user-group --shell /bin/bash dd; chmod 700 /home/dd; \
    for agent in $DD_AGENTS; do \
        case "$agent" in \
            claude) account=dd-agent id=1003 ;; \
            codex) account=dd-codex id=1004 ;; \
            gemini) account=dd-gemini id=1002 ;; \
        esac; \
        if ! id -u "$account" >/dev/null 2>&1; then \
            groupadd --gid "$id" "$account"; \
            useradd --create-home --uid "$id" --gid "$account" --shell /bin/bash "$account"; \
            chmod 700 "/home/$account"; \
        fi; \
    done

# Only the installer's inputs, never the build folder's config or keys. The start
# step re-runs the installer from DD's checkout on every start.
COPY deploy/dd-install deploy/dd-agent-run deploy/dd-rollback deploy/sudoers-dynamic-dispatch \
    /opt/dd-build/deploy/
RUN touch /opt/dd-build/main.py \
    && cd /opt/dd-build \
    && python3 deploy/dd-install --container --dd-user dd --agents $DD_AGENTS \
    && rm -rf /opt/dd-build \
    && install -d -o dd -g dd -m 700 /home/dd/checkout /home/dd/config /home/dd/.local \
        /home/dd/.local/share /home/dd/.local/share/dynamic-dispatch \
    && install -d -o root -g root -m 700 /etc/dd-credentials
COPY --chmod=755 deploy/dd-container-start /usr/local/bin/dd-container-start
WORKDIR /home/dd/checkout
ENTRYPOINT ["/usr/local/bin/dd-container-start"]
