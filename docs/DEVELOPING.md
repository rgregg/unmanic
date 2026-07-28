# Trawlarr development

The development environment can be configured in 2 ways:

1. Using Docker

2. As a Pip develop installation


Depending on what you are trying to develop, one way may work better than the other.

Regardless of the method you use, you will need to build the frontend component.

> **Note on naming:** the project brand is Trawlarr, but the Python package,
> the CLI entry point, the config directory (`~/.unmanic/`) and the API base
> path are all still `unmanic`. That is inherited from upstream, not a
> decision — it is being cleaned up, tracked in
> [#49](https://github.com/rgregg/trawlarr/issues/49). Commands and import
> paths below still use `unmanic` until that lands.



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
python3 -m pip uninstall unmanic
```

You should now be able to run unmanic from the commandline:
```
# In develop mode this should return "UNKNOWN"
unmanic --version
```



## Building the Frontend

The frontend UI lives at `unmanic/webserver/frontend/`. It is a regular part of
this repository — **not** a submodule. (Upstream keeps it in a separate repo;
this fork absorbed it via `git subtree`, so there is nothing to initialise or
pull separately.)

Run the frontend_install.sh script.

```
devops/frontend_install.sh
```

This will install the NPM modules and build the frontend package. The end result will be located in `unmanic/webserver/public`

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
`dev_environment/config-profiling/unmanic-yappi.pstat`

To summarize the results:

```
python - <<'PY'
import pstats
p = pstats.Stats('dev_environment/config-profiling/unmanic-yappi.pstat')
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
python3 -m pytest tests/unit/ --cov=unmanic --cov-report=term-missing
```

CI also enforces a coverage floor (see `.github/workflows/test.yml`); coverage
must not regress below it.

The tests under `tests/unit/` are fork-authored and each pins an invariant this
fork relies on. `tests/integration/` is inherited from upstream and is not part
of the CI run.

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

There is no manual release process. Every push to `main` builds the Python
wheel and the Docker image and publishes to GHCR. The workflows, the image
tags, and how to trigger a manual rebuild are documented in
[`FORK.md`](../FORK.md#build-pipeline).
