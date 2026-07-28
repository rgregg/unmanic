Trawlarr is self-hosted software. It runs on hardware you control, and the project
operates no servers, no accounts, and no analytics. Nobody involved in Trawlarr
receives information about you or your installation.

This document describes what the software does with your data and which network
connections it makes, so you can verify the claim rather than take it on trust.

## What Trawlarr collects about you

Nothing. There is no registration, no telemetry, no usage reporting, no crash
reporting, and no unique installation identifier sent anywhere.

Trawlarr is a fork of [Unmanic](https://github.com/Unmanic/unmanic) with the
phone-home functionality removed. Upstream Unmanic registers each installation
with `api.unmanic.app`, reports plugin installs, and periodically refreshes a
supporter session. Trawlarr does none of these — those calls are stubbed out, and
the internal site URL resolves to an intentionally invalid hostname so that any
missed call fails immediately rather than reaching a third party.

## What is stored, and where

Everything Trawlarr stores stays on the machine you run it on, under your
configuration directory (`/config/.unmanic/` in the Docker image):

 - **Configuration and library settings** — in a local SQLite database.
 - **Installed plugins and their settings** — including any credentials you
   enter into a plugin's configuration.
 - **Task history** — the files processed, their sizes, and the outcome.
 - **Logs** — these record file paths and file names from your library, and are
   more verbose when debugging is enabled.

None of this is transmitted anywhere. It is yours to inspect, back up, or
delete. Removing the configuration directory removes all of it.

## Network connections Trawlarr makes

The software reaches out to the network in only these situations:

 - **Plugin catalogs and downloads.** When you browse, install, or update
   plugins, Trawlarr fetches the catalog and the plugin archives directly from
   GitHub (`raw.githubusercontent.com`). GitHub will see your IP address for
   these requests, as it would for any download. The catalog URL can be pointed
   elsewhere with the `UNMANIC_DEFAULT_PLUGIN_REPO_URL` environment variable.
 - **Plugin icons in the web interface.** Plugin listings reference icon images
   hosted on GitHub, which your browser loads directly when you view the plugin
   pages.
 - **Linked installations.** If you configure links to other Trawlarr or Unmanic
   installations, this instance communicates with the addresses you supplied.
   These are your machines, typically on your own network.
 - **Remote log forwarding — off unless you turn it on.** Trawlarr can forward
   its logs to a log sink you operate. This is disabled by default and only
   activates when you set the `UNMANIC_REMOTE_LOGGING_ENDPOINT` environment
   variable to your own endpoint. Logs are sent only to the address you specify.

There are no other outbound connections. In particular, there is no contact with
`api.unmanic.app` or any Trawlarr-operated service, because no such service
exists.

## Plugins are third-party code

This is the most important caveat on this page.

Plugins run as part of Trawlarr, with the same access to your files and network
that Trawlarr has. A plugin can make its own network connections, and this
privacy policy cannot make promises on its behalf. The default catalog is
maintained by the Unmanic project, not by Trawlarr.

Treat installing a plugin the way you would treat installing any other software:
review what it does if it matters to you.

## Sharing logs when reporting a problem

If you report a bug and choose to attach logs, be aware they contain the file
paths and file names of media in your library. Review them before posting, and
redact anything you would rather not publish. Logs are never uploaded
automatically.

## Changes to this policy

Changes will be recorded in the changelog below, and the effective date updated.
Because Trawlarr is self-hosted, this policy is only ever the one shipped in the
version you are running — you can read it in the source at
`unmanic/webserver/docs/privacy_policy.md`, and check what changed in the
project's git history.

---

<div style="text-align: right">
<b>Effective Date:</b>
27 July, 2026
</div>

---

## Changelog

**27 July, 2026**
 - Rewritten for Trawlarr. The previous version was inherited from upstream
   Unmanic and described data collection — installation registration, plugin
   install reporting, and contact through the Unmanic website — that this fork
   does not perform.
