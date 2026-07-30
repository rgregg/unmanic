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

A plugin that does not have the key behaves exactly as it always has.
No existing plugin declares dependencies, so nothing about them changes.

## Enabling installation

```
TRAWLARR_ALLOW_PLUGIN_DEPENDENCY_INSTALL=true
```

With this unset (the default), installing a plugin that declares
dependencies **fails**, with a message naming the packages it asked for.
It does not install the plugin without its dependencies — a plugin whose
imports are going to fail must not look installed.

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
package names taken from that plugin's metadata. pip downloads from the
index the image is configured with and executes package build and
install hooks. So the plugin author picks a *name*; whoever controls
that name on the index picks the *code*. A typosquatted or
newly-compromised package name in a plugin's `info.json` becomes code
running as the Trawlarr user.

What that trust is bounded by:

- The requirement grammar. Plugin metadata cannot supply a URL, a path,
  or a pip option, so it cannot redirect pip at an index you did not
  configure, and cannot point pip at a file inside the container.
  Requirements are also passed after `--`, so one can never be read as
  an option.
- The opt-in, for *declared* dependencies. Leave it off and no
  `python_dependencies` list installs anything; the plugin is refused
  instead. See the section below for what the opt-in does **not**
  cover.
- Failures are terminal, not partial. If pip is missing, fails or times
  out, the plugin install fails and no plugin record is written.

## What the opt-in does not cover

Be clear about this, because it is easy to read the flag as a promise
it does not make: **leaving the flag off does not mean pip never runs.**

Long before this feature existed, installing a plugin that ships a
`requirements.txt` (with `defer_dependency_install` set) or a
`requirements.post-install.txt` ran `pip install -r` against that file.
That path is unchanged and is **not** gated by
`TRAWLARR_ALLOW_PLUGIN_DEPENDENCY_INSTALL`. Gating it would break every
existing plugin that ships one, for a risk those plugins have always
carried — and the operator already accepted running that plugin's code
when they installed it.

What the flag off *does* guarantee is narrower and true:

- No plugin's `info.json` can drive a pip install.
- **No plugin, by either route, can choose where pip fetches from.**
  Plugin-shipped requirements files are held to the same grammar as
  declared dependencies: a line that is a pip option (`--index-url`,
  `--extra-index-url`, `--find-links`, `-e`, `-r`, …), a URL, a VCS
  reference or a local path is refused, and the refusal fails the whole
  plugin install. Plain `name`/`name==version` lines — what real
  plugins actually ship — install exactly as before.

So the boundary is not "pip runs or it does not". It is: **pip only ever
installs package names, from the index this installation is configured
with.** The flag decides whether a plugin's *metadata* may add to those
names.

If you want the stronger guarantee that no plugin install may reach the
network for packages at all, that is not something this flag gives you;
run Trawlarr without outbound access to your package index, or install
only plugins you have vetted.

What it is **not** bounded by, and you should know it: there is no
lockfile, no hash pinning, and no allowlist of packages. If you enable
this, you are trusting the plugin authors you install to name
dependencies as carefully as they write their plugin.

If you would rather not take that on, leave the flag off. Be aware of
what that costs, stated plainly: a plugin that declares dependencies
then cannot be installed **at all**, even if you have already baked
those exact packages into a derived image. Trawlarr does not try to
detect that — checking whether a requirement is "already satisfied"
means either importing third-party code as a side effect of an install
check, or reasoning about distribution metadata and version specifiers
well enough to be trusted when it says yes. Getting that wrong in the
permissive direction would let a plugin install and then fail at first
execution, which is the failure this whole feature exists to remove. So
the refusal is deliberately blunt: it tells you the package names, and
you decide.
