#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_plugin_requirements_file_gate.py

    Issue #88: a plugin could start a package install with no opt-in and no
    gate, just by shipping a file.

    `requirements.post-install.txt` ran `pip install -r` unconditionally,
    and `requirements.txt` did the same whenever info.json set
    `defer_dependency_install` - which also ran `npm install` on a shipped
    package.json. None of it was behind
    `TRAWLARR_ALLOW_PLUGIN_DEPENDENCY_INSTALL`, so the gate an operator read
    about in docs/PLUGIN-DEPENDENCIES.md was not the gate they had.

    These tests are mostly WIRING tests. The gate helpers themselves are two
    `if` statements; what is worth pinning is that the install path actually
    calls them, on every route in, and that deleting a call makes something
    go red. Each of the tests below fails if its call site is removed:

      * install_plugin_requirements -> assert_requirements_file_install_permitted
      * install_npm_modules         -> assert_npm_install_permitted

    And the invariant that keeps the change honest: a plugin like the 54 in
    the official catalog - ships a requirements.txt, sets no flag, declares
    nothing, vendors its own site-packages - installs with the gate OFF,
    runs no pip, and keeps its vendored packages.

    Gating those routes also made docs/PLUGIN-DEPENDENCIES.md claim
    something about them that was not true: "failures are terminal, not
    partial". Both call sites used `subprocess.call` and discarded the exit
    code, so with the gate ON a pip that exited 1 installed the plugin
    anyway, with its packages absent - the mid-task ImportError this
    milestone exists to remove, arriving by a different door. The last three
    classes here are the rest of that sentence: failures are terminal, a
    requirements file that names nothing is a no-op rather than a refusal,
    and the developer CLI's reload - the other caller of these two functions
    - skips a plugin it cannot satisfy instead of aborting the run.
