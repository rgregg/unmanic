#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_packaging_metadata.py

    Issue #58 moved the distribution metadata out of setup.cfg and setup.py
    and into pyproject.toml. Four values in that file are consumed by things
    outside Python, and none of them raise when they are wrong -- they fail
    in a multi-arch buildx run, or produce an image whose CMD is a path that
    does not exist:

      * the distribution NAME, because docker/Dockerfile pip-installs
        `/src/trawlarr-*.whl` by glob and .github/workflows/build.yml
        asserts that glob matches exactly one file;
      * the `trawlarr` CONSOLE SCRIPT, because docker/root/usr/bin/trawlarr
        execs `${VIRTUAL_ENV}/bin/trawlarr`, which is the script setuptools
        writes from [project.scripts], and that launcher is the image CMD;
      * the `unmanic` CONSOLE SCRIPT, kept for installs and wrapper scripts
        that predate the rename in #49;
      * the PACKAGE LIST, which has to include the legacy `unmanic`
        namespace alias shim -- if it ships only in a source checkout, every
        installed community plugin that imports `unmanic.libs.*` breaks.

    So this file does not merely check that pyproject.toml parses. It reads
    the Dockerfile, the build workflow and the container launcher, extracts
    the names those files depend on, and compares them against what the
    packaging actually declares. That is the pin: renaming the distribution
    without updating the glob turns this red rather than green.

    The remaining assertions guard against the split-brain configuration
    that #58 removed coming back, and against the versioninfo.py leftovers
    being reintroduced.
