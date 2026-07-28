# Trawlarr Docker Image

Published images live at `ghcr.io/rgregg/trawlarr`. Every push to `main` builds
and publishes automatically — see [`FORK.md`](../FORK.md#build-pipeline) for the
pipeline and the available tags. You only need the steps below to build locally.

### Building the Source
Before building the image, you need to have built the python package. The
distribution is named `trawlarr`, so the build produces `dist/trawlarr-*`.
If you have an older checkout, delete any `dist/unmanic-*` left over from
before the rename ([#49](https://github.com/rgregg/trawlarr/issues/49)) —
the Dockerfile installs `/src/trawlarr-*.whl` by name, but a stale wheel in
`dist/` is still copied into the build context.

First build the frontend (`devops/frontend_install.sh`), then:

```bash
rm -rfv ./build && rm -fv ./dist/unmanic-* ./dist/trawlarr-*
python3 -m build --no-isolation --skip-dependency-check --wheel
python3 -m build --no-isolation --skip-dependency-check --sdist
```

### Building the image
Run this command from the root of the project:
```bash
docker build -f ./docker/Dockerfile -t ghcr.io/rgregg/trawlarr:dev .
```

### Running the image

The container mounts `/config`, `/library` and `/tmp/unmanic` (encode cache)
and listens on 8888, exactly as upstream Unmanic did. Inside `/config`,
Trawlarr uses `/config/.trawlarr/` — an existing `/config/.unmanic/` is
**not** read, and the container refuses to start rather than come up looking
like a fresh install. Environment variables are prefixed `TRAWLARR_`;
`UNMANIC_*` names are ignored with a startup warning. Both are covered in
[`../README.md`](../README.md#upgrading-from-unmanic-or-a-pre-rename-trawlarr).

The image `CMD` is `/usr/bin/trawlarr`. `/usr/bin/unmanic` still exists as a
wrapper that prints a notice and forwards, so a compose file carrying
`command: /usr/bin/unmanic` from an older image keeps working; it will be
removed in a later release.

### Compose files

| File | What it is |
|---|---|
| `docker-compose-vaapi.yml` | Deployment template, VAAPI hardware encoding |
| `docker-compose-nvidia.yml` | Deployment template, NVENC hardware encoding |
| `docker-compose-cifs.yml` | Deployment template, library on a CIFS mount |
| `docker-compose-caddy.yml` | Supported internet-facing deployment (see below) |
| `docker-compose-ssl.yml` | TLS **test fixture**, not a deployment template |
| `docker-compose-test-instance.yml` | The isolated test instance in [`FORK.md`](../FORK.md#test-instance) |
| `docker-compose-test.yml` | Upstream-inherited test environment; not used by CI |

The three deployment templates use `<PLACEHOLDER>` paths and pin
`ghcr.io/rgregg/trawlarr:1` — the major line, which picks up releases but
never a breaking change on its own. See
[`FORK.md`](../FORK.md#image-tags) for the other tags.

`docker-compose-caddy.yml` (with `Caddyfile.example`) is the supported
internet-facing deployment: Trawlarr publishes no host ports and Caddy
terminates TLS and authenticates every request in front of it. Trawlarr itself
does not authenticate inbound requests, so port 8888 must never be published to
the internet — see [`docs/SECURITY_MODEL.md`](../docs/SECURITY_MODEL.md).
`docker-compose-ssl.yml` is a TLS *test* fixture, not a deployment template; it
is unauthenticated and binds to loopback. It runs a locally built
`trawlarr-ssl:test` image — build it with the command in the file's header.
