# Trawlarr container guide

The production image is `ghcr.io/rgregg/trawlarr`. It runs the
monorepo's Python backend and bundled Vue frontend on port 8888. Some
internal paths and process names intentionally remain `unmanic`; see
[Mounts and names](../README.md#mounts-and-names).

## Run with Docker Compose

Create host directories and a `compose.yaml` beside them:

```bash
mkdir -p trawlarr/config trawlarr/cache
sudo chown -R 1000:1000 trawlarr/config trawlarr/cache
```

```yaml
services:
  trawlarr:
    image: ghcr.io/rgregg/trawlarr:latest
    container_name: trawlarr
    restart: unless-stopped
    ports:
      - "127.0.0.1:8888:8888"
    environment:
      PUID: "1000"
      PGID: "1000"
      TZ: Etc/UTC
    volumes:
      - ./trawlarr/config:/config
      - /path/to/your/media:/library
      - ./trawlarr/cache:/tmp/unmanic
```

```bash
docker compose up -d
docker compose ps
docker compose logs --tail 100 trawlarr
```

Open <http://127.0.0.1:8888/> and follow the
[first-run path](../README.md#quick-start). To allow trusted-LAN
clients, replace `127.0.0.1` with a private host address and restrict
access with the host firewall. Read the root
[network-security guidance](../README.md#network-security) before
changing exposure; it is not repeated here.

The image currently published by CI is `linux/amd64`. The repository
also contains older specialized Compose templates; review their image
and network settings before using them with Trawlarr.

## Mounts and permissions

| Container path | Purpose | Persistence |
| --- | --- | --- |
| `/config` | Settings, SQLite database, plugins, and application state | Required |
| `/library` | Media visible to scanners and workers | Required; manage backups separately |
| `/tmp/unmanic` | In-progress transcodes and worker cache | Recommended; not part of the config backup |

The container starts as root so its initialization scripts can align
the `ubuntu` runtime user with `PUID`/`PGID`, prepare `/config` and
`/tmp/unmanic`, and then launch Trawlarr as that identity. Set the IDs
to a host user/group with the access your plugin flow needs:

```bash
id
sudo chown -R 1000:1000 trawlarr/config trawlarr/cache
```

If Trawlarr must replace media, that identity needs read/write access
to the media directories and execute access on every parent
directory. A read-only library mount is suitable only for workflows
that never replace or write beside source files. Paths selected in
the UI are container paths such as `/library/movies`, never host paths.

For VA-API, pass `/dev/dri` to the service. The startup scripts add the
runtime user to the device groups they find. NVIDIA acceleration also
requires a working NVIDIA Container Toolkit on the host. Establish a
software-encoding first run before adding hardware-specific settings.

## Backups

Stop the service so SQLite and plugin state form a consistent backup,
then archive the bind-mounted configuration:

```bash
docker compose stop trawlarr
tar --create --gzip --file "trawlarr-config-$(date +%Y%m%d-%H%M%S).tar.gz" \
  trawlarr/config
docker compose start trawlarr
```

Store the archive away from the Docker host. Back up the media library
according to its own storage policy. The cache can be omitted while
Trawlarr is stopped. If using a named volume instead of the bind mount
above, use your volume-management platform's stopped-volume backup
procedure.

## Image tags

CI publishes:

- `latest`: rolling `main`; convenient, but it changes on later pulls;
- `main-<sha7>`: commit-specific and preferred for an immutable pin;
- `<application-version>`: release/version convenience tag.

An image digest is the strongest immutable reference. Avoid unattended
updates from `latest` when rollback and reproducibility matter.

## Upgrades and rollback

1. Note the current `image:` value and resolved image:

   ```bash
   docker compose images
   ```

2. Stop Trawlarr and create the `/config` backup described above.
3. Change `image:` to the desired commit tag or digest, then recreate:

   ```bash
   docker compose pull trawlarr
   docker compose up -d trawlarr
   docker compose ps
   docker compose logs --tail 100 trawlarr
   ```

4. Open the Dashboard, confirm libraries and plugins loaded, and
   process a test file before a broad scan.

To roll back, stop the service and restore the backup made for the
previous image:

```bash
docker compose stop trawlarr
mv trawlarr/config "trawlarr/config.failed-$(date +%Y%m%d-%H%M%S)"
tar --extract --gzip --file trawlarr-config-YYYYMMDD-HHMMSS.tar.gz
```

Change `image:` back to that previous commit tag or digest, then run
the same `pull` and `up -d` commands. Keep the failed-upgrade config
until diagnosis is complete. No downgrade compatibility is assumed;
restore the matching backup rather than starting an older image
against newer state.

## Troubleshooting

- **UI does not open:** run `docker compose ps` and
  `docker compose logs --tail 200 trawlarr`. The image health check
  allows up to two minutes for first-start initialization. Confirm the
  published address is reachable from the browser you are using.
- **Library looks empty:** verify the host source path in
  `docker compose config`, then inspect the container view with
  `docker compose exec trawlarr ls -la /library`. Configure the UI
  with that container path.
- **Permission denied or files are not replaced:** compare `PUID` and
  `PGID` with `id`, inspect host ownership with `ls -ld`, and verify
  write and parent-directory execute permission for the media path.
- **Tasks never run:** enable the library scanner, add and configure
  plugins in its Plugin Flow, set a nonzero worker count, request
  **Rescan library now**, and inspect **Application Logs**.
- **Transcodes fill the disk:** check free space for the host path
  mounted at `/tmp/unmanic`; worker output can be much larger than the
  source while processing.
- **Hardware acceleration fails:** first confirm the device is present
  in the container and software processing works. Then validate the
  host driver/toolkit and the plugin's hardware-specific configuration.

## Internet access with TLS and authentication

Before using this example, read and follow the root
[network-security requirements](../README.md#network-security). The
[`docker-compose-reverse-proxy.yml`](docker-compose-reverse-proxy.yml)
example implements that pattern with Caddy.

Prerequisites:

- a public DNS name whose A/AAAA record points to the Docker host;
- inbound TCP ports 80 and 443 (and optionally UDP 443) routed to it;
- Docker Compose.

From the repository root, generate a password hash and start the stack:

```bash
export TRAWLARR_HOSTNAME=trawlarr.example.com
export TRAWLARR_PASSWORD_HASH="$(
  docker run --rm -it caddy:2-alpine caddy hash-password
)"
export TRAWLARR_CONFIG_PATH=/path/to/trawlarr/config
export TRAWLARR_LIBRARY_PATH=/path/to/your/library
export TRAWLARR_CACHE_PATH=/path/to/trawlarr/cache
mkdir -p "$TRAWLARR_CONFIG_PATH" "$TRAWLARR_CACHE_PATH"
sudo chown -R 1000:1000 "$TRAWLARR_CONFIG_PATH" "$TRAWLARR_CACHE_PATH"
docker compose -f docker/docker-compose-reverse-proxy.yml up -d
```

Replace the example host paths. The configuration path is the
`/config` directory to stop and archive during backup or rollback;
the cache can be recreated while Trawlarr is stopped. The hash is an
authentication credential; keep it out of source control and set
these variables again when recreating the stack. Caddy obtains and
renews the public certificate automatically. Browse to
`https://$TRAWLARR_HOSTNAME` and sign in as `trawlarr` with the
password used to generate the hash. For private names, use a
client-trusted certificate or a private VPN.

## Build the current monorepo

The Dockerfile installs a prebuilt wheel from `dist/`. `setup.py`
builds the frontend with `npm ci` and `npm run build:publish`, embeds
`dist/spa` as the backend's public assets, and then builds the wheel.
There is no frontend submodule.

From the repository root, use Python 3.10 and Node 22 (the versions
used by CI), install the existing build requirements, and build:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m build --no-isolation --skip-dependency-check --wheel
docker build --pull -f docker/Dockerfile -t trawlarr:local .
```

Run `trawlarr:local` with the same mounts and environment shown above.
The authoritative production sequence and published tags are in
[the build workflow](../.github/workflows/build.yml).
