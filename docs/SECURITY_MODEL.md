# Network security model

> **Trawlarr does not authenticate inbound requests.** Anyone who can
> reach the web UI or the API has full control of the installation.
> Treat the listening port exactly as you would an unauthenticated
> root shell on the host, and never expose it directly to the
> internet.

This document describes what access control Trawlarr does and does not
provide today, what an unauthenticated request can actually do, and the
supported way to put the UI on the internet if you need to.

*Reporting a vulnerability?* Open an issue on the
[tracker](https://github.com/rgregg/trawlarr/issues), or email the
maintainer if the details shouldn't be public.

## What the application does today

The web server (`trawlarr/libs/uiserver.py`) is a Tornado application
with three route groups: the frontend, the v1/v2 REST API under
`/unmanic/api/`, and the plugin panel/API routes. **None of them are
wrapped in an authentication or authorization layer.** There is no
login handler, no session cookie, no API key, no token check, and no
per-route `@authenticated` decorator anywhere in the request path. The
only per-request header handling is `set_default_headers`, which sets
the JSON content type.

Consequences worth stating explicitly:

- **No accounts, no passwords, no roles.** There is nothing to log in
  to and nothing to lock down from inside the application.
- **No CSRF protection.** Tornado's `xsrf_cookies` is not enabled, so
  a page in a user's browser can issue requests to a Trawlarr instance
  that browser can reach.
- **The API surface is the whole application.** Swagger UI is served
  at `/unmanic/swagger` and documents every endpoint.
- **The default bind address is every interface.** `ui_address`
  defaults to `''` and `ui_port` to `8888` (`trawlarr/config.py`), so
  an unconfigured install listens on `0.0.0.0:8888`. Publishing that
  port from Docker exposes it to whatever your host and network let
  through.

### What an unauthenticated request can do

This is not a UI-only concern; the API is the same surface. Anyone who
can reach the port can, among other things:

- **Run arbitrary code on the host.** Plugins execute inside the
  Trawlarr process, and the upload API
  (`/unmanic/api/v2/upload/plugin`) accepts a plugin zip and installs
  it. Plugin install is remote code execution as the container user,
  by design.
- **Browse the filesystem.** The file browser API lists any directory
  the process can read, not just configured library paths.
- **Read and rewrite configuration** — library paths, cache path,
  worker counts — via the settings API.
- **Read and delete task history and pending tasks**, and queue work
  against any path the process can see.
- **Move and delete media.** That is the application's whole job; a
  caller with API access inherits it.

### What removing the upstream account system did and did not change

Trawlarr removed upstream Unmanic's central account integration: no
registration with `api.unmanic.app`, no supporter-tier login, no
telemetry, no phone-home. `Session.sign_out()` wipes a local DB row,
`init_device_auth_flow()` is a no-op, and the Patreon/GitHub login
URLs return empty strings (`trawlarr/libs/session.py`).

**That was never access control, and removing it did not weaken
any.** Upstream's account existed to establish a supporter *level*
that gated features, and to talk to a remote service. It never
authenticated inbound requests to your web UI — upstream Unmanic is
also unauthenticated on the local port. So:

- The `session` endpoints and the UI's "sign out" menu item are
  vestigial. They do not protect anything, and clicking sign out does
  not lock anyone out of the instance.
- The security posture of a Trawlarr install and an upstream Unmanic
  install is the same on this axis. What changed is that Trawlarr no
  longer makes outbound calls to a central service; see the
  [privacy policy](../trawlarr/webserver/docs/privacy_policy.md).

Do not read "the account system is gone" as "the account system was
removed from in front of the UI". There was nothing in front of the
UI.

### HTTPS support is not authentication

Trawlarr can terminate TLS itself (`ssl_enabled`, `ssl_certfilepath`,
`ssl_keyfilepath`). That encrypts the connection. It does not identify
or restrict the caller. An HTTPS-enabled instance reachable from the
internet is exactly as exposed as an HTTP one.

## Supported deployment: trusted network only

The supported deployment is a **trusted local network**, with the port
reachable only by people you would hand the host's shell to. Practical
forms of that:

- Publish the port on the LAN and rely on your router not forwarding
  it — the default in the README quickstart. Fine for a home network
  you control; note that it does mean every device on that network can
  reach it.
- Bind to loopback only (`-p 127.0.0.1:8888:8888`) and reach it over
  SSH port-forwarding or from the host itself.
- Reach it over a VPN or overlay network (WireGuard, Tailscale) and
  don't publish it on the LAN at all. This is the lowest-effort way to
  get remote access safely, and is a legitimate alternative to
  everything in the next section.

Additionally: run the container as a non-root `PUID`/`PGID`, and mount
only the paths Trawlarr needs. Nothing above changes the fact that
anyone reaching the port controls the installation, but it does bound
what "controls the installation" reaches.

## Exposing it to the internet

**Do not publish the Trawlarr port directly.** If you need remote
access without a VPN, put a reverse proxy in front that terminates TLS
and authenticates *every* request, and keep Trawlarr's own port off
the host entirely so the proxy is the only way in.

A worked example ships in this repository:

- [`docker/docker-compose-caddy.yml`](../docker/docker-compose-caddy.yml)
- [`docker/Caddyfile.example`](../docker/Caddyfile.example)

The shape of it:

- Trawlarr joins an internal Docker network and **publishes no host
  ports**. It is unreachable except through the proxy.
- Caddy publishes 80/443, obtains and renews a certificate
  automatically, and proxies to `trawlarr:8888`.
- A `basic_auth` directive covers the site block — `*`, not a subpath
  — so the API, the websocket, the Swagger UI, the plugin panels and
  the static assets are all behind it. A rule that protects only
  `/unmanic/ui/*` protects nothing, because `/unmanic/api/*` is the
  same power.

Read the compose file's header for the generate-a-password-hash and
DNS steps. The same pattern works with nginx, Traefik or HAProxy;
what matters is that the auth applies to the whole vhost and that the
application port is not separately reachable.

If you want something stronger than a shared password, front it with
an identity-aware proxy (Authelia, Authentik, oauth2-proxy,
Cloudflare Access) in the same position. The requirement is unchanged:
authenticate everything, and leave no path to port 8888 that bypasses
the proxy.

### Things that look like protection but aren't

- **Obscure port / no link to it.** The address space is scanned
  continuously. This buys nothing.
- **Protecting only the UI path.** See above — the API is the
  application.
- **Trawlarr's own TLS with no proxy auth.** Encryption without
  authentication.
- **Signing out in the UI.** Vestigial; see above.
- **`docker-compose-ssl.yml`.** That file is a TLS *test* fixture
  wired to `dev_environment/` with a self-signed certificate. It is
  unauthenticated and binds to loopback deliberately. It is not a
  deployment template.

## First-party authentication

Optional built-in local authentication — a username and password, or
an API token, enforced by the application itself so a reverse proxy
isn't mandatory — is **not implemented**. It is tracked separately in
[#55](https://github.com/rgregg/trawlarr/issues/55).
This document describes only what is true today. Until such a feature
exists and this page says otherwise, assume every request reaching the
port is trusted completely, and place your access control in front of
it.
