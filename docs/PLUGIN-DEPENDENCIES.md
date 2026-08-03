# Plugin Python dependencies

A plugin can declare the Python packages it needs, and Trawlarr will
install them when the plugin is installed or updated.

This is **opt-in**. Read [Trusting a plugin's
dependencies](#trusting-a-plugins-dependencies) before turning it on.

## Declaring dependencies

Add a `python_dependencies` list to the plugin's `info.json`:

```json
{
  "id": "my_plugin",
  "name": "My Plugin",
  "version": "1.0.0",
  "python_dependencies": [
    "requests>=2.31",
    "pillow==10.3.0"
  ]
}
```

Each entry must be a plain requirement: a package name, optional
`[extras]`, optional version specifiers. That is the whole grammar
Trawlarr accepts. URLs, local paths, `-e` editable installs,
environment markers and pip options are **rejected**, and rejecting one
fails the plugin install.

A plugin that does not have the key behaves exactly as it always has. No
plugin in the official catalog declares dependencies (measured 2026-08-02
across all 56 published zips — see [What the opt-in
covers](#what-the-opt-in-covers)), so nothing about them changes.

## Enabling installation

```
TRAWLARR_ALLOW_PLUGIN_DEPENDENCY_INSTALL=true
```

With this unset (the default), installing a plugin that declares
dependencies **fails**, with a message naming the packages it asked for.
It does not install the plugin without its dependencies — a plugin whose
imports are going to fail must not look installed.

The same flag governs the older routes a plugin has for starting a
package install — a `requirements.post-install.txt`, or a
`requirements.txt` with `defer_dependency_install` — see [What the
opt-in covers](#what-the-opt-in-covers).

## Where they go

`~/.trawlarr/plugins/<plugin_id>/site-packages`, one directory per
plugin. `PluginExecutor` already puts that directory on `sys.path`
before importing the plugin, so the plugin just imports the package
normally — it must **not** hand-edit `sys.path` itself.

Plugin-scoped rather than the shared venv, for three reasons:

- `~/.trawlarr` is your persistent config volume. The venv is inside
  the container image: it may be read-only, and it is replaced wholesale
  on every image update. Installing into the venv is exactly the problem
  this feature exists to fix.
- It is the directory plugins were already vendoring into by hand, so
  this replaces an untracked hack with a tracked one.
- Two plugins that pin incompatible versions of the same package do not
  fight over one directory.

The trade-offs are real and worth knowing:

- A package used by three plugins is on disk three times.
- `sys.path` is process-wide. If two plugins import different versions
  of the same module name, the first one loaded wins for the life of the
  process. Per-plugin directories bound that problem; they do not
  abolish it.

## Restarts and image updates

Each install writes a receipt at
`site-packages/.trawlarr-plugin-deps.json` recording what was installed
and the `major.minor` of the Python that installed it.

At startup Trawlarr checks every plugin that a library has enabled
against its receipt, and reports loudly — in the log and as a UI
notification — when:

- there is no receipt, or it does not match the currently declared
  requirements (the plugin declared dependencies after it was installed,
  or the site-packages directory did not come across with the volume);
- the receipt was written by a different Python `major.minor` (the image
  moved across a Python version bump, so any compiled extension in there
  will not import).

Trawlarr does **not** re-install automatically. Re-installing at startup
would mean this service contacts a package index and executes what it
finds, unattended, on every boot. Re-installing the plugin from the UI
runs the whole path and is one click.

Until that is done, the plugin is **refused at load time** rather than
imported: `PluginExecutor` logs an error naming the plugin and the
problem, and yields no runner for it. It fails at the point you can see
it, not halfway through a task.

## Trusting a plugin's dependencies

Turning `TRAWLARR_ALLOW_PLUGIN_DEPENDENCY_INSTALL` on widens what
Trawlarr trusts. Be clear about how:

**Before**, installing a plugin ran code from the plugin zip, fetched
from a repository you added. That is already remote code execution as
the container user — see [SECURITY_MODEL.md](SECURITY_MODEL.md) — but
the code came from one place you chose.

**After**, installing a plugin additionally runs `pip install` with
package names taken from that plugin's metadata or its requirements
file — and `npm install` if it ships a `package.json`. pip downloads
from the index the image is configured with and executes package build
and install hooks. So the plugin author picks a *name*; whoever controls
that name on the index picks the *code*. A typosquatted or
newly-compromised package name in a plugin's `info.json` becomes code
running as the Trawlarr user.

What that trust is bounded by:

- The requirement grammar. Plugin metadata cannot supply a URL, a path,
  or a pip option, so it cannot redirect pip at an index you did not
  configure, and cannot point pip at a file inside the container.
  Declared requirements are also passed after `--`, so one can never be
  read as an option.
- The opt-in, which covers every route from a plugin into a package
  manager that Trawlarr will actually take. Leave it off and none of
  them run; the plugin is refused instead. The next section lists them
  exactly — including which combination of files and metadata keys is a
  route and which is inert.
- Failures are terminal, not partial. This holds for every route in the
  table below: if pip or npm cannot be run, exits non-zero, or times out,
  the plugin install fails with the package manager's own error and no
  plugin record is written. A plugin is never recorded as installed with
  its dependencies missing — that would move the failure to first
  execution, in the middle of a file, which is what this whole feature
  exists to prevent.

## What the opt-in covers

Everything that makes a plugin install run a package manager. There are
four routes and they are all behind the one flag:

| What the plugin ships | What it runs | Gated |
| --- | --- | --- |
| `python_dependencies` in `info.json` | `pip install <names>` | yes |
| `requirements.post-install.txt` | `pip install -r` | yes |
| `requirements.txt` **and** `"defer_dependency_install": true` | `pip install -r` | yes |
| `package.json` (same `defer_dependency_install` route) | `npm install`, then `npm run build` if the package.json defines a `build` script | yes |

With the flag off, a plugin that matches any **row of that table** is
**refused at install time**, with a message naming what it wanted. It is
not installed-without-them.

Read the third and fourth rows as written: the trigger is
`defer_dependency_install`, not the file. A plugin that ships a
`requirements.txt` and does *not* set that key — which is 54 of the 56
plugins in the official catalog — is not refused and not gated, because
Trawlarr never reads the file. There is no pip run to prevent.

Two details of the requirements-file routes, so the table is not read as
more than it says:

- A requirements file that names nothing — empty, or nothing but
  comments — is a **no-op**, not a refusal. There is no pip run to gate.
  A file whose only content is a pip option is not "nothing": it is
  refused by the grammar rule below. A requirements file that cannot be
  read is also refused, because what it asks for is then unknown.
- The developer CLI (`--manage_plugins` → "Reload Plugin from Disk")
  runs these same two routes against plugins already on disk, so it is
  behind the same flag. A plugin it cannot install dependencies for is
  reported and **skipped**, leaving its database row as it was; the
  reload continues with the remaining plugins.

The last three were **not** gated until issue #88 was fixed, and that
was a real hole: an operator could read this page, leave the flag
off, and still get a pip install from a plugin they installed. Bringing
them under the flag is a behaviour change. Its cost, measured rather
than assumed — all 56 published zips in the official catalog unpacked on
**2026-08-02**, re-runnable with
`devops/doc_claims.py --survey-plugin-catalog`:

| Measured | Count |
|---|---|
| Plugins in the catalog | 56 |
| Ship a `requirements.post-install.txt` | 0 |
| Set `defer_dependency_install` | 0 |
| Ship a `requirements.txt` | 54 |
| …of those, naming no package at all (empty or comments only) | 38 |
| …of those, naming at least one package | 16 |
| Ship a vendored `site-packages/` | 16 — exactly the 16 above |
| Ship a `package.json` | 1 |
| Declare `python_dependencies` in `info.json` | 0 |

Because none set `defer_dependency_install`, Trawlarr never reads a
catalog plugin's `requirements.txt` in the first place, so **no plugin in
the catalog changes behaviour**. A plugin from elsewhere that does use
one of these routes now needs the flag, and that is the point: it was
always installing packages onto your system, and now you are the one who
decides.

> This is a measurement of somebody else's repository on a given day,
> not an invariant of this one, and no test in this tree can keep it
> true. It carries its date and the command that reproduces it for that
> reason. An earlier version of this paragraph asserted that the 54
> requirements-shipping plugins vendor `site-packages/` into their zip;
> 38 of them do not, they ship an empty file. The conclusion survived the
> measurement, the explanation did not.

On top of the flag, for the pip routes only:

- **No plugin can choose where pip fetches from**, flag on or off.
  Plugin-shipped requirements files are held to the same grammar as
  declared dependencies: a line that is a pip option (`--index-url`,
  `--extra-index-url`, `--find-links`, `-e`, `-r`, …), a URL, a VCS
  reference or a local path is refused, and the refusal fails the whole
  plugin install. Plain `name`/`name==version` lines — what real
  plugins actually ship — are untouched.

There is **no** equivalent for npm, and this page will not pretend
otherwise. A `package.json` names its own registries, can depend on git
URLs and tarballs, and runs lifecycle scripts on install. There is no
useful subset of that to validate, so the flag is the whole of the
protection: leave it off and npm never runs; turn it on and a plugin
with a `package.json` gets to execute what it likes as the Trawlarr
user.

If you want the stronger guarantee that no plugin install may reach the
network for packages at all, leaving the flag off now gives you that for
plugin-driven installs — but a plugin's own code can still make network
calls once it runs. For a hard boundary, run Trawlarr without outbound
access, or install only plugins you have vetted.

What the flag is **not** bounded by, and you should know it: there is no
lockfile, no hash pinning, and no allowlist of packages. If you enable
this, you are trusting the plugin authors you install to name
dependencies as carefully as they write their plugin.

If you would rather not take that on, leave the flag off. Be aware of
what that costs, stated plainly: a plugin that declares or ships
dependencies then cannot be installed **at all**, even if you have baked
those exact packages into a derived image. Trawlarr does not try to
detect that — checking whether a requirement is "already satisfied"
means either importing third-party code as a side effect of an install
check, or reasoning about distribution metadata and version specifiers
well enough to be trusted when it says yes. Getting that wrong in the
permissive direction would let a plugin install and then fail at first
execution, which is the failure this whole feature exists to remove. So
the refusal is deliberately blunt: it tells you the package names, and
you decide.
