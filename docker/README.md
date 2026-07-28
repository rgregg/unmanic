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

### Compose files
The compose files in this directory cover the common runtime configurations
(`-vaapi`, `-nvidia`, `-cifs`, `-ssl`). `docker-compose-test-instance.yml` is the
isolated test instance described in [`FORK.md`](../FORK.md#test-instance).

`docker-compose-caddy.yml` (with `Caddyfile.example`) is the supported
internet-facing deployment: Trawlarr publishes no host ports and Caddy
terminates TLS and authenticates every request in front of it. Trawlarr itself
does not authenticate inbound requests, so port 8888 must never be published to
the internet — see [`docs/SECURITY_MODEL.md`](../docs/SECURITY_MODEL.md).
`docker-compose-ssl.yml` is a TLS *test* fixture, not a deployment template; it
is unauthenticated and binds to loopback.