"""
import ast
import configparser
import importlib
import os
import re
import subprocess

import pytest

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.8-3.10
    import tomli as tomllib

from trawlarr import metadata

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
PYPROJECT_PATH = os.path.join(PROJECT_ROOT, 'pyproject.toml')
DOCKERFILE_PATH = os.path.join(PROJECT_ROOT, 'docker', 'Dockerfile')
BUILD_WORKFLOW_PATH = os.path.join(PROJECT_ROOT, '.github', 'workflows', 'build.yml')
CONTAINER_LAUNCHER_PATH = os.path.join(PROJECT_ROOT, 'docker', 'root', 'usr', 'bin', 'trawlarr')
SETUP_CFG_PATH = os.path.join(PROJECT_ROOT, 'setup.cfg')

ENTRY_POINT = 'trawlarr.service:main'


def _read(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read()


@pytest.fixture(scope='module')
def pyproject():
    with open(PYPROJECT_PATH, 'rb') as handle:
        return tomllib.load(handle)


class TestDistributionIdentity:
    """pyproject.toml is now the only place these live, so it has to agree
    with trawlarr.metadata, which is what the running application reports."""

    def test_distribution_name_is_trawlarr(self, pyproject):
        assert pyproject['project']['name'] == 'trawlarr'

    def test_distribution_name_matches_application_metadata(self, pyproject):
        # `__name` is a module-level global, so getattr avoids the private
        # name mangling `metadata.__name` would suffer inside a class body.
        assert pyproject['project']['name'] == getattr(metadata, '__name')

    def test_description_matches_application_metadata(self, pyproject):
        assert pyproject['project']['description'] == metadata.__description__

    def test_homepage_matches_application_metadata(self, pyproject):
        assert pyproject['project']['urls']['Homepage'] == metadata.__website__

    def test_author_matches_application_metadata(self, pyproject):
        authors = pyproject['project']['authors']
        assert authors == [{
            'name':  getattr(metadata, '__author'),
            'email': getattr(metadata, '__email'),
        }]

    def test_declared_license_matches_the_shipped_license_file(self, pyproject):
        # GPL-3.0-only is what upstream's setup.py declared and what #58
        # deliberately carried over unchanged; see the PR for why the
        # -only/-or-later question is not settled here.
        assert pyproject['project']['license'] == 'GPL-3.0-only'
        assert pyproject['project']['license-files'] == ['LICENSE']
        assert 'GNU GENERAL PUBLIC LICENSE' in _read(os.path.join(PROJECT_ROOT, 'LICENSE'))
        assert 'Version 3, 29 June 2007' in _read(os.path.join(PROJECT_ROOT, 'LICENSE'))


class TestConsoleScripts:

    def test_both_console_scripts_are_declared(self, pyproject):
        assert pyproject['project']['scripts'] == {
            'trawlarr': ENTRY_POINT,
            'unmanic':  ENTRY_POINT,
        }

    def test_the_entry_point_target_resolves(self):
        module_path, attribute = ENTRY_POINT.split(':')
        module = importlib.import_module(module_path)
        assert callable(getattr(module, attribute))


class TestPackagingCallSites:
    """The consumers outside Python. Each test reads the real file."""

    def test_dockerfile_installs_the_declared_distribution_name(self, pyproject):
        dockerfile = _read(DOCKERFILE_PATH)
        globs = re.findall(r'/src/([A-Za-z0-9_.-]+)-\*\.whl', dockerfile)
        assert globs, 'docker/Dockerfile no longer pip-installs a wheel by glob'
        assert set(globs) == {pyproject['project']['name']}

    def test_build_workflow_guard_uses_the_declared_distribution_name(self, pyproject):
        workflow = _read(BUILD_WORKFLOW_PATH)
        globs = re.findall(r'dist/([A-Za-z0-9_.-]+)-\*\.whl', workflow)
        assert globs, '.github/workflows/build.yml no longer guards the wheel glob'
        assert set(globs) == {pyproject['project']['name']}

    def test_container_launcher_execs_a_declared_console_script(self, pyproject):
        launcher = _read(CONTAINER_LAUNCHER_PATH)
        match = re.search(r'\$\{VIRTUAL_ENV:-/opt/venv\}/bin/([A-Za-z0-9_-]+)', launcher)
        assert match, 'docker/root/usr/bin/trawlarr no longer execs a venv console script'
        assert match.group(1) in pyproject['project']['scripts']

    def test_fork_md_says_where_these_names_are_defined(self, pyproject):
        """FORK.md's rename table tells a reader which file to open for each
        renamed name. Until #58 it sent them to setup.cfg and setup.py.
        Parse the two rows rather than string-match the prose: the cell
        contents are what the claim is."""
        rows = {}
        for line in _read(os.path.join(PROJECT_ROOT, 'FORK.md')).splitlines():
            if not line.startswith('|'):
                continue
            cells = [cell.strip() for cell in line.strip('|').split('|')]
            if len(cells) == 4:
                rows[cells[0]] = cells
        for label, defined_in in (('Distribution / wheel', '`pyproject.toml` `[project] name`'),
                                  ('Console script', '`pyproject.toml` `[project.scripts]`')):
            assert label in rows, 'FORK.md rename table lost its "{}" row'.format(label)
            _, _, trawlarr_name, source = rows[label]
            assert trawlarr_name == '`{}`'.format(pyproject['project']['name'])
            assert source == defined_in

    def test_the_legacy_namespace_shim_is_packaged(self, pyproject):
        find = pyproject['tool']['setuptools']['packages']['find']
        assert 'trawlarr*' in find['include']
        assert 'unmanic*' in find['include']
        assert find['namespaces'] is True
        # The shim must exist to be packaged.
        assert os.path.isfile(os.path.join(PROJECT_ROOT, 'unmanic', '__init__.py'))


class TestBuildConfiguration:

    def test_a_build_backend_is_declared(self, pyproject):
        assert pyproject['build-system']['build-backend'] == 'setuptools.build_meta'
        assert any(req.startswith('setuptools') for req in pyproject['build-system']['requires'])

    def test_version_and_requirements_are_declared_dynamic(self, pyproject):
        assert set(pyproject['project']['dynamic']) == {
            'version', 'dependencies', 'optional-dependencies'}

    def test_dependencies_are_read_from_the_requirements_files(self, pyproject):
        dynamic = pyproject['tool']['setuptools']['dynamic']
        assert dynamic['dependencies'] == {'file': ['requirements.txt']}
        assert dynamic['optional-dependencies']['dev'] == {'file': ['requirements-dev.txt']}
        assert os.path.isfile(os.path.join(PROJECT_ROOT, 'requirements.txt'))
        assert os.path.isfile(os.path.join(PROJECT_ROOT, 'requirements-dev.txt'))

    def test_setup_cfg_declares_no_distribution_metadata(self):
        """Split-brain metadata is what #58 removed. setuptools merges
        setup.cfg over pyproject.toml silently, so a [metadata] section
        reappearing here would override the table above without a word."""
        parser = configparser.ConfigParser()
        parser.read(SETUP_CFG_PATH)
        assert not parser.has_section('metadata')
        assert not parser.has_section('options')


class TestVersioninfoLeftoversStayGone:

    def test_no_test_placeholder_description(self):
        versioninfo = importlib.import_module('versioninfo')
        assert not hasattr(versioninfo, 'DESCRIPTION')

    def test_no_changes_reader_for_a_file_that_does_not_exist(self):
        versioninfo = importlib.import_module('versioninfo')
        assert not hasattr(versioninfo, 'changes')
        assert not os.path.exists(os.path.join(PROJECT_ROOT, 'CHANGES.txt'))


class TestTheDocumentedLicenceHeaderSituation:
    """docs/CONTRIBUTING.md, "Why upstream's files carry an MIT-style
    notice", makes a claim about every file in the tree: that every Python
    file which is not fork-authored carries the MIT-style permission grant,
    with one grandfathered exception named in the header checker. Issue #58
    reported that claim after counting once. This is what keeps it true."""

    # Assembled rather than written out, so that this file -- which is
    # fork-authored and carries an SPDX header -- does not itself count as a
    # file carrying both headers under the assertions below.
    GRANT = 'Permission is hereby ' + 'granted, free of charge'
    SPDX = 'SPDX-License-Identifier'

    def _grandfathered(self):
        """The exceptions, read out of the checker the document names."""
        script = _read(os.path.join(PROJECT_ROOT, 'devops', 'check_license_headers.sh'))
        block = re.search(r'GRANDFATHERED=\((.*?)\)', script, re.S)
        assert block, 'devops/check_license_headers.sh no longer declares GRANDFATHERED'
        return set(re.findall(r'"([^"]+)"', block.group(1)))

    def _tracked_python_files(self):
        listing = subprocess.check_output(
            ['git', 'ls-files', '*.py'], cwd=PROJECT_ROOT).decode('utf-8').splitlines()
        # The vendored frontend subtree is third-party; the checker skips it
        # and so does the document's claim.
        return [path for path in listing if not path.startswith('trawlarr/webserver/frontend/')]

    def test_the_document_still_makes_the_claim(self):
        contributing = _read(os.path.join(PROJECT_ROOT, 'docs', 'CONTRIBUTING.md'))
        assert "Why upstream's files carry an MIT-style notice" in contributing
        assert self.GRANT in contributing
        assert 'devops/check_license_headers.sh' in contributing

    def test_exactly_one_grandfathered_exception(self):
        assert len(self._grandfathered()) == 1

    def test_every_non_fork_python_file_carries_the_mit_style_grant(self):
        grandfathered = self._grandfathered()
        offenders = []
        for path in self._tracked_python_files():
            if path in grandfathered:
                continue
            source = _read(os.path.join(PROJECT_ROOT, path))
            if self.SPDX in source:
                continue
            if self.GRANT not in source:
                offenders.append(path)
        assert offenders == [], (
            'These files are neither fork-authored (SPDX) nor carry upstream\'s '
            'permission grant, which docs/CONTRIBUTING.md says is impossible: '
            '{}'.format(offenders))

    def test_no_file_carries_both_headers(self):
        """A fork-authored file that also carries upstream's grant would be
        relicensing by accretion, which the policy above forbids."""
        both = []
        for path in self._tracked_python_files():
            source = _read(os.path.join(PROJECT_ROOT, path))
            if self.SPDX in source and self.GRANT in source:
                both.append(path)
        assert both == []


class TestSetupPyStillDoesTheTwoThingsLeftToIt:

    def test_setup_py_still_supplies_the_dynamic_version(self):
        """`version` is declared dynamic in pyproject.toml, which means
        setuptools takes it from the setup() call and nowhere else. Delete
        that keyword and the build does not fail -- it ships a wheel
        versioned 0.0.0, which the Dockerfile installs quite happily. So
        read setup.py's AST for the call itself."""
        tree = ast.parse(_read(os.path.join(PROJECT_ROOT, 'setup.py')))
        setup_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == 'setup'
        ]
        assert len(setup_calls) == 1, 'expected exactly one setup() call in setup.py'
        version_kwargs = [kw for kw in setup_calls[0].keywords if kw.arg == 'version']
        assert version_kwargs, 'setup.py no longer passes version= to setup()'
        assert ast.unparse(version_kwargs[0].value) == 'versioninfo.version()'

    def test_setup_py_still_installs_the_frontend_build_command(self):
        """Without the build_py override the wheel installs a webserver
        with no compiled frontend to serve, and nothing raises."""
        source = _read(os.path.join(PROJECT_ROOT, 'setup.py'))
        tree = ast.parse(source)
        setup_call = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == 'setup'
        )
        assert [kw for kw in setup_call.keywords if kw.arg == 'cmdclass'], \
            'setup.py no longer passes cmdclass= to setup()'
        assert "'build_py'" in source and 'BuildFrontendCommand' in source
