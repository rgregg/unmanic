# Trawlarr development

The development environment can be configured in 2 ways:

1. Using Docker

2. As a Pip develop installation


Depending on what you are trying to develop, one way may work better than the other.

Regardless of the method you use, you will need to build the frontend component.

## Package layout and naming

The application is the `trawlarr/` package. `unmanic/` beside it is a
one-file bootstrap that installs the meta path finder in
`trawlarr/namespace_shim.py`; both ship in the wheel.

| | Value |
|---|---|
| Python package | `trawlarr` |
| Distribution / wheel | `trawlarr` (`dist/trawlarr-*`) |
| Console script | `trawlarr` |
| Config directory | `~/.trawlarr/` (`/config/.trawlarr/` in Docker) |
| Database | `~/.trawlarr/config/trawlarr.db` |
| API base path | `/trawlarr/api/v2/` |
| Env var prefix | `TRAWLARR_` |

Write new code against `trawlarr.*`. `unmanic.*` resolves to the same
module objects through the shim, which is what keeps every community
plugin working; it is supported compatibility, not a deprecation. Do not
add new intra-project imports through it — a duplicate import path in
our own code buys nothing and confuses `--cov=trawlarr`.

The console script `unmanic` installed by the wheel is a second entry
point onto the same `main()`. The Docker image's `/usr/bin/unmanic` is a
different thing: a wrapper that prints a notice and execs
`/usr/bin/trawlarr`, and it says it will be removed in a later release.

Environment variables are a clean break: `UNMANIC_*` names are **not**
read and never fall back. If one is set, startup prints a warning naming
the `TRAWLARR_*` variable that replaced it. `RENAMED_ENV_VARS` in
`trawlarr/libs/envvars.py` is the full list (eight entries); the scan
itself is by prefix, so unlisted `UNMANIC_*` names are reported too.

The on-disk and on-the-wire names all come from
`trawlarr/libs/runtimepaths.py`. Change them there, not inline. Both that
module and `envvars.py` must stay free of intra-package imports — they
run before the config and the logger exist.

