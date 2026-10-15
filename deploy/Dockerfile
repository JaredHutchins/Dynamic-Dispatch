# syntax=docker/dockerfile:1
# DD in a container, for ARM64 and Intel/AMD64. Build from the repository root;
# see "Docker" in deploy/README.md.
# Node's official images are Debian, multi-architecture, and recent enough for Gemini CLI.
FROM node:22-bookworm-slim

# Marks the container for the doctor and the guide.
ENV DD_CONTAINER=1 PYTHONUNBUFFERED=1
# Named even when the base image has them: deploy/dd-install needs groupadd, useradd
# and usermod (passwd) and visudo (sudo); the start step needs runuser (util-linux).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 git sudo passwd util-linux ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# The image installs the agent tool; deploy/dd-install never does. 0.59.0 is the
# version whose JSON output DD reads.
RUN npm install --global @google/gemini-cli@0.59.0 && npm cache clean --force
# Fixed IDs keep volumes' owners stable across rebuilds. The node image already uses 1000.
RUN useradd --create-home --uid 1001 --user-group --shell /bin/bash dd \
    && groupadd --gid 1002 dd-gemini \
    && useradd --create-home --uid 1002 --gid dd-gemini --shell /bin/bash dd-gemini \
    && chmod 700 /home/dd /home/dd-gemini

# Only the installer's inputs, never the build folder's config or keys. The start
# step re-runs the installer from DD's checkout on every start.
COPY deploy/dd-install deploy/dd-agent-run deploy/dd-rollback deploy/sudoers-dynamic-dispatch \
    /opt/dd-build/deploy/
RUN touch /opt/dd-build/main.py \
    && cd /opt/dd-build \
    && python3 deploy/dd-install --container --dd-user dd --agents gemini \
    && rm -rf /opt/dd-build \
    && install -d -o dd -g dd -m 700 /home/dd/checkout /home/dd/config /home/dd/.local \
        /home/dd/.local/share /home/dd/.local/share/dynamic-dispatch \
    && install -d -o root -g root -m 700 /etc/dd-credentials
COPY --chmod=755 deploy/dd-container-start /usr/local/bin/dd-container-start
WORKDIR /home/dd/checkout
ENTRYPOINT ["/usr/local/bin/dd-container-start"]
