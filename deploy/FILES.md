# Deploy files

This page lists every file under `deploy/` and how it reaches the machine DD
runs on. When DD updates itself after a merge, it reads this page from the
commit it is updating to, so a merge that adds a deploy file and lists it
here is handled by that listing. A changed file under `deploy/` that isn't
listed counts as installed (Markdown files count as documentation), and so
does every changed deploy file if DD can't read this page. A test fails if a
file in `deploy/` is missing here, so add each new deploy file to the table
in the same pull request.

The categories:

- **installed**: copied out of the checkout by `deploy/dd-install` (to
  `/usr/local/bin`, `/etc/sudoers.d` or `/etc/systemd/system`). A change
  needs the installer, so on a Pi DD holds the update back and sends the
  steps. In a container the start step runs the installer, so DD restarts
  instead.
- **checkout**: runs straight from DD's checkout, so it updates like
  documentation.
- **image**: only used to build the container image. It takes effect when
  someone rebuilds the image, so DD updates normally (in a container, it
  also sends one alert saying to rebuild).
- **documentation**: only read.

| File | Category |
| --- | --- |
| `deploy/dd-agent-run` | installed |
| `deploy/dd-rollback` | installed |
| `deploy/sudoers-dynamic-dispatch` | installed |
| `deploy/dynamic-dispatch.service` | installed |
| `deploy/dynamic-dispatch-rollback.service` | installed |
| `deploy/dd-install` | checkout |
| `deploy/Dockerfile` | image |
| `deploy/compose.yaml` | image |
| `deploy/dd-container-start` | image |
| `deploy/README.md` | documentation |
| `deploy/PLANE_CLOUD.md` | documentation |
| `deploy/CONTAINER.md` | documentation |
| `deploy/FILES.md` | documentation |
