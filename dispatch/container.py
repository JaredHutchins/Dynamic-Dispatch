"""Whether DD runs in the supplied container image, which the doctor and self-update both ask."""

import os

# deploy/Dockerfile sets this; the container has no systemd, and its start step does the services' work.
MARKER = 'DD_CONTAINER'


def in_container():
    return os.environ.get(MARKER) == '1'
