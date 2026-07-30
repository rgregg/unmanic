#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_plugin_declared_dependencies.py

    Tests for trawlarr/libs/plugin_dependencies.py and its wiring (issue #39).

    A plugin that needs a package the image does not ship used to fail at
    first execution, in the middle of a task, with an ImportError. This
    feature moves that failure to install time and makes it terminal.

    Invariants covered:

    1. A plugin that declares NOTHING - every plugin that exists today -
       takes no new code path: no pip invocation, no directory created, no
       finding, no refused import. This is the false alarm to avoid.
    2. Only plain name/extras/specifier requirements are accepted. A URL, a
       path, an editable install, a pip option or an environment marker is
       refused, and the refusal fails the install rather than being dropped.
    3. Installation is OPT-IN. Without the env var, a plugin that declares
       dependencies is refused and pip is never invoked.
    4. A successful install writes a receipt naming the requirements and the
       running Python major.minor.
    5. pip failing, pip missing and pip timing out each raise, and none of
       them writes a receipt - so nothing later believes the deps are there.
    6. install_plugin propagates the failure, so download_and_install_plugin
       returns False and no plugins-table row is written.
    7. A plugin that declares dependencies AND ships a requirements file is
       refused, because the two mechanisms rebuild the same directory.
    8. check_dependencies detects a missing receipt, a stale receipt and a
       receipt written by a different Python version - and stays silent on a
       matching one.
    9. The startup registration report carries the dependency findings.
    10. PluginExecutor refuses to import a plugin whose declared dependencies
        are not installed, rather than importing it and failing later.
"""
import json
import logging
import os
import subprocess
import sys
from unittest import mock

import pytest
from peewee import SqliteDatabase

from trawlarr.libs import plugin_dependencies, plugin_registration
from trawlarr.libs.plugin_dependencies import PluginDependencyError
from trawlarr.libs.plugins import PluginsHandler
from trawlarr.libs.unmodels import EnabledPlugins, Libraries, LibraryPluginFlow, Plugins
from trawlarr.libs.unmodels.lib import db as db_proxy

MODELS = [Libraries, Plugins, EnabledPlugins, LibraryPluginFlow]

PY_TAG = '{}.{}'.format(sys.version_info[0], sys.version_info[1])


@pytest.fixture
def allow_installs(monkeypatch):
    monkeypatch.setenv(plugin_dependencies.ALLOW_INSTALL_ENV_VAR, 'true')


@pytest.fixture
def deny_installs(monkeypatch):
    monkeypatch.delenv(plugin_dependencies.ALLOW_INSTALL_ENV_VAR, raising=False)


@pytest.fixture
def plugin_dir(tmp_path):
    path = tmp_path / 'plugins' / 'my_plugin'
    path.mkdir(parents=True)
    return str(path)


def write_info(plugin_dir, **info):
    with open(os.path.join(plugin_dir, 'info.json'), 'w') as f:
        json.dump(info, f)
    return info


def fake_pip(returncode=0, stdout='', stderr=''):
    """A stand-in for subprocess.run that records the command it was given."""
    calls = []

    def _run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)

    _run.calls = calls
    return _run


# ---------------------------------------------------------------------------
# 1. A plugin that declares nothing changes nothing
# ---------------------------------------------------------------------------

class TestPluginsDeclaringNothing:

    @pytest.mark.parametrize('info', [
        {},
        {'id': 'my_plugin', 'version': '1.0'},
        {'python_dependencies': []},
        None,
    ])
    def test_no_declaration_reads_as_no_dependencies(self, info):
        assert plugin_dependencies.read_declared_dependencies(info) == []

    def test_no_declaration_never_runs_pip(self, plugin_dir, allow_installs):
        runner = fake_pip()
        with mock.patch('subprocess.run', runner):
            installed = plugin_dependencies.ensure_dependencies_installed(
                'my_plugin', plugin_dir, {'id': 'my_plugin'})
        assert installed == []
        assert runner.calls == []
        # And nothing was created on disk.
        assert not os.path.exists(plugin_dependencies.site_packages_path(plugin_dir))

    def test_no_declaration_produces_no_finding(self, plugin_dir):
        write_info(plugin_dir, id='my_plugin')
        assert plugin_dependencies.check_dependencies('my_plugin', plugin_dir, {'id': 'my_plugin'}) is None
        findings = plugin_dependencies.validate_installed_dependencies(
            os.path.dirname(plugin_dir), ['my_plugin'])
        assert findings == []

    def test_no_declaration_is_checked_even_with_installs_denied(self, plugin_dir, deny_installs):
        """The opt-in must not become a gate on plugins that want nothing."""
        assert plugin_dependencies.ensure_dependencies_installed(
            'my_plugin', plugin_dir, {'id': 'my_plugin'}) == []


# ---------------------------------------------------------------------------
# 2. What may be handed to pip
# ---------------------------------------------------------------------------

class TestRequirementValidation:

    @pytest.mark.parametrize('requirement', [
        'requests',
        'requests>=2.31',
        'pillow==10.3.0',
        'ruamel.yaml',
        'zope-interface',
        'requests[socks]>=2.31,<3',
        'foo!=1.2.*',
        'foo~=1.4',
    ])
    def test_plain_requirements_are_accepted(self, requirement):
        assert plugin_dependencies.validate_requirement(requirement) == requirement

    @pytest.mark.parametrize('requirement', [
        'requests @ https://example.invalid/requests.tar.gz',
        'https://example.invalid/evil.tar.gz',
        'git+https://example.invalid/evil.git',
        './local/path',
        '/etc/passwd',
        '-e .',
        '--index-url=https://example.invalid/simple',
        '--extra-index-url https://example.invalid/simple',
        '-r other-requirements.txt',
        'requests; python_version < "3.9"',
        'requests --no-deps',
        'requests\n--index-url https://example.invalid',
        '',
        '   ',
    ])
    def test_anything_else_is_refused(self, requirement):
        with pytest.raises(PluginDependencyError):
            plugin_dependencies.validate_requirement(requirement)

    @pytest.mark.parametrize('requirement', [None, 3, ['requests'], {'name': 'requests'}])
    def test_non_strings_are_refused(self, requirement):
        with pytest.raises(PluginDependencyError):
            plugin_dependencies.validate_requirement(requirement)

    @pytest.mark.parametrize('declared', ['requests', {'requests': '1'}, 42])
    def test_a_declaration_that_is_not_a_list_is_refused(self, declared):
        with pytest.raises(PluginDependencyError):
            plugin_dependencies.read_declared_dependencies({'python_dependencies': declared})

    def test_one_bad_entry_refuses_the_whole_declaration(self):
        with pytest.raises(PluginDependencyError):
            plugin_dependencies.read_declared_dependencies(
                {'python_dependencies': ['requests', '--index-url=https://example.invalid/simple']})

    def test_refusal_message_quotes_the_requirement(self):
        with pytest.raises(PluginDependencyError) as excinfo:
            plugin_dependencies.validate_requirement('-e .')
        assert '-e .' in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3. Installation is opt-in
# ---------------------------------------------------------------------------

class TestOptIn:

    def test_denied_by_default(self, plugin_dir, deny_installs):
        runner = fake_pip()
        with mock.patch('subprocess.run', runner):
            with pytest.raises(PluginDependencyError) as excinfo:
                plugin_dependencies.ensure_dependencies_installed(
                    'my_plugin', plugin_dir, {'python_dependencies': ['requests>=2.31']})
        assert runner.calls == [], "pip must not run when installs are not permitted"
        message = str(excinfo.value)
        assert 'requests>=2.31' in message, "the operator must be told what the plugin wanted"
        assert plugin_dependencies.ALLOW_INSTALL_ENV_VAR in message

    @pytest.mark.parametrize('value', ['false', '0', 'no', '', 'off'])
    def test_falsey_values_do_not_opt_in(self, plugin_dir, monkeypatch, value):
        monkeypatch.setenv(plugin_dependencies.ALLOW_INSTALL_ENV_VAR, value)
        assert plugin_dependencies.installs_are_permitted() is False

    @pytest.mark.parametrize('value', ['true', 'TRUE', '1', 'yes', 'on'])
    def test_truthy_values_opt_in(self, monkeypatch, value):
        monkeypatch.setenv(plugin_dependencies.ALLOW_INSTALL_ENV_VAR, value)
        assert plugin_dependencies.installs_are_permitted() is True


# ---------------------------------------------------------------------------
# 4/5. The install itself
# ---------------------------------------------------------------------------

class TestInstall:

    def test_success_writes_a_receipt(self, plugin_dir, allow_installs):
        runner = fake_pip()
        with mock.patch('subprocess.run', runner):
            plugin_dependencies.install_dependencies('my_plugin', plugin_dir, ['requests>=2.31'])

        receipt = plugin_dependencies.read_receipt(plugin_dir)
        assert receipt == {'dependencies': ['requests>=2.31'], 'python': PY_TAG}

    def test_install_targets_the_plugins_own_site_packages(self, plugin_dir, allow_installs):
        runner = fake_pip()
        with mock.patch('subprocess.run', runner):
            plugin_dependencies.install_dependencies('my_plugin', plugin_dir, ['requests'])

        command = runner.calls[0][0]
        target = plugin_dependencies.site_packages_path(plugin_dir)
        assert '--target={}'.format(target) in command
        assert os.path.isdir(target)
        # Requirements come after a `--` terminator so one can never be read
        # as a pip option.
        assert command.index('--') < command.index('requests')
        assert command[:3] == [sys.executable, '-m', 'pip']

    def test_install_is_bounded_in_time(self, plugin_dir, allow_installs):
        runner = fake_pip()
        with mock.patch('subprocess.run', runner):
            plugin_dependencies.install_dependencies('my_plugin', plugin_dir, ['requests'])
        assert runner.calls[0][1].get('timeout') == plugin_dependencies.INSTALL_TIMEOUT

    def test_install_rebuilds_the_directory(self, plugin_dir, allow_installs):
        target = plugin_dependencies.site_packages_path(plugin_dir)
        os.makedirs(target)
        stale = os.path.join(target, 'nolongerdeclared.py')
        with open(stale, 'w') as f:
            f.write('# a package the plugin no longer declares\n')

        with mock.patch('subprocess.run', fake_pip()):
            plugin_dependencies.install_dependencies('my_plugin', plugin_dir, ['requests'])

        assert not os.path.exists(stale)

    def test_pip_failure_raises_and_writes_no_receipt(self, plugin_dir, allow_installs):
        runner = fake_pip(returncode=1, stderr='ERROR: No matching distribution found for nosuchpkg')
        with mock.patch('subprocess.run', runner):
            with pytest.raises(PluginDependencyError) as excinfo:
                plugin_dependencies.install_dependencies('my_plugin', plugin_dir, ['nosuchpkg'])

        assert 'No matching distribution' in str(excinfo.value), "pip's own diagnosis must survive"
        assert plugin_dependencies.read_receipt(plugin_dir) is None

    def test_missing_pip_raises(self, plugin_dir, allow_installs):
        def _boom(command, **kwargs):
            raise FileNotFoundError(2, 'No such file or directory')

        with mock.patch('subprocess.run', _boom):
            with pytest.raises(PluginDependencyError):
                plugin_dependencies.install_dependencies('my_plugin', plugin_dir, ['requests'])
        assert plugin_dependencies.read_receipt(plugin_dir) is None

    def test_timeout_raises(self, plugin_dir, allow_installs):
        def _hang(command, **kwargs):
            raise subprocess.TimeoutExpired(command, plugin_dependencies.INSTALL_TIMEOUT)

        with mock.patch('subprocess.run', _hang):
            with pytest.raises(PluginDependencyError) as excinfo:
                plugin_dependencies.install_dependencies('my_plugin', plugin_dir, ['requests'])
        assert 'Timed out' in str(excinfo.value)
        assert plugin_dependencies.read_receipt(plugin_dir) is None


# ---------------------------------------------------------------------------
# 6/7. Wiring into the plugin install pipeline
# ---------------------------------------------------------------------------

def _handler(plugins_path):
    handler = PluginsHandler.__new__(PluginsHandler)
    handler.logger = logging.getLogger('test_plugin_dependencies')
    handler.version = 2
    handler.settings = mock.Mock()
    handler.settings.get_plugins_path.return_value = plugins_path
    return handler


def _plugin_on_disk(plugins_path, plugin_id='my_plugin', **info):
    path = os.path.join(plugins_path, plugin_id)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, 'plugin.py'), 'w') as f:
        f.write("def on_worker_process(data):\n    return data\n")
    payload = {'id': plugin_id, 'version': '1.0'}
    payload.update(info)
    with open(os.path.join(path, 'info.json'), 'w') as f:
        json.dump(payload, f)
    return path


class TestInstallPluginWiring:
    """
    install_plugin is exercised with extraction stubbed out, because what is
    under test is what it does with info.json once the files are in place.
    """

    def _install(self, handler, plugin_id='my_plugin'):
        with mock.patch('zipfile.ZipFile'), \
                mock.patch.object(PluginsHandler, '_assert_zip_members_safe'):
            return handler.install_plugin('/tmp/does-not-matter.zip', plugin_id)

    def test_declared_dependencies_are_installed_at_install_time(self, tmp_path, allow_installs):
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path, python_dependencies=['requests>=2.31'])
        handler = _handler(plugins_path)

        runner = fake_pip()
        with mock.patch('subprocess.run', runner):
            self._install(handler)

        assert len(runner.calls) == 1
        assert 'requests>=2.31' in runner.calls[0][0]

    def test_refused_dependencies_abort_the_install(self, tmp_path, deny_installs):
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path, python_dependencies=['requests>=2.31'])
        handler = _handler(plugins_path)

        runner = fake_pip()
        with mock.patch('subprocess.run', runner), mock.patch('subprocess.call', runner):
            with pytest.raises(PluginDependencyError):
                self._install(handler)
        assert runner.calls == []

    def test_failed_dependency_install_means_the_plugin_is_not_recorded(self, tmp_path, allow_installs):
        """
        6. The whole point: a plugin whose dependencies did not install must
        not end up in the plugins table looking installed.
        """
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path, python_dependencies=['nosuchpkg'])
        handler = _handler(plugins_path)
        handler.download_plugin = mock.Mock(return_value='/tmp/does-not-matter.zip')
        handler.notify_site_of_plugin_install = mock.Mock()

        runner = fake_pip(returncode=1, stderr='ERROR: No matching distribution')
        with mock.patch('subprocess.run', runner), \
                mock.patch('os.path.isfile', return_value=False), \
                mock.patch('zipfile.ZipFile'), \
                mock.patch.object(PluginsHandler, '_assert_zip_members_safe'):
            result = handler.download_and_install_plugin({'plugin_id': 'my_plugin', 'name': 'My Plugin'})

        assert result is False, "a plugin whose dependencies failed must not report a successful install"

    def test_a_plugin_declaring_nothing_installs_without_touching_pip(self, tmp_path, allow_installs):
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path)
        handler = _handler(plugins_path)

        runner = fake_pip()
        with mock.patch('subprocess.run', runner):
            info = self._install(handler)

        assert runner.calls == []
        assert info.get('id') == 'my_plugin'

    def test_declaration_alongside_a_requirements_file_is_refused(self, tmp_path, allow_installs):
        """
        7. Both write the same site-packages and each rebuilds it, so one
        would silently erase the other.
        """
        plugins_path = str(tmp_path / 'plugins')
        path = _plugin_on_disk(plugins_path, python_dependencies=['requests'])
        with open(os.path.join(path, 'requirements.txt'), 'w') as f:
            f.write('pillow\n')
        handler = _handler(plugins_path)

        runner = fake_pip()
        with mock.patch('subprocess.run', runner), mock.patch('subprocess.call', runner):
            with pytest.raises(PluginDependencyError) as excinfo:
                self._install(handler)
        assert 'requirements.txt' in str(excinfo.value)
        assert runner.calls == [], "neither mechanism may run before the conflict is refused"


# ---------------------------------------------------------------------------
# 8. Checking what is on disk
# ---------------------------------------------------------------------------

class TestCheckDependencies:

    def test_matching_receipt_is_silent(self, plugin_dir):
        info = {'python_dependencies': ['requests>=2.31']}
        with mock.patch('subprocess.run', fake_pip()), \
                mock.patch.object(plugin_dependencies, 'installs_are_permitted', return_value=True):
            plugin_dependencies.install_dependencies('my_plugin', plugin_dir, ['requests>=2.31'])
        assert plugin_dependencies.check_dependencies('my_plugin', plugin_dir, info) is None

    def test_missing_receipt_is_reported(self, plugin_dir):
        result = plugin_dependencies.check_dependencies(
            'my_plugin', plugin_dir, {'python_dependencies': ['requests>=2.31']})
        assert result is not None
        assert result[0] == plugin_dependencies.FINDING_DEPENDENCIES_NOT_INSTALLED
        assert 'requests>=2.31' in result[1]

    def test_receipt_for_different_requirements_is_reported(self, plugin_dir):
        with mock.patch('subprocess.run', fake_pip()), \
                mock.patch.object(plugin_dependencies, 'installs_are_permitted', return_value=True):
            plugin_dependencies.install_dependencies('my_plugin', plugin_dir, ['requests>=2.31'])

        result = plugin_dependencies.check_dependencies(
            'my_plugin', plugin_dir, {'python_dependencies': ['requests>=2.31', 'pillow']})
        assert result[0] == plugin_dependencies.FINDING_DEPENDENCIES_STALE

    def test_receipt_from_a_different_python_is_reported(self, plugin_dir):
        target = plugin_dependencies.site_packages_path(plugin_dir)
        os.makedirs(target)
        with open(plugin_dependencies.receipt_path(plugin_dir), 'w') as f:
            json.dump({'dependencies': ['requests>=2.31'], 'python': '2.7'}, f)

        result = plugin_dependencies.check_dependencies(
            'my_plugin', plugin_dir, {'python_dependencies': ['requests>=2.31']})
        assert result[0] == plugin_dependencies.FINDING_DEPENDENCIES_WRONG_PYTHON
        assert '2.7' in result[1] and PY_TAG in result[1]

    def test_requirement_order_does_not_matter(self, plugin_dir):
        target = plugin_dependencies.site_packages_path(plugin_dir)
        os.makedirs(target)
        with open(plugin_dependencies.receipt_path(plugin_dir), 'w') as f:
            json.dump({'dependencies': ['pillow', 'requests'], 'python': PY_TAG}, f)

        assert plugin_dependencies.check_dependencies(
            'my_plugin', plugin_dir, {'python_dependencies': ['requests', 'pillow']}) is None

    def test_unreadable_receipt_reads_as_no_receipt(self, plugin_dir):
        target = plugin_dependencies.site_packages_path(plugin_dir)
        os.makedirs(target)
        with open(plugin_dependencies.receipt_path(plugin_dir), 'w') as f:
            f.write('{ not json')

        result = plugin_dependencies.check_dependencies(
            'my_plugin', plugin_dir, {'python_dependencies': ['requests']})
        assert result[0] == plugin_dependencies.FINDING_DEPENDENCIES_NOT_INSTALLED


# ---------------------------------------------------------------------------
# 9. The startup report
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    test_db = SqliteDatabase(str(tmp_path / 'test.db'), pragmas={'foreign_keys': 0})
    db_proxy.initialize(test_db)
    test_db.connect()
    test_db.create_tables(MODELS)
    yield test_db
    test_db.drop_tables(MODELS)
    test_db.close()
    db_proxy.initialize(None)


def _register_enabled(plugins_path, plugin_id, **info):
    _plugin_on_disk(plugins_path, plugin_id, **info)
    library = Libraries.create(name='TV', path='/library/tv')
    plugin = Plugins.create(
        plugin_id=plugin_id, name=plugin_id, author='someone', version='1.0.0',
        tags='', description='', icon='',
        local_path=os.path.join(plugins_path, plugin_id),
    )
    EnabledPlugins.create(library_id=library, plugin_id=plugin, plugin_name=plugin.name)
    return library, plugin


class TestStartupReport:

    def test_missing_dependencies_reach_the_startup_report(self, db, tmp_path):
        plugins_path = str(tmp_path / 'plugins')
        _register_enabled(plugins_path, 'my_plugin', python_dependencies=['requests>=2.31'])

        report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_path)

        codes = [f.code for f in report.findings]
        assert plugin_dependencies.FINDING_DEPENDENCIES_NOT_INSTALLED in codes
        assert report.errors

    def test_a_plugin_declaring_nothing_keeps_the_report_clean(self, db, tmp_path):
        """The false alarm: today's plugins must not light this up."""
        plugins_path = str(tmp_path / 'plugins')
        _register_enabled(plugins_path, 'my_plugin')

        report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_path)
        assert report.ok, [f.message for f in report.findings]

    def test_satisfied_dependencies_keep_the_report_clean(self, db, tmp_path):
        plugins_path = str(tmp_path / 'plugins')
        _register_enabled(plugins_path, 'my_plugin', python_dependencies=['requests>=2.31'])
        plugin_path = os.path.join(plugins_path, 'my_plugin')
        os.makedirs(plugin_dependencies.site_packages_path(plugin_path))
        with open(plugin_dependencies.receipt_path(plugin_path), 'w') as f:
            json.dump({'dependencies': ['requests>=2.31'], 'python': PY_TAG}, f)

        report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_path)
        assert report.ok, [f.message for f in report.findings]

    def test_a_broken_dependency_check_cannot_take_startup_down(self, db, tmp_path, monkeypatch):
        plugins_path = str(tmp_path / 'plugins')
        _register_enabled(plugins_path, 'my_plugin')
        monkeypatch.setattr(plugin_dependencies, 'validate_installed_dependencies',
                            mock.Mock(side_effect=RuntimeError('boom')))

        report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_path)
        assert report is not None

    def test_an_uninstalled_plugin_is_not_reported_twice(self, db, tmp_path):
        """
        The registration validator already reports a plugin missing from
        disk. The dependency check must stay quiet about it rather than add
        a second line about the same thing.
        """
        plugins_path = str(tmp_path / 'plugins')
        os.makedirs(plugins_path)
        library = Libraries.create(name='TV', path='/library/tv')
        plugin = Plugins.create(
            plugin_id='gone', name='gone', author='x', version='1.0.0',
            tags='', description='', icon='', local_path='/nowhere')
        EnabledPlugins.create(library_id=library, plugin_id=plugin, plugin_name=plugin.name)

        report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_path)
        codes = [f.code for f in report.findings]
        assert codes == [plugin_registration.FINDING_PLUGIN_NOT_INSTALLED]