"""
import json
import logging
import os
import subprocess
from unittest import mock

import pytest

from trawlarr.libs import plugin_dependencies
from trawlarr.libs.plugin_dependencies import PluginDependencyError
from trawlarr.libs.plugins import PluginsHandler

ENV_VAR = plugin_dependencies.ALLOW_INSTALL_ENV_VAR


@pytest.fixture
def allow_installs(monkeypatch):
    monkeypatch.setenv(ENV_VAR, 'true')


@pytest.fixture
def deny_installs(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)


def _handler(plugins_path):
    handler = PluginsHandler.__new__(PluginsHandler)
    handler.logger = logging.getLogger('test_plugin_requirements_gate')
    handler.version = 2
    handler.settings = mock.Mock()
    handler.settings.get_plugins_path.return_value = plugins_path
    return handler


def _plugin_on_disk(plugins_path, plugin_id='my_plugin', files=None, **info):
    """A plugin already extracted into the plugins directory."""
    path = os.path.join(plugins_path, plugin_id)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, 'plugin.py'), 'w') as f:
        f.write("def on_worker_process(data):\n    return data\n")
    payload = {'id': plugin_id, 'version': '1.0'}
    payload.update(info)
    with open(os.path.join(path, 'info.json'), 'w') as f:
        json.dump(payload, f)
    for name, contents in (files or {}).items():
        with open(os.path.join(path, name), 'w') as f:
            f.write(contents)
    return path


def _recorder(returncode=0):
    """Stand-in for subprocess.run that records what it was asked to do."""
    calls = []

    def _run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout='', stderr='')

    _run.calls = calls
    return _run


def _install(handler, plugin_id='my_plugin'):
    """install_plugin with extraction stubbed - the files are already there."""
    with mock.patch('zipfile.ZipFile'), \
            mock.patch.object(PluginsHandler, '_assert_zip_members_safe'):
        return handler.install_plugin('/tmp/does-not-matter.zip', plugin_id)


# ---------------------------------------------------------------------------
# The gate helpers
# ---------------------------------------------------------------------------

class TestGateHelpers:

    def test_requirements_file_is_refused_when_installs_are_denied(self, tmp_path, deny_installs):
        path = tmp_path / 'requirements.post-install.txt'
        path.write_text('# what it wants\nrequests>=2.31\npillow\n')

        with pytest.raises(PluginDependencyError) as excinfo:
            plugin_dependencies.assert_requirements_file_install_permitted('my_plugin', str(path))

        message = str(excinfo.value)
        # The operator has to be able to act on this: which plugin, which
        # file, which packages, which switch.
        assert 'my_plugin' in message
        assert 'requirements.post-install.txt' in message
        assert 'requests>=2.31' in message and 'pillow' in message
        assert ENV_VAR in message

    def test_requirements_file_is_permitted_when_opted_in(self, tmp_path, allow_installs):
        path = tmp_path / 'requirements.txt'
        path.write_text('requests\n')
        assert plugin_dependencies.assert_requirements_file_install_permitted(
            'my_plugin', str(path)) is None

    def test_npm_is_refused_when_installs_are_denied(self, tmp_path, deny_installs):
        path = tmp_path / 'package.json'
        path.write_text('{}')
        with pytest.raises(PluginDependencyError) as excinfo:
            plugin_dependencies.assert_npm_install_permitted('my_plugin', str(path))
        assert 'my_plugin' in str(excinfo.value)
        assert ENV_VAR in str(excinfo.value)

    def test_npm_is_permitted_when_opted_in(self, tmp_path, allow_installs):
        path = tmp_path / 'package.json'
        path.write_text('{}')
        assert plugin_dependencies.assert_npm_install_permitted('my_plugin', str(path)) is None

    def test_the_environment_is_the_only_thing_consulted(self, tmp_path):
        """An explicit environ wins, so the gate is testable and callers can
        pass one - but nothing about the plugin can turn it on."""
        path = tmp_path / 'requirements.txt'
        path.write_text('requests\n')
        plugin_dependencies.assert_requirements_file_install_permitted(
            'my_plugin', str(path), environ={ENV_VAR: 'true'})
        with pytest.raises(PluginDependencyError):
            plugin_dependencies.assert_requirements_file_install_permitted(
                'my_plugin', str(path), environ={})


# ---------------------------------------------------------------------------
# The wiring: install_plugin_requirements
# ---------------------------------------------------------------------------

class TestInstallPluginRequirementsIsGated:

    def test_pip_does_not_run_when_installs_are_denied(self, tmp_path, deny_installs):
        path = _plugin_on_disk(str(tmp_path / 'plugins'),
                               files={'requirements.txt': 'requests\n'})
        runner = _recorder()
        with mock.patch('subprocess.run', runner):
            with pytest.raises(PluginDependencyError):
                PluginsHandler.install_plugin_requirements(path)
        assert runner.calls == [], "pip ran without the operator opting in"

    def test_pip_runs_when_opted_in(self, tmp_path, allow_installs):
        path = _plugin_on_disk(str(tmp_path / 'plugins'),
                               files={'requirements.txt': 'requests\n'})
        runner = _recorder()
        with mock.patch('subprocess.run', runner):
            PluginsHandler.install_plugin_requirements(path)
        assert len(runner.calls) == 1
        assert '-r' in runner.calls[0]

    def test_a_refused_install_does_not_destroy_the_vendored_site_packages(self, tmp_path, deny_installs):
        """
        install_plugin_requirements rebuilds site-packages from empty. If the
        gate were checked after that, refusing would leave the plugin worse
        off than never having tried - its vendored packages deleted and
        nothing installed in their place.
        """
        path = _plugin_on_disk(str(tmp_path / 'plugins'),
                               files={'requirements.txt': 'requests\n'})
        vendored = os.path.join(path, 'site-packages')
        os.makedirs(vendored)
        with open(os.path.join(vendored, 'somedep.py'), 'w') as f:
            f.write('# vendored by the plugin author\n')

        with mock.patch('subprocess.run', _recorder()):
            with pytest.raises(PluginDependencyError):
                PluginsHandler.install_plugin_requirements(path)

        assert os.path.isfile(os.path.join(vendored, 'somedep.py'))

    def test_a_bad_grammar_line_is_reported_before_the_gate(self, tmp_path, deny_installs):
        """
        Both checks refuse this file. The one worth telling the operator
        about is the `--index-url`, not the flag they could flip - flipping
        it would not make this plugin installable.
        """
        path = _plugin_on_disk(
            str(tmp_path / 'plugins'),
            files={'requirements.txt': 'requests\n--index-url https://attacker.invalid/simple\n'})
        with mock.patch('subprocess.run', _recorder()):
            with pytest.raises(PluginDependencyError) as excinfo:
                PluginsHandler.install_plugin_requirements(path)
        message = str(excinfo.value)
        assert 'pip options are not accepted' in message, \
            "the grammar refusal must be reported, not the gate"
        assert ENV_VAR not in message, \
            "this plugin is not installable at any setting; do not send the operator to the flag"


# ---------------------------------------------------------------------------
# The wiring: install_plugin, both routes in
# ---------------------------------------------------------------------------

class TestInstallPluginRoutesAreGated:

    def test_post_install_requirements_do_not_run_pip_when_denied(self, tmp_path, deny_installs):
        """The exact demonstration in issue #88: a plugin that declares
        nothing, shipping only a post-install requirements file."""
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path,
                        files={'requirements.post-install.txt': 'some-package\n'})
        runner = _recorder()
        with mock.patch('subprocess.run', runner):
            with pytest.raises(PluginDependencyError) as excinfo:
                _install(_handler(plugins_path))
        assert runner.calls == []
        assert 'requirements.post-install.txt' in str(excinfo.value)

    def test_post_install_requirements_run_pip_when_opted_in(self, tmp_path, allow_installs):
        plugins_path = str(tmp_path / 'plugins')
        path = _plugin_on_disk(plugins_path,
                               files={'requirements.post-install.txt': 'some-package\n'})
        runner = _recorder()
        with mock.patch('subprocess.run', runner):
            _install(_handler(plugins_path))
        assert len(runner.calls) == 1
        command = runner.calls[0]
        assert os.path.join(path, 'requirements.post-install.txt') in command

    def test_deferred_requirements_do_not_run_pip_when_denied(self, tmp_path, deny_installs):
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path, defer_dependency_install=True,
                        files={'requirements.txt': 'some-package\n'})
        runner = _recorder()
        with mock.patch('subprocess.run', runner):
            with pytest.raises(PluginDependencyError):
                _install(_handler(plugins_path))
        assert runner.calls == []

    def test_npm_does_not_run_when_denied(self, tmp_path, deny_installs):
        """`npm install` executes the package.json's lifecycle scripts, so it
        is at least as much trust as the pip path and gets the same gate."""
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path, defer_dependency_install=True,
                        files={'package.json': '{"name": "x"}'})
        runner = _recorder()
        with mock.patch('subprocess.run', runner):
            with pytest.raises(PluginDependencyError) as excinfo:
                _install(_handler(plugins_path))
        assert runner.calls == [], "npm ran without the operator opting in"
        assert 'package.json' in str(excinfo.value)

    def test_npm_runs_when_opted_in(self, tmp_path, allow_installs):
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path, defer_dependency_install=True,
                        files={'package.json': '{"name": "x", "scripts": {"build": "webpack"}}'})
        runner = _recorder()
        with mock.patch('subprocess.run', runner):
            _install(_handler(plugins_path))
        assert [c[:2] for c in runner.calls] == [['npm', 'install'], ['npm', 'run']]

    def test_npm_build_is_skipped_when_the_package_json_has_no_build_script(self, tmp_path, allow_installs):
        """
        `npm run build` on a package.json without a `build` script exits 1.
        That was invisible while the exit code was discarded; now that a
        failure fails the install, asking for a script the plugin never
        defined would refuse every plugin that ships a package.json with no
        build step.
        """
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path, defer_dependency_install=True,
                        files={'package.json': '{"name": "x"}'})
        runner = _recorder()
        with mock.patch('subprocess.run', runner):
            _install(_handler(plugins_path))
        assert [c[:2] for c in runner.calls] == [['npm', 'install']]

    def test_a_refused_plugin_is_not_recorded_as_installed(self, tmp_path, deny_installs):
        """
        download_and_install_plugin swallows the failure and returns False,
        which is what keeps the plugins-table row from being written. A gate
        that let the install "succeed without dependencies" would leave a
        plugin that ImportErrors mid-task.
        """
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path,
                        files={'requirements.post-install.txt': 'some-package\n'})
        handler = _handler(plugins_path)
        runner = _recorder()
        with mock.patch('subprocess.run', runner), \
                mock.patch.object(handler, 'download_plugin', return_value='/tmp/x.zip'), \
                mock.patch.object(handler, 'write_plugin_data_to_db') as write_db, \
                mock.patch('zipfile.ZipFile'), \
                mock.patch.object(PluginsHandler, '_assert_zip_members_safe'):
            result = handler.download_and_install_plugin({'plugin_id': 'my_plugin', 'version': '1.0'})

        assert result is False
        write_db.assert_not_called()
        assert runner.calls == []


# ---------------------------------------------------------------------------
# What must NOT change: the plugins that actually exist
# ---------------------------------------------------------------------------

class TestExistingCatalogPluginsAreUnaffected:
    """
    Measured against the official catalog (56 plugins, fetched from
    raw.githubusercontent.com): 54 ship a `requirements.txt`, none set
    `defer_dependency_install`, none ship a `requirements.post-install.txt`,
    and they vendor the resulting `site-packages/` into the zip. Trawlarr
    never reads their requirements file, so gating it breaks none of them.
    This test is that measurement, written down.
    """

    def test_a_catalog_shaped_plugin_installs_with_the_gate_off(self, tmp_path, deny_installs):
        plugins_path = str(tmp_path / 'plugins')
        path = _plugin_on_disk(plugins_path, files={'requirements.txt': 'humanfriendly>=9.1\n'})
        vendored = os.path.join(path, 'site-packages')
        os.makedirs(vendored)
        with open(os.path.join(vendored, 'humanfriendly.py'), 'w') as f:
            f.write('# vendored into the plugin zip by the plugin repo CI\n')

        runner = _recorder()
        with mock.patch('subprocess.run', runner):
            info = _install(_handler(plugins_path))

        assert info.get('id') == 'my_plugin'
        assert runner.calls == [], "no package manager may run for a plugin that asked for nothing"
        assert os.path.isfile(os.path.join(vendored, 'humanfriendly.py')), \
            "the vendored packages the plugin ships must survive its own install"

    def test_a_plugin_with_no_files_at_all_installs_with_the_gate_off(self, tmp_path, deny_installs):
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path)
        runner = _recorder()
        with mock.patch('subprocess.run', runner):
            info = _install(_handler(plugins_path))
        assert info.get('id') == 'my_plugin'
        assert runner.calls == []


# ---------------------------------------------------------------------------
# Failures are terminal, not partial
# ---------------------------------------------------------------------------

class TestPackageManagerFailuresAreTerminal:
    """
    docs/PLUGIN-DEPENDENCIES.md tells the operator that "if pip or npm
    cannot be run, exits non-zero, or times out, the plugin install fails
    and no plugin record is written". These call sites used
    `subprocess.call` and threw the exit code away, so that sentence was
    false for exactly the routes #88 brought into scope: a pip that exited 1
    left the plugin installed with its packages absent, and it then failed
    at first execution instead. These tests are that sentence.
    """

    def test_a_failing_pip_fails_the_install(self, tmp_path, allow_installs):
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path,
                        files={'requirements.post-install.txt': 'nosuchpkg\n'})

        def _failing_pip(command, **kwargs):
            return subprocess.CompletedProcess(
                command, 1, stdout='', stderr='ERROR: No matching distribution found for nosuchpkg')

        with mock.patch('subprocess.run', _failing_pip):
            with pytest.raises(PluginDependencyError) as excinfo:
                _install(_handler(plugins_path))
        assert 'No matching distribution' in str(excinfo.value), "pip's own diagnosis must survive"

    def test_a_failing_pip_means_no_plugin_record(self, tmp_path, allow_installs):
        """The whole point: the failure must reach download_and_install_plugin,
        which is what keeps the plugins-table row from being written."""
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path, files={'requirements.post-install.txt': 'nosuchpkg\n'})
        handler = _handler(plugins_path)

        def _failing_pip(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout='', stderr='boom')

        with mock.patch('subprocess.run', _failing_pip), \
                mock.patch.object(handler, 'download_plugin', return_value='/tmp/x.zip'), \
                mock.patch.object(handler, 'write_plugin_data_to_db') as write_db, \
                mock.patch('zipfile.ZipFile'), \
                mock.patch.object(PluginsHandler, '_assert_zip_members_safe'):
            result = handler.download_and_install_plugin({'plugin_id': 'my_plugin', 'version': '1.0'})

        assert result is False
        write_db.assert_not_called()

    def test_a_missing_package_manager_fails_the_install(self, tmp_path, allow_installs):
        """npm is not in the image at all, which is the common case."""
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path, defer_dependency_install=True,
                        files={'package.json': '{"name": "x"}'})

        def _no_npm(command, **kwargs):
            raise FileNotFoundError(2, 'No such file or directory: npm')

        with mock.patch('subprocess.run', _no_npm):
            with pytest.raises(PluginDependencyError) as excinfo:
                _install(_handler(plugins_path))
        assert 'npm' in str(excinfo.value)

    def test_a_hung_pip_fails_the_install(self, tmp_path, allow_installs):
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path, files={'requirements.post-install.txt': 'requests\n'})

        def _hang(command, **kwargs):
            raise subprocess.TimeoutExpired(command, plugin_dependencies.INSTALL_TIMEOUT)

        with mock.patch('subprocess.run', _hang):
            with pytest.raises(PluginDependencyError) as excinfo:
                _install(_handler(plugins_path))
        assert 'Timed out' in str(excinfo.value)

    def test_pip_is_given_a_timeout(self, tmp_path, allow_installs):
        """A wedged pip must not hold the install request open forever."""
        calls = []

        def _run(command, **kwargs):
            calls.append(kwargs)
            return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

        path = _plugin_on_disk(str(tmp_path / 'plugins'), files={'requirements.txt': 'requests\n'})
        with mock.patch('subprocess.run', _run):
            PluginsHandler.install_plugin_requirements(path)
        assert calls[0].get('timeout') == plugin_dependencies.INSTALL_TIMEOUT

    def test_a_failing_npm_build_fails_the_install(self, tmp_path, allow_installs):
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path, defer_dependency_install=True,
                        files={'package.json': '{"name": "x", "scripts": {"build": "exit 1"}}'})

        def _npm(command, **kwargs):
            code = 1 if command[:2] == ['npm', 'run'] else 0
            return subprocess.CompletedProcess(command, code, stdout='', stderr='build failed')

        with mock.patch('subprocess.run', _npm):
            with pytest.raises(PluginDependencyError) as excinfo:
                _install(_handler(plugins_path))
        assert 'build failed' in str(excinfo.value)

    def test_an_unparseable_package_json_fails_the_install(self, tmp_path, allow_installs):
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path, defer_dependency_install=True,
                        files={'package.json': 'not json at all'})
        runner = _recorder()
        with mock.patch('subprocess.run', runner):
            with pytest.raises(PluginDependencyError) as excinfo:
                _install(_handler(plugins_path))
        assert 'package.json' in str(excinfo.value)
        assert runner.calls == [], "npm must not be run against a package.json we could not read"


# ---------------------------------------------------------------------------
# A requirements file that asks for nothing
# ---------------------------------------------------------------------------

class TestRequirementsFileThatNamesNothing:
    """
    An empty or comment-only requirements file asks pip for no packages.
    There is no install to gate, so refusing the plugin over it is wrong -
    and the refusal message read "ships a 'requirements.txt' (no
    requirements) and installing plugin dependencies is disabled", which
    tells the operator to flip a flag that would only make pip run with
    nothing to do.
    """

    @pytest.mark.parametrize('contents', ['', '\n\n', '# nothing here\n', '  \n# just a note\n\n'])
    def test_it_is_a_no_op_with_the_gate_off(self, tmp_path, deny_installs, contents):
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path, defer_dependency_install=True,
                        files={'requirements.txt': contents})
        runner = _recorder()
        with mock.patch('subprocess.run', runner):
            info = _install(_handler(plugins_path))
        assert info.get('id') == 'my_plugin'
        assert runner.calls == [], "there is nothing to install"

    def test_it_does_not_run_pip_with_the_gate_on_either(self, tmp_path, allow_installs):
        path = _plugin_on_disk(str(tmp_path / 'plugins'),
                               files={'requirements.txt': '# nothing here\n'})
        runner = _recorder()
        with mock.patch('subprocess.run', runner):
            PluginsHandler.install_plugin_requirements(path)
        assert runner.calls == [], "pip has no packages to be given"

    def test_it_does_not_destroy_the_vendored_site_packages(self, tmp_path, allow_installs):
        """A no-op must be a no-op on disk too: site-packages is rebuilt from
        empty by a real install, and there is no install here."""
        path = _plugin_on_disk(str(tmp_path / 'plugins'),
                               files={'requirements.txt': '# nothing here\n'})
        vendored = os.path.join(path, 'site-packages')
        os.makedirs(vendored)
        with open(os.path.join(vendored, 'somedep.py'), 'w') as f:
            f.write('# vendored by the plugin author\n')

        with mock.patch('subprocess.run', _recorder()):
            PluginsHandler.install_plugin_requirements(path)

        assert os.path.isfile(os.path.join(vendored, 'somedep.py'))

    def test_a_file_of_nothing_but_a_pip_option_is_still_refused(self, tmp_path, deny_installs):
        """`--index-url` on its own is not "nothing"; the grammar rule runs
        first precisely so that emptiness cannot be a way past it."""
        path = _plugin_on_disk(str(tmp_path / 'plugins'),
                               files={'requirements.txt': '--index-url https://attacker.invalid/simple\n'})
        with mock.patch('subprocess.run', _recorder()):
            with pytest.raises(PluginDependencyError) as excinfo:
                PluginsHandler.install_plugin_requirements(path)
        assert 'pip options are not accepted' in str(excinfo.value)

    def test_an_unreadable_requirements_file_is_refused(self, tmp_path, allow_installs):
        """Unreadable is not empty: we cannot say what it asked for, so we do
        not install a plugin whose requirements were never read."""
        path = _plugin_on_disk(str(tmp_path / 'plugins'), files={'requirements.txt': 'requests\n'})
        requirements = os.path.join(path, 'requirements.txt')
        os.chmod(requirements, 0o000)
        try:
            if os.access(requirements, os.R_OK):
                pytest.skip("running as a user that can read a 0o000 file")
            with mock.patch('subprocess.run', _recorder()):
                with pytest.raises(PluginDependencyError) as excinfo:
                    PluginsHandler.install_plugin_requirements(path)
        finally:
            os.chmod(requirements, 0o644)
        assert 'requirements.txt' in str(excinfo.value)


# ---------------------------------------------------------------------------
# The other caller: the developer CLI
# ---------------------------------------------------------------------------

class TestDeveloperCliReloadFromDisk:
    """
    `--manage_plugins` -> "Reload Plugin from Disk" calls the same two
    functions, so gating them changed that command too. Before, it ran pip
    for any plugin with a requirements.txt - no flag, no
    `defer_dependency_install`. Now those calls can raise, and they sat
    outside any try/except: one plugin the operator has not opted in for
    would abort the whole reload with a traceback, after its database row
    had already been written.
    """

    def _cli(self, plugins_directory):
        from trawlarr.libs.unplugins.pluginscli import PluginsCLI
        cli = PluginsCLI.__new__(PluginsCLI)
        cli.plugins_directory = plugins_directory
        return cli

    def _reload(self, cli, plugin_ids):
        from trawlarr.libs.unplugins.pluginscli import PluginsCLI
        with mock.patch.object(PluginsCLI, '_PluginsCLI__get_installed_plugins',
                               return_value=[{'plugin_id': p} for p in plugin_ids]), \
                mock.patch.object(PluginsHandler, 'write_plugin_data_to_db') as write_db, \
                mock.patch('subprocess.run', _recorder()):
            cli.reload_plugin_from_disk()
        return [call.args[0].get('plugin_id') for call in write_db.call_args_list]

    def test_a_refused_plugin_does_not_stop_the_reload(self, tmp_path, deny_installs, capsys):
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path, plugin_id='needs_deps',
                        files={'requirements.txt': 'requests\n'})
        _plugin_on_disk(plugins_path, plugin_id='plain_plugin')

        written = self._reload(self._cli(plugins_path), ['needs_deps', 'plain_plugin'])

        assert written == ['plain_plugin'], \
            "the refused plugin must be skipped and the next one still reloaded"
        assert ENV_VAR in capsys.readouterr().out, \
            "the operator has to be told which switch would have allowed it"

    def test_a_failing_pip_does_not_record_the_plugin(self, tmp_path, allow_installs):
        """Same rule as install: dependencies first, and no database row for a
        plugin whose dependencies are not there."""
        from trawlarr.libs.unplugins.pluginscli import PluginsCLI
        plugins_path = str(tmp_path / 'plugins')
        _plugin_on_disk(plugins_path, plugin_id='needs_deps', files={'requirements.txt': 'nosuchpkg\n'})
        cli = self._cli(plugins_path)

        def _failing_pip(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout='', stderr='boom')

        with mock.patch.object(PluginsCLI, '_PluginsCLI__get_installed_plugins',
                               return_value=[{'plugin_id': 'needs_deps'}]), \
                mock.patch.object(PluginsHandler, 'write_plugin_data_to_db') as write_db, \
                mock.patch('subprocess.run', _failing_pip):
            cli.reload_plugin_from_disk()

        write_db.assert_not_called()


@pytest.mark.unittest
class TestAFileThatAsksForNothingIsNotAThreat:
    """The gate exists to stop pip being run against a package index using
    names taken from a third-party plugin's files.

    A requirements file that is empty, or contains only comments, names
    nothing. There is no index to reach and no name to trust, so there is
    nothing for the gate to protect against. Refusing it turned a harmless
    file into a hard install failure -- the plugin would not install at all
    under the default configuration, where on main it installed fine.

    The message it produced said so itself: "ships a requirements.txt
    (no requirements) ... Refusing to install the plugin - it would not
    work." A refusal whose own text reports there was nothing to refuse.
    """

    def test_an_empty_requirements_file_is_a_no_op(self, tmp_path):
        requirements = tmp_path / 'requirements.txt'
        requirements.write_text('')
        # Must not raise: installs are NOT permitted here (environ={}).
        plugin_dependencies.assert_requirements_file_install_permitted(
            'demo', str(requirements), environ={})

    def test_a_comment_only_requirements_file_is_a_no_op(self, tmp_path):
        requirements = tmp_path / 'requirements.txt'
        requirements.write_text('# nothing here\n\n#  not even this\n')
        plugin_dependencies.assert_requirements_file_install_permitted(
            'demo', str(requirements), environ={})

    def test_a_file_that_does_ask_for_something_is_still_gated(self, tmp_path):
        """The guard above must not become a hole: one real requirement and
        the refusal stands."""
        requirements = tmp_path / 'requirements.txt'
        requirements.write_text('# a comment\nrequests==2.31.0\n')
        with pytest.raises(PluginDependencyError) as raised:
            plugin_dependencies.assert_requirements_file_install_permitted(
                'demo', str(requirements), environ={})
        assert 'requests==2.31.0' in str(raised.value), (
            "the refusal should name what it wanted, so an operator can decide"
        )