Some names deliberately still say `unmanic`: the encode cache path
`/tmp/unmanic`, the log file `logs/unmanic.log`, the frontend's
`unmanicGlobals.js` / `$unmanic` / `Unmanic*` components, and every
reference to upstream. See [`FORK.md`](../FORK.md#the-rename-issue-49).



## Dev env

### Option 1: Docker

Docker is by far the simplest way to develop. You can either pull the latest Docker image, or build
the docker image by following the [Docker documentation](../docker/README.md)

Once you have a Docker image, you can run it using the scripts in the `../devops/` directory.

Examples:
```
# Enable VAAPI
devops/run_docker.sh --debug --hw=vaapi --cpus=1

# Enable NVIDIA
devops/run_docker.sh --debug --hw=nvidia --cpus=1

# Standard dev env
devops/run_docker.sh --debug
```

The following folders are generated in the Docker environment:

  - `/dev_environment/config` - Contains the containers mutable config data
  - `/dev_environment/library` - A library in which media files can be placed for testing
  - `/dev_environment/cache` - The temporary location used by ffmpeg for converting file formats

### Option 2: Pip

You can also just install the module natively in your home directory in "develop" mode.

Start by creating a venv.
```
python3 -m venv venv
echo 'export HOME_DIR=$(readlink -e ${VIRTUAL_ENV}/../)/dev_environment/config-venv' >> ./venv/bin/activate
source ./venv/bin/activate
```

Then install the dependencies into that venv
```
python3 -m pip install --upgrade pip
python3 -m pip install --upgrade -r ./requirements.txt -r ./requirements-dev.txt
```

Then install the module:

```
python3 -m pip install --editable .
```

This creates an egg symlink to the project directory for development.

To later uninstall the development symlink:

```
python3 -m pip uninstall trawlarr
```

If the checkout predates the rename you may also have an `unmanic`
distribution installed; uninstall that too, or its `unmanic` package
directory will shadow the alias shim.

You should now be able to run Trawlarr from the commandline:
```
# In develop mode this should return "UNKNOWN"
trawlarr --version
```



## Building the Frontend

The frontend UI lives at `trawlarr/webserver/frontend/`. It is a regular part of
this repository — **not** a submodule. (Upstream keeps it in a separate repo;
this fork absorbed it via `git subtree`, so there is nothing to initialise or
pull separately.)

Run the frontend_install.sh script.

```
devops/frontend_install.sh
```

This will install the NPM modules and build the frontend package. The end result will be located in `trawlarr/webserver/public`

## Profiling and testing

### Profiling (Docker)

Use a clean profile config prefix and enable profiling with `--profiling`.

```
./devops/run_docker.sh --force-recreate --config-prefix=profiling --profiling
```

Wait for the container logs to show Trawlarr is running before opening the UI:

```
./devops/run_docker.sh logs --tail 200
```

The profile output is written to the host path:
`dev_environment/config-profiling/trawlarr-yappi.pstat`

To summarize the results:

```
python - <<'PY'
import pstats
p = pstats.Stats('dev_environment/config-profiling/trawlarr-yappi.pstat')
p.strip_dirs().sort_stats('tottime').print_stats(40)
PY
```

### Profiling (Chrome DevTools)

Open Trawlarr in Chrome at `http://localhost:8888`, then open DevTools:

1. Performance tab: record 10-30 seconds while idle.
2. Network tab: check for repeated polling/websocket traffic.
3. Memory tab: take heap snapshots if you suspect a leak.

### Testing

Run the unit test suite from a host venv — this is what CI runs, and it must
pass on every PR:

```
python3 -m pytest tests/unit/ --cov=trawlarr --cov-report=term-missing
```

Coverage must be measured against `trawlarr`, not the `unmanic` alias —
the alias is a one-file bootstrap and reports on almost nothing.

CI also enforces a coverage floor (see `.github/workflows/test.yml`); coverage
must not regress below it.

The tests under `tests/unit/` are fork-authored and each pins an invariant this
fork relies on. `tests/integration/` is inherited from upstream and is not part
of the CI run.

### Testing the frontend

The frontend has its own suite, run by [Vitest](https://vitest.dev/) with
`@vue/test-utils`. It lives in `trawlarr/webserver/frontend/test/`, configured
by `vitest.config.js`.

```
cd trawlarr/webserver/frontend
npm ci
npm run lint          # eslint
npm test              # vitest
npm run test:coverage # vitest + coverage thresholds
npm run build         # quasar build
```

All four run in CI (`.github/workflows/test.yml`, job `frontend`). `npm run
build` matters as much as the tests: Quasar's webpack build is the only step
that resolves every import in every `.vue` file, so it is what catches a broken
import before the Docker image build does.

Two things about the setup are worth knowing before you add a test:

- **Vitest brings its own Vite pipeline**, while the app is built by Quasar's
  *webpack* CLI. The aliases webpack injects (`src/`, `pages/`, `layouts/`, …)
  are restated in `vitest.config.js`; add an alias to `quasar.conf.js` and you
  must add it there too.
- **`test/storage-polyfill.js` replaces `window.localStorage`.** What jsdom
  provides varies by Node version — on Node 25 it arrives as a bare `{}` with no
  `setItem`, and Quasar's `LocalStorage` plugin then degrades to a silent no-op.
  Without the polyfill a persistence test would really persist on CI and assert
  against nothing locally, passing green in both places.

Coverage thresholds are in `vitest.config.js`. Read `functions` as the honest
figure — v8 counts a module's top-level statements as covered merely for having
been imported, and the router spec imports every page in the app.

### License headers

Every Python file carries a license header — upstream's block on inherited
files, an SPDX identifier on fork-authored ones. CI checks this; run it locally
before pushing:

```
devops/check_license_headers.sh
```

The policy, and the header to use on new files, is in
[CONTRIBUTING.md](CONTRIBUTING.md#license-headers).



## Database upgrades

This project uses Peewee migrations for managing the sqlite database.
`devops/migrations.sh` provides a small wrapper for the cli tool. To get started, run:
```
devops/migrations.sh --help
```



## Builds and releases

Every push to `main` builds the Python wheel and the Docker image and
publishes a development build (`:dev`, `:main-<sha7>`) to GHCR. Versioned
tags come only from publishing a GitHub Release. The workflows, the image
tags, the semver policy and how to trigger a manual rebuild are documented
in [`FORK.md`](../FORK.md#build-pipeline).

Note that the plugin-facing surface — `trawlarr.libs.*` imports, the
`unmanic.*` alias, the `~/.trawlarr` config directory and the
`/trawlarr/api/v2/` paths — counts as public API for versioning purposes.
Breaking third-party plugins is a MAJOR release.
