#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_dockerfile_runtime_stage.py

    docker/Dockerfile is a multi-stage build (#5): a builder stage that
    holds the toolchain and is discarded, and a runtime stage that ships.
    Two things about that split are silent when they break, so they are
    asserted here rather than left to a reviewer's eye.

    1. THE CALL SITE OF THE SAFETY CHECK.
       Trimming the runtime apt list means a shared object can resolve
       during the build and be absent in the shipped image. The mechanism
       that catches it is docker/verify_runtime_libs.sh, and it only exists
       if the runtime stage actually runs it. Deleting that one RUN line
       would leave a build that still succeeds and an image that dies at an
       operator's first transcode - the exact failure mode
       tests/unit/test_safety_mechanism_call_sites.py was written about.

    2. THE TOOLCHAIN STAYING OUT.
       The whole point of the split is that build-only packages do not
       reach the runtime stage. Adding `build-essential` back to the
       runtime apt list to fix some build error is a one-word edit that
       quietly restores several hundred MB to every pulled layer, and
       nothing else in this repository would notice.

    These are structural assertions over the Dockerfile text. They do not
    prove the image works - only a build and the smoke workflow do that.
    They prove the structure the image's correctness depends on is still
    the structure that is written down.
"""
import os
import re

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOCKERFILE = os.path.join(PROJECT_ROOT, 'docker', 'Dockerfile')
VERIFY_SCRIPT = os.path.join(PROJECT_ROOT, 'docker', 'verify_runtime_libs.sh')
DOCKER_README = os.path.join(PROJECT_ROOT, 'docker', 'README.md')

# Packages that exist only to compile something. Any of these in the
# runtime stage is the regression this file guards against. Matched as
# whole words against the runtime stage's instruction text.
BUILD_ONLY_PACKAGES = (
    'autoconf',
    'automake',
    'build-essential',
    'cmake',
    'g++',
    'gcc',
    'libtool',
    'meson',
    'nasm',
    'ninja-build',
    'python3-dev',
    'yasm',
)


def _read_dockerfile():
    with open(DOCKERFILE, 'r', encoding='utf-8') as fh:
        return fh.read()


def _strip_comments(text):
    """
    Drop comment lines. A package name mentioned in a comment explaining
    why it is *not* installed must not be read as an installation.
    """
    return '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))


def _stages(text):
    """
    Split a Dockerfile into {stage_name: instruction_text}.

    Only `FROM ... AS <name>` stages are returned; a build with an unnamed
    final stage would produce no 'runtime' key and fail the tests below,
    which is the correct outcome - the runtime stage is named on purpose.
    """
    stages = {}
    current = None
    for line in text.splitlines():
        match = re.match(r'^FROM\s+\S+\s+AS\s+(\S+)\s*$', line.strip(), re.IGNORECASE)
        if match:
            current = match.group(1)
            stages[current] = []
            continue
        if line.strip().upper().startswith('FROM '):
            current = None
            continue
        if current is not None:
            stages[current].append(line)
    return {name: '\n'.join(lines) for name, lines in stages.items()}


@pytest.fixture(scope='module')
def dockerfile_stages():
    return _stages(_read_dockerfile())


@pytest.fixture(scope='module')
def runtime_stage(dockerfile_stages):
    assert 'runtime' in dockerfile_stages, (
        "docker/Dockerfile no longer defines a stage named 'runtime'. "
        "The build/smoke pipeline and every assertion in this file are "
        "written against that name.")
    return _strip_comments(dockerfile_stages['runtime'])


class TestTheRuntimeStageRunsTheSharedLibraryCheck:
    """
    The call site, not the mechanism. verify_runtime_libs.sh is tested on
    its own terms elsewhere; what is asserted here is that the image build
    invokes it.
    """

    def test_the_verify_script_exists(self):
        assert os.path.isfile(VERIFY_SCRIPT), (
            "docker/verify_runtime_libs.sh is missing. The runtime stage "
            "runs it; without it the build fails, but silently losing the "
            "check by deleting both is the failure worth naming.")

    def test_the_runtime_stage_copies_the_verify_script(self, runtime_stage):
        assert re.search(r'^COPY\s+/docker/verify_runtime_libs\.sh\s', runtime_stage, re.MULTILINE), (
            "The runtime stage does not COPY docker/verify_runtime_libs.sh.")

    def test_the_runtime_stage_executes_the_verify_script(self, runtime_stage):
        # The script must be *run*, not merely present in the image. A COPY
        # with no RUN ships a check nothing performs.
        assert re.search(r'\bbash\s+/tmp/verify_runtime_libs\.sh\b', runtime_stage), (
            "The runtime stage copies docker/verify_runtime_libs.sh but never "
            "runs it. A shared object missing from the trimmed runtime apt "
            "list would then reach production undetected.")

    def test_the_verify_script_fails_the_build_on_a_missing_library(self):
        # The RUN is chained with `&&`, so this only guards the image if the
        # script exits non-zero. Read that out of the script rather than
        # trusting the shell.
        with open(VERIFY_SCRIPT, 'r', encoding='utf-8') as fh:
            source = fh.read()
        assert 'not found' in source, (
            "verify_runtime_libs.sh no longer looks for ldd's 'not found' marker.")
        assert re.search(r'exit\s+1', source), (
            "verify_runtime_libs.sh has no non-zero exit; a missing library "
            "would be printed and the build would go green anyway.")


class TestTheToolchainStaysOutOfTheRuntimeStage:

    @pytest.mark.parametrize('package', BUILD_ONLY_PACKAGES)
    def test_build_only_package_is_not_installed_at_runtime(self, runtime_stage, package):
        # Word-boundary match on the package name as it would appear on its
        # own line in an apt-get install list.
        pattern = r'^\s*{}\s*\\?\s*$'.format(re.escape(package))
        offending = [
            line for line in runtime_stage.splitlines()
            if re.match(pattern, line)
        ]
        assert not offending, (
            "'{}' is installed in the runtime stage of docker/Dockerfile. "
            "It is a build-time package; it belongs in the builder stage, "
            "which is discarded. See the file header for why.".format(package))

    def test_no_dev_headers_are_installed_at_runtime(self, runtime_stage):
        offending = [
            line.strip().rstrip('\\').strip()
            for line in runtime_stage.splitlines()
            if re.match(r'^\s*lib\S+-dev\s*\\?\s*$', line)
        ]
        assert not offending, (
            "Development headers are installed in the runtime stage: {}. "
            "Nothing compiles against them in the shipped image.".format(offending))


class TestTheRuntimeStageStillShipsWhatTheAppNeeds:
    """
    The other half of the split. Everything the discarded stages produce
    has to be copied forward, and the container contract (entrypoint, CMD,
    port, health probe) has to survive the rewrite.
    """

    def test_the_virtualenv_is_copied_from_the_builder(self, runtime_stage):
        assert re.search(r'^COPY\s+--from=builder\s+/opt/venv\s+/opt/venv\s*$',
                         runtime_stage, re.MULTILINE), (
            "The runtime stage does not copy /opt/venv from the builder stage.")

    def test_the_btbn_ffmpeg_build_is_copied_forward(self, runtime_stage):
        assert re.search(r'^COPY\s+--from=btbn-ffmpeg\s+/usr/lib/btbn-ffmpeg\s+/usr/lib/btbn-ffmpeg\s*$',
                         runtime_stage, re.MULTILINE), (
            "The runtime stage does not copy the BtbN FFmpeg build forward.")

    def test_the_container_local_files_are_installed(self, runtime_stage):
        # docker/root carries entrypoint.sh, the cont-init.d scripts and
        # /usr/bin/trawlarr. Dropping this COPY produces an image whose
        # ENTRYPOINT does not exist.
        assert re.search(r'^COPY\s+/docker/root\s+/\s*$', runtime_stage, re.MULTILINE), (
            "The runtime stage does not COPY /docker/root; entrypoint.sh and "
            "the cont-init.d scripts would be absent.")

    def test_the_entrypoint_and_command_are_unchanged(self, runtime_stage):
        assert 'ENTRYPOINT ["/entrypoint.sh"]' in runtime_stage
        assert 'CMD ["/usr/bin/trawlarr"]' in runtime_stage

    def test_the_port_and_health_probe_survive(self, runtime_stage):
        assert 'EXPOSE 8888/tcp' in runtime_stage
        assert 'HEALTHCHECK' in runtime_stage
        assert '/trawlarr/api/v2/version/read' in runtime_stage

    def test_the_virtualenv_path_is_declared(self, runtime_stage):
        # /usr/bin/trawlarr and entrypoint.sh both read VIRTUAL_ENV and fall
        # back to /opt/venv, so this is belt and braces - but the venv is
        # copied to a hard-coded path and the env var is what makes that
        # path discoverable to anything running `docker exec`.
        assert re.search(r'^ENV\s+VIRTUAL_ENV=/opt/venv\s*$', runtime_stage, re.MULTILINE)

    def test_ffmpeg_is_still_installed(self, runtime_stage):
        assert 'jellyfin-ffmpeg${JELLYFIN_FFMPEG_VERSION}' in runtime_stage
        assert '/usr/local/bin/ffmpeg' in runtime_stage
        assert '/usr/local/bin/ffprobe' in runtime_stage

    @pytest.mark.parametrize('package', [
        'gosu',           # /usr/bin/trawlarr refuses to start without it
        'sqlite3',        # entrypoint.sh sqlite_maintenance()
        'grc',            # /usr/bin/unmanic-tail-logs pipes to grcat
        'nodejs',         # plugin package.json installs run npm at runtime
        'python3',        # the copied venv is a venv, not an interpreter
        'python3-venv',   # entrypoint.sh recreates /opt/venv if it vanishes
        'libmediainfo0v5',
        'libimage-exiftool-perl',
    ])
    def test_runtime_tool_is_installed(self, runtime_stage, package):
        pattern = r'^\s*{}\s*\\?\s*$'.format(re.escape(package))
        assert any(re.match(pattern, line) for line in runtime_stage.splitlines()), (
            "'{}' is not installed in the runtime stage, but something in the "
            "running container uses it.".format(package))


class TestTheDockerReadmeDescribesTheImageThatIsBuilt:
    """
    docker/README.md has a table naming the build stages and telling an
    operator that the runtime stage carries no compiler. That is a
    behavioural claim about the image; pin it to the Dockerfile rather than
    let it drift into a description of a build that no longer exists.
    """

    @staticmethod
    def _documented_stages():
        with open(DOCKER_README, 'r', encoding='utf-8') as fh:
            readme = fh.read()
        # The stage table rows look like: | `builder` | ... |
        section = readme.split('### How the image is built', 1)
        assert len(section) == 2, (
            "docker/README.md no longer has a 'How the image is built' section. "
            "Either restore it or delete this test and its doc_claims.py row - "
            "do not leave a test pinning a document that stopped saying it.")
        table = section[1].split('### ', 1)[0]
        return [
            match.group(1)
            for match in re.finditer(r'^\|\s*`([a-z0-9-]+)`\s*\|', table, re.MULTILINE)
        ]

    def test_the_documented_stage_names_are_the_stages_that_exist(self, dockerfile_stages):
        documented = self._documented_stages()
        assert documented, "No stage rows found in the docker/README.md stage table."
        assert sorted(documented) == sorted(dockerfile_stages), (
            "docker/README.md documents stages {} but docker/Dockerfile defines "
            "{}.".format(sorted(documented), sorted(dockerfile_stages)))

    def test_the_readme_names_the_verify_script_the_build_runs(self, runtime_stage):
        with open(DOCKER_README, 'r', encoding='utf-8') as fh:
            readme = fh.read()
        assert 'docker/verify_runtime_libs.sh' in readme
        assert 'verify_runtime_libs.sh' in runtime_stage, (
            "docker/README.md tells operators the build runs "
            "docker/verify_runtime_libs.sh, and the runtime stage does not.")

    def test_the_directories_the_readme_says_are_walked_are_the_ones_walked(self):
        # The README lists the three trees the check covers. A list that
        # drifts is worse than no list: it tells an operator a directory is
        # protected when nothing looks at it.
        with open(DOCKER_README, 'r', encoding='utf-8') as fh:
            readme = fh.read()
        with open(VERIFY_SCRIPT, 'r', encoding='utf-8') as fh:
            script = fh.read()
        defaults = re.search(r'dirs=\((/[^)]*)\)', script)
        assert defaults, "verify_runtime_libs.sh has no default directory list."
        for directory in defaults.group(1).split():
            assert directory in readme, (
                "verify_runtime_libs.sh walks {} and docker/README.md does not "
                "say so.".format(directory))


class TestTheBuilderStageIsWhereTheToolchainLives:

    def test_the_builder_stage_exists(self, dockerfile_stages):
        assert 'builder' in dockerfile_stages

    def test_the_builder_installs_a_compiler(self, dockerfile_stages):
        builder = _strip_comments(dockerfile_stages['builder'])
        assert re.search(r'^\s*build-essential\s*\\?\s*$', builder, re.MULTILINE), (
            "The builder stage no longer installs a compiler. A requirement "
            "with no prebuilt wheel would fail to build, and the runtime "
            "stage deliberately has no compiler to fall back on.")

    def test_the_builder_installs_requirements_before_the_wheel(self, dockerfile_stages):
        # Layer-cache ordering, and the reason a code-only commit does not
        # reinstall the dependency set.
        builder = dockerfile_stages['builder']
        requirements_at = builder.find('/tmp/requirements.txt')
        wheel_at = builder.find('/src/trawlarr-*.whl')
        assert requirements_at != -1 and wheel_at != -1
        assert requirements_at < wheel_at, (
            "The Trawlarr wheel is installed before requirements.txt. That "
            "invalidates the expensive dependency layer on every commit.")