# ---------------------------------------------------------------------------
# 10. The executor refuses to import
# ---------------------------------------------------------------------------

class TestExecutorRefusesUnsatisfiedPlugins:

    def _executor(self, plugins_path):
        from trawlarr.libs.unplugins import PluginExecutor
        executor = PluginExecutor.__new__(PluginExecutor)
        executor.plugins_directory = plugins_path
        executor.logger = logging.getLogger('test_plugin_dependencies_executor')
        return executor

    def test_unsatisfied_dependencies_block_the_import(self, tmp_path, caplog):
        plugins_path = str(tmp_path / 'plugins')
        path = _plugin_on_disk(plugins_path, 'blocked_plugin', python_dependencies=['requests>=2.31'])
        executor = self._executor(plugins_path)

        with caplog.at_level(logging.ERROR):
            module = executor._PluginExecutor__load_plugin_module('blocked_plugin', path)

        assert module is None, "a plugin with unsatisfied dependencies must not be imported"
        assert 'blocked_plugin' in caplog.text, "and the refusal must be loud"
        assert 'blocked_plugin.plugin' not in sys.modules

    def test_a_plugin_declaring_nothing_still_imports(self, tmp_path):
        plugins_path = str(tmp_path / 'plugins')
        path = _plugin_on_disk(plugins_path, 'plain_plugin')
        executor = self._executor(plugins_path)
        try:
            module = executor._PluginExecutor__load_plugin_module('plain_plugin', path)
            assert module is not None, "a plugin that declares nothing must load exactly as before"
            assert hasattr(module, 'on_worker_process')
        finally:
            for name in ('plain_plugin', 'plain_plugin.plugin'):
                sys.modules.pop(name, None)
            if plugins_path in sys.path:
                sys.path.remove(plugins_path)

    def test_satisfied_dependencies_do_not_block_the_import(self, tmp_path):
        plugins_path = str(tmp_path / 'plugins')
        path = _plugin_on_disk(plugins_path, 'happy_plugin', python_dependencies=['requests>=2.31'])
        os.makedirs(plugin_dependencies.site_packages_path(path))
        with open(plugin_dependencies.receipt_path(path), 'w') as f:
            json.dump({'dependencies': ['requests>=2.31'], 'python': PY_TAG}, f)
        executor = self._executor(plugins_path)
        try:
            module = executor._PluginExecutor__load_plugin_module('happy_plugin', path)
            assert module is not None
        finally:
            for name in ('happy_plugin', 'happy_plugin.plugin'):
                sys.modules.pop(name, None)
            for entry in (plugins_path, plugin_dependencies.site_packages_path(path)):
                if entry in sys.path:
                    sys.path.remove(entry)


