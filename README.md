# Trawlarr

A self-hosted, no-phone-home, no-license-tier media library optimiser.

Trawlarr is built on top of the excellent
[**Unmanic**](https://github.com/Unmanic/unmanic) by
[Josh.5](https://github.com/Josh5) — all credit for the underlying
transcoding pipeline, plugin architecture, scanner, worker pool, and
web UI goes there. Trawlarr is a fork that exercises the freedoms
granted by Unmanic's GPLv3 license to remove the phone-home
dependency on `api.unmanic.app`, drop the supporter-tier feature
gates, and make the project run end-to-end against the local network
and the public GitHub-hosted plugin catalogs.

If you want the upstream experience with official support and the
project's funding model intact, use
[`josh5/unmanic`](https://hub.docker.com/r/josh5/unmanic) — please
[support the upstream project on Patreon](https://www.patreon.com/Unmanic)
or [GitHub Sponsors](https://github.com/sponsors/Josh5) if you find
the underlying tool valuable. Trawlarr exists for self-hosters whose
threat model excludes phoning home; it does not replace upstream and
does not compete with it.

## License

GPL-3.0, same as upstream Unmanic. See [`LICENSE`](LICENSE).

## What's different from upstream

- **No `api.unmanic.app` dependency.** Plugin discovery, plugin
  downloads, registration, token refresh, and "linked installation"
  sync are all removed or stubbed. The app runs entirely against the
  local network and the public GitHub-hosted plugin catalogs.
- **No telemetry.** No registration heartbeat, no plugin-install
  reporting, no "user info" lookups, no remote installation address
  sync.
- **No supporter level gates.** Library count limits,
  linked-installation count limits, and per-plugin `req_lev` setting
  restrictions are removed. Every feature in the codebase is
  available to every installation.
- **A handful of upstream bug/security fixes** that Trawlarr carries
  ahead of upstream: zip-slip protection on plugin install, no
  `shell=True` on plugin commands, postprocessor source-removal
  ordering, scheduler dict-vs-model fix.
- **CI + tests** — build, test, smoke and BtbN-release-watch
  workflows, 113 unit tests pinning the invariants that make this
  fork what it is.

A more detailed audit of what differs from upstream is in
[`FORK.md`](FORK.md).

## Installing

```bash
docker pull ghcr.io/rgregg/trawlarr:latest
docker run -d --name trawlarr --restart unless-stopped \
    -p 8888:8888 \
    -e PUID=1000 -e PGID=1000 -e TZ=America/Los_Angeles \
    -v /your/config/path:/config \
    -v /your/library:/library \
    -v /your/cache:/tmp/unmanic \
    ghcr.io/rgregg/trawlarr:latest
```

The configuration directory layout, the `/library` mount, the cache
mount, the env vars, and the running ports are all identical to
upstream Unmanic — switching between the two images is a one-line
change in your compose file.

The web UI is at `http://<host>:8888/`, same as upstream.

Internal package and config paths still use the `unmanic` name (the
internal Python package is `unmanic.libs.*`, configs live at
`/config/.unmanic/`, the database is `unmanic.db`, etc.) so that the
upstream plugin ecosystem and existing installs migrate cleanly.
"Trawlarr" is the project brand; the underlying engine is Unmanic.

## Documentation

For day-to-day use of the application — adding libraries, plugin
configuration, the worker model, the API — refer to the upstream
documentation at <https://docs.unmanic.app/>. The user-facing
behavior is the same.

For Trawlarr-specific operational topics (CI, build pipeline, the
`trawlarr-test` instance, production cutover) see
[`FORK.md`](FORK.md).

## Contributing

PRs into `main` welcome. The test suite must pass and coverage must
not regress below the floor enforced by `.github/workflows/test.yml`.

If you're carrying a change you think upstream Unmanic would benefit
from, please consider sending the relevant patch upstream too — they
have a single maintainer and we both benefit from a healthier
upstream.