# ---------------------------------------------------------------------------
# 10. The OTHER path into pip: a plugin-shipped requirements file
#
# `requirements.txt` / `requirements.post-install.txt` have driven
# `pip install -r` since long before this feature, and that is deliberately
# NOT behind the opt-in - gating it would break every existing plugin that
# ships one. What must not survive is the full pip grammar inside that file:
# an `--index-url` line in a plugin's requirements file redirects pip at a
# host the operator never configured, which is exactly the power
# validate_requirement refuses to take from a plugin's info.json.
#
# The documented boundary is therefore "pip is only ever given package names
# from the configured index", and these tests are what makes that sentence
# true rather than aspirational.
# ---------------------------------------------------------------------------

class TestRequirementsFileBoundary:

    def _write(self, plugin_dir, contents, name='requirements.txt'):
        path = os.path.join(plugin_dir, name)
        with open(path, 'w') as f:
            f.write(contents)
        return path

    @pytest.mark.parametrize('contents', [
        'requests\n',
        'requests>=2.31\npillow==10.3.0\n',
        '# a comment\n\nrequests  # trailing comment\n',
        'requests[socks]>=2.31\n',
        'requests; python_version >= "3.9"\n',
    ])
    def test_plain_requirements_files_are_accepted(self, plugin_dir, contents):
        """Every real plugin ships one of these. None may start failing."""
        path = self._write(plugin_dir, contents)
        assert plugin_dependencies.scan_requirements_file(path) == []
        plugin_dependencies.assert_requirements_file_is_safe('my_plugin', path)

    @pytest.mark.parametrize('line', [
        '--index-url https://evil.example/simple',
        '--extra-index-url https://evil.example/simple',
        '-i https://evil.example/simple',
        '--find-links https://evil.example/wheels',
        '--trusted-host evil.example',
        '-e git+https://evil.example/pkg.git#egg=pkg',
        '-r /etc/passwd',
        'https://evil.example/pkg-1.0-py3-none-any.whl',
        'git+https://evil.example/pkg.git',
        'pkg @ https://evil.example/pkg.tar.gz',
        './local-package',
        '/opt/anything',
    ])
    def test_a_requirements_file_may_not_choose_where_pip_fetches_from(self, plugin_dir, line):
        path = self._write(plugin_dir, 'requests\n{}\n'.format(line))
        refused = plugin_dependencies.scan_requirements_file(path)
        assert [r[0] for r in refused] == [2], "line {!r} was passed through to pip".format(line)
        with pytest.raises(PluginDependencyError) as excinfo:
            plugin_dependencies.assert_requirements_file_is_safe('my_plugin', path)
        assert 'my_plugin' in str(excinfo.value)

    def test_an_option_hidden_after_a_line_continuation_is_still_seen(self, plugin_dir):
        path = self._write(plugin_dir, 'requests \\\n    --index-url https://evil.example/simple\n')
        assert [r[0] for r in plugin_dependencies.scan_requirements_file(path)] == [2]

    def test_installing_a_plain_requirements_file_still_runs_pip(self, tmp_path, deny_installs):
        """
        The opt-in does not gate this path, and must not start gating it:
        existing plugins depend on it.
        """
        plugins_path = str(tmp_path / 'plugins')
        path = _plugin_on_disk(plugins_path, defer_dependency_install=True)
        self._write(path, 'requests\n')
        handler = _handler(plugins_path)

        runner = fake_pip()
        with mock.patch('subprocess.call', runner), mock.patch('subprocess.run', runner):
            with mock.patch('zipfile.ZipFile'), mock.patch.object(PluginsHandler, '_assert_zip_members_safe'):
                handler.install_plugin('/tmp/does-not-matter.zip', 'my_plugin')

        assert len(runner.calls) == 1, "a plain requirements file must install exactly as it always has"
        assert '-r' in runner.calls[0][0]

    @pytest.mark.parametrize('filename', ['requirements.txt', 'requirements.post-install.txt'])
    def test_a_requirements_file_with_an_index_url_aborts_the_install(self, tmp_path, deny_installs, filename):
        """
        The documented guarantee, end to end and with the opt-in OFF: pip is
        never handed a line that chooses an index.
        """
        plugins_path = str(tmp_path / 'plugins')
        path = _plugin_on_disk(plugins_path, defer_dependency_install=True)
        self._write(path, 'requests\n--index-url https://evil.example/simple\n', name=filename)
        handler = _handler(plugins_path)

        runner = fake_pip()
        with mock.patch('subprocess.call', runner), mock.patch('subprocess.run', runner):
            with mock.patch('zipfile.ZipFile'), mock.patch.object(PluginsHandler, '_assert_zip_members_safe'):
                with pytest.raises(PluginDependencyError) as excinfo:
                    handler.install_plugin('/tmp/does-not-matter.zip', 'my_plugin')

        assert runner.calls == [], "pip ran with an attacker-chosen index"
        assert filename in str(excinfo.value)
        assert 'index-url' in str(excinfo.value)

    def test_the_opt_in_being_on_does_not_unlock_the_pip_grammar(self, tmp_path, allow_installs):
        """The opt-in widens WHICH package names may be installed. It is not
        a switch that lets a plugin pick the index."""
        plugins_path = str(tmp_path / 'plugins')
        path = _plugin_on_disk(plugins_path, defer_dependency_install=True)
        self._write(path, '--index-url https://evil.example/simple\nrequests\n')
        handler = _handler(plugins_path)

        runner = fake_pip()
        with mock.patch('subprocess.call', runner), mock.patch('subprocess.run', runner):
            with mock.patch('zipfile.ZipFile'), mock.patch.object(PluginsHandler, '_assert_zip_members_safe'):
                with pytest.raises(PluginDependencyError):
                    handler.install_plugin('/tmp/does-not-matter.zip', 'my_plugin')
        assert runner.calls == []
