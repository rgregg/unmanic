#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_required_plugin_settings.py

    Tests for trawlarr/libs/plugin_settings.py (issue #40).

    A plugin whose required setting is empty does not fail - it returns the
    file unchanged and the task goes green. The point of this feature is that
    such a plugin refuses to run and says so against the library that
    references it.

    The dangerous failure mode here is NOT "missed a misconfiguration", it is
    "refused a healthy task". Every plugin has optional settings and almost all
    of them are empty on a normal install, so the bulk of these tests are
    false-alarm tests: nothing may be reported unless a plugin author
    explicitly declared a setting required AND that setting is genuinely empty.

    Invariants covered:

    1.  A plugin that declares nothing required - every plugin that exists
        today - produces no findings whatever its settings look like.
    2.  `False` and `0` are configured values, not omissions.
    3.  Empty strings, whitespace-only strings, None and empty containers are
        omissions.
    4.  Display-only form entries (section headers) are never reported.
    5.  A required setting that IS set produces no finding.
    6.  An unset required setting is reported as an error, names the library,
        the plugin and the setting, and says CONFIGURATION ERROR.
    7.  The finding is scoped per (library, plugin): the same plugin can be
        fine on one library and broken on another.
    8.  The startup path merges these findings into the registration report
        rather than opening a second channel, and startup is actually wired.
    9.  A worker refuses such a task BEFORE any runner executes, records a
        durable reason, and returns False.
    10. A worker with correctly configured plugins is not refused, and a check
        that blows up fails open rather than blocking the library.
"""
import logging
import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest
from peewee import SqliteDatabase

from trawlarr import config as config_module
from trawlarr.libs import plugin_registration, plugin_settings, runtimepaths
from trawlarr.libs.unmodels import EnabledPlugins, Libraries, LibraryPluginFlow, Plugins
from trawlarr.libs.unmodels.lib import db as db_proxy
from trawlarr.libs.task import TaskDataStore
from trawlarr.libs.workers import TASK_FAILURE_STATE_KEY, Worker

MODELS = [Libraries, Plugins, EnabledPlugins, LibraryPluginFlow]


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


def make_library(name='TV', path='/library/tv'):
    return Libraries.create(name=name, path=path)


def make_plugin(plugin_directory_id, display_name=None):
    return Plugins.create(
        plugin_id=plugin_directory_id,
        name=display_name or plugin_directory_id.replace('_', ' ').title(),
        author='someone',
        version='1.0.0',
        tags='',
        description='',
        icon='',
        local_path='/config/.trawlarr/plugins/{}'.format(plugin_directory_id),
    )


def enable(library, plugin):
    return EnabledPlugins.create(library_id=library, plugin_id=plugin, plugin_name=plugin.name)


class FakeExecutor:
    """
    Stands in for PluginExecutor. Maps (plugin_id, library_id) to the
    (settings, form_settings) pair the real executor would return.
    """

    def __init__(self, responses=None, default=None):
        self.responses = responses or {}
        self.default = default if default is not None else ({}, {})
        self.calls = []

    def get_plugin_settings(self, plugin_id, library_id=None):
        self.calls.append((plugin_id, library_id))
        if (plugin_id, library_id) in self.responses:
            return self.responses[(plugin_id, library_id)]
        return self.responses.get(plugin_id, self.default)


# ---------------------------------------------------------------------------
# 1-3. What counts as unset
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value', [None, '', '   ', '\n\t', [], (), set(), {}])
def test_empty_values_are_unset(value):
    assert plugin_settings.setting_is_unset(value) is True


@pytest.mark.parametrize('value', [False, True, 0, 0.0, -1, 'x', ' x ', [0], {'a': 1}, object()])
def test_configured_values_are_not_unset(value):
    """
    `False` and `0` are the ones that matter. A required checkbox that is off
    is a decision; reporting it would fire on every install that deliberately
    turned something off.
    """
    assert plugin_settings.setting_is_unset(value) is False


# ---------------------------------------------------------------------------
# The false alarms
# ---------------------------------------------------------------------------

def test_plugin_declaring_nothing_required_is_never_reported():
    """
    1. THE false alarm to avoid. Every community plugin in existence declares
    no `required` key, and most have empty optional settings.
    """
    settings = {'stream_language': '', 'advanced': '', 'keep_container': False, 'threads': 0}
    form_settings = {
        'stream_language': {'label': 'Language'},
        'advanced':        {'label': 'Advanced options', 'input_type': 'textarea'},
        'keep_container':  {'label': 'Keep container', 'input_type': 'checkbox'},
    }
    assert plugin_settings.find_unset_required_settings(settings, form_settings) == []


def test_plugin_with_no_form_settings_at_all_is_never_reported():
    assert plugin_settings.find_unset_required_settings({'a': ''}, {}) == []
    assert plugin_settings.find_unset_required_settings({'a': ''}, None) == []


def test_required_must_be_literally_true():
    """
    A truthy value that is not `True` is not a declaration. This keeps a
    plugin that happens to use the key for something else from failing its own
    tasks.
    """
    for declared in ['yes', 1, 'true', [1]]:
        form_settings = {'lang': {'label': 'Language', 'required': declared}}
        assert plugin_settings.find_unset_required_settings({'lang': ''}, form_settings) == [], \
            "required={!r} should not be read as a requirement".format(declared)


def test_non_dict_form_entry_is_ignored():
    assert plugin_settings.find_unset_required_settings({'lang': ''}, {'lang': 'Language'}) == []


def test_display_only_form_entries_are_never_reported():
    """
    4. `section_header` and friends live in form_settings with no matching key
    in settings. They can never be "set".
    """
    settings = {'lang': 'eng'}
    form_settings = {
        'lang':      {'label': 'Language', 'required': True},
        'my_header': {'label': 'Advanced', 'input_type': 'section_header', 'required': True},
    }
    assert plugin_settings.find_unset_required_settings(settings, form_settings) == []


def test_required_setting_that_is_set_is_not_reported():
    """5."""
    form_settings = {'lang': {'label': 'Language', 'required': True}}
    assert plugin_settings.find_unset_required_settings({'lang': 'eng'}, form_settings) == []


def test_required_boolean_that_is_false_is_not_reported():
    form_settings = {'strip': {'label': 'Strip', 'input_type': 'checkbox', 'required': True}}
    assert plugin_settings.find_unset_required_settings({'strip': False}, form_settings) == []


# ---------------------------------------------------------------------------
# The real thing
# ---------------------------------------------------------------------------

def test_unset_required_setting_is_found():
    form_settings = {'lang': {'label': 'Stream language to keep', 'required': True}}
    unset = plugin_settings.find_unset_required_settings({'lang': ''}, form_settings)
    assert [u['key'] for u in unset] == ['lang']
    assert unset[0]['label'] == 'Stream language to keep'


def test_several_unset_required_settings_are_all_found_in_a_stable_order():
    form_settings = {
        'zeta':  {'label': 'Zeta', 'required': True},
        'alpha': {'label': 'Alpha', 'required': True},
        'other': {'label': 'Other'},
    }
    unset = plugin_settings.find_unset_required_settings({'zeta': None, 'alpha': '  ', 'other': ''}, form_settings)
    assert [u['key'] for u in unset] == ['alpha', 'zeta']


# ---------------------------------------------------------------------------
# 6-7. Startup validation, against the library
# ---------------------------------------------------------------------------

BROKEN = ({'stream_language': ''}, {'stream_language': {'label': 'Stream language', 'required': True}})
HEALTHY = ({'stream_language': 'eng'}, {'stream_language': {'label': 'Stream language', 'required': True}})
NO_DECLARATION = ({'stream_language': ''}, {'stream_language': {'label': 'Stream language'}})


def test_startup_validation_is_quiet_on_a_fresh_install(db):
    assert plugin_settings.validate_required_plugin_settings(plugin_executor=FakeExecutor()) == []


def test_startup_validation_is_quiet_when_nothing_declares_a_requirement(db):
    library = make_library()
    plugin = make_plugin('encoder_video_h265')
    enable(library, plugin)
    findings = plugin_settings.validate_required_plugin_settings(
        plugin_executor=FakeExecutor({'encoder_video_h265': NO_DECLARATION}))
    assert findings == [], [f.message for f in findings]


def test_startup_validation_is_quiet_when_the_required_setting_is_configured(db):
    library = make_library()
    plugin = make_plugin('encoder_video_h265')
    enable(library, plugin)
    findings = plugin_settings.validate_required_plugin_settings(
        plugin_executor=FakeExecutor({'encoder_video_h265': HEALTHY}))
    assert findings == []


def test_unset_required_setting_is_reported_against_the_library(db):
    """6. The whole point of the issue."""
    library = make_library(name='TV Shows')
    plugin = make_plugin('encoder_video_h265', display_name='H265 Encoder')
    enable(library, plugin)

    findings = plugin_settings.validate_required_plugin_settings(
        plugin_executor=FakeExecutor({'encoder_video_h265': BROKEN}))

    assert len(findings) == 1, "The misconfigured plugin was not reported at all"
    finding = findings[0]
    assert finding.code == plugin_settings.FINDING_REQUIRED_SETTING_UNSET
    assert finding.severity == plugin_registration.SEVERITY_ERROR
    assert finding.library_id == library.id
    assert finding.library_name == 'TV Shows'
    assert finding.plugin_id == 'encoder_video_h265'
    assert 'CONFIGURATION ERROR' in finding.message
    assert 'TV Shows' in finding.message
    assert 'stream_language' in finding.message


def test_a_plugin_installed_but_not_enabled_is_not_reported(db):
    """Nothing asked it to run, so it cannot silently fail to run."""
    make_library()
    make_plugin('encoder_video_h265')
    findings = plugin_settings.validate_required_plugin_settings(
        plugin_executor=FakeExecutor({'encoder_video_h265': BROKEN}))
    assert findings == []


def test_settings_are_read_per_library(db):
    """
    7. Plugin settings are per-library profiles. The same plugin can be
    configured on one library and empty on another, and only the empty one may
    be reported.
    """
    tv = make_library(name='TV', path='/library/tv')
    movies = make_library(name='Movies', path='/library/movies')
    plugin = make_plugin('encoder_video_h265')
    enable(tv, plugin)
    enable(movies, plugin)

    executor = FakeExecutor({
        ('encoder_video_h265', tv.id):     HEALTHY,
        ('encoder_video_h265', movies.id): BROKEN,
    })
    findings = plugin_settings.validate_required_plugin_settings(plugin_executor=executor)

    assert [f.library_id for f in findings] == [movies.id], \
        "Settings must be resolved against each library's own profile"


def test_a_broken_plugin_load_does_not_produce_a_finding(db):
    """
    A plugin that cannot be loaded is already `plugin_not_installed` in the
    registration validator. Reporting it twice, under a misleading code, would
    send the operator to the wrong screen.
    """
    library = make_library()
    plugin = make_plugin('encoder_video_h265')
    enable(library, plugin)

    class ExplodingExecutor:
        def get_plugin_settings(self, plugin_id, library_id=None):
            raise RuntimeError('module blew up on import')

    assert plugin_settings.validate_required_plugin_settings(plugin_executor=ExplodingExecutor()) == []


# ---------------------------------------------------------------------------
# 8. Merged into the one startup report
# ---------------------------------------------------------------------------

def test_extra_findings_are_merged_into_the_registration_report(db, tmp_path):
    plugins_dir = str(tmp_path / 'plugins')
    extra = plugin_settings.validate_required_plugin_settings(plugin_executor=FakeExecutor())
    extra.append(plugin_registration.RegistrationFinding(
        code=plugin_settings.FINDING_REQUIRED_SETTING_UNSET,
        severity=plugin_registration.SEVERITY_ERROR,
        message='CONFIGURATION ERROR: made up finding',
        library_id=1,
        plugin_id='some_plugin',
    ))

    with mock.patch('trawlarr.libs.notifications.Notifications') as notifications:
        report = plugin_registration.report_plugin_registration(
            plugins_directory=plugins_dir, extra_findings=extra)

    assert report is not None
    assert not report.ok, "A settings finding must make the merged report non-clean"
    assert 'made up finding' in report.notification_message()
    assert notifications.return_value.update.called, \
        "The merged finding never reached the operator"


def test_startup_runs_the_required_settings_check():
    """
    8. The check is worthless if nothing calls it. Pin the wiring.
    """
    import inspect

    from trawlarr import service

    source = inspect.getsource(service.RootService.start_threads)
    assert 'plugin_settings.validate_required_plugin_settings()' in source
    assert 'extra_findings=' in source


# ---------------------------------------------------------------------------
# 9-10. The worker refuses the task
# ---------------------------------------------------------------------------

def _bare_worker(task_id=4242):
    worker = Worker.__new__(Worker)
    object.__setattr__(worker, '_name', 'Worker-Test')
    worker._initialized = True
    worker.logger = logging.getLogger('test_required_plugin_settings')
    worker.worker_log = []
    worker.worker_runners_info = {}
    worker.current_task = mock.Mock()
    worker.current_task.get_task_id.return_value = task_id
    return worker


def _refuse(worker, plugin_modules, executor):
    with mock.patch.object(plugin_settings, 'unset_required_settings_for_plugin',
                              side_effect=lambda pid, library_id=None, plugin_executor=None:
                              plugin_settings.find_unset_required_settings(
                                  *executor.get_plugin_settings(pid, library_id=library_id))), \
            mock.patch.object(plugin_settings, 'raise_configuration_notification'):
        return worker._Worker__refuse_task_on_unconfigured_plugins(plugin_modules, 7, 'TV Shows')


def test_worker_runs_normally_when_plugins_are_configured():
    """10. The false alarm that would stop every library dead."""
    worker = _bare_worker()
    executor = FakeExecutor({'encoder_video_h265': HEALTHY})
    assert _refuse(worker, [{'plugin_id': 'encoder_video_h265'}], executor) is True
    assert worker.worker_log == []
    assert not worker.current_task.save_command_log.called


def test_worker_runs_normally_when_there_are_no_plugins():
    worker = _bare_worker()
    assert _refuse(worker, [], FakeExecutor()) is True


def test_worker_refuses_a_task_whose_plugin_is_unconfigured():
    """9."""
    task_id = 55501
    TaskDataStore.clear_task(task_id)
    worker = _bare_worker(task_id=task_id)
    worker.worker_runners_info = {'encoder_video_h265': {'status': 'pending'}}
    executor = FakeExecutor({'encoder_video_h265': BROKEN})

    result = _refuse(worker, [{'plugin_id': 'encoder_video_h265'}], executor)

    assert result is False, "The task was allowed to run with an unconfigured plugin"

    joined = ''.join(worker.worker_log)
    assert 'TASK FAILED [CONFIGURATION]' in joined
    assert 'stream_language' in joined
    assert 'has NOT been modified' in joined

    record = TaskDataStore.get_task_state(TASK_FAILURE_STATE_KEY, task_id=task_id)
    TaskDataStore.clear_task(task_id)
    assert record is not None, "No durable reason was left on the refused task"
    assert record['category'] == 'configuration'
    assert worker.current_task.save_command_log.called, \
        "The refusal banner was never persisted to the task's command log"
    assert worker.worker_runners_info['encoder_video_h265']['status'] == 'configuration_error'


def test_worker_fails_open_when_the_check_itself_breaks():
    """
    10. A bug in a diagnostic must not be able to stop a library from being
    processed.
    """
    worker = _bare_worker()
    with mock.patch.object(plugin_settings, 'check_plugins_before_run',
                           side_effect=RuntimeError('boom')):
        assert worker._Worker__refuse_task_on_unconfigured_plugins(
            [{'plugin_id': 'encoder_video_h265'}], 7, 'TV') is True


def test_worker_gate_runs_before_any_runner():
    """
    The gate is only a gate if it precedes the runner loop. Refusing after a
    runner has produced a cache file would trade a silent no-op for a
    half-processed file.
    """
    import inspect

    source = inspect.getsource(Worker._Worker__exec_worker_runners_on_set_task)
    gate = source.index('__refuse_task_on_unconfigured_plugins')
    first_runner = source.index('events.worker_process_started')
    assert gate < first_runner, \
        "The required-settings gate must run before any plugin runner or event fires"


# ---------------------------------------------------------------------------
# 11. The checks against a real install layout, with no injected executor
#
# Everything above this line injects a FakeExecutor, which proves the logic
# and proves nothing at all about whether either check can see a plugin on a
# real machine. Both checks build their own PluginExecutor in production, and
# an executor pointed at the wrong directory does not fail - it finds no
# plugin, reports nothing, and passes. A gate that is inert on every real
# install is worse than no gate, because the PR claims one exists.
#
# So these tests install an actual plugin package on disk, under an actual
# ~/.trawlarr/plugins, and call the checks the way production calls them.
# ---------------------------------------------------------------------------

PLUGIN_SOURCE = '''
from trawlarr.libs.unplugins.settings import PluginSettings


class Settings(PluginSettings):
    settings = {{
        'stream_language': {value!r},
    }}
    form_settings = {{
        'stream_language': {{
            'label':    'Stream language to keep',
            'required': True,
        }},
    }}


def worker_process(data):
    return data
'''


def _install_plugin(plugins_directory, plugin_id, value=''):
    """Write a real, importable plugin package to disk."""
    plugin_directory = os.path.join(plugins_directory, plugin_id)
    os.makedirs(plugin_directory, exist_ok=True)
    with open(os.path.join(plugin_directory, 'plugin.py'), 'w') as f:
        f.write(PLUGIN_SOURCE.format(value=value))
    return plugin_directory


@pytest.fixture
def real_install(tmp_path, monkeypatch, request):
    """
    A real install: HOME/.trawlarr/plugins, an installed plugin, and no
    injected executor anywhere.

    Config is a singleton, so it is dropped from the registry on the way in
    and on the way out; otherwise this measures whatever HOME the first test
    in the suite happened to run under.
    """
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('HOME_DIR', raising=False)
    for key in config_module.DERIVED_PATH_CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)
    registry = type(config_module.Config)._instances
    registry.pop(config_module.Config, None)

    # A unique id per test: the executor caches loaded modules in sys.modules
    # by '<plugin_id>.plugin', so a shared id would let one test read another
    # test's settings.
    plugin_id = 'required_settings_probe_{}'.format(abs(hash(request.node.name)) % 100000)
    plugins_directory = os.path.join(str(tmp_path), runtimepaths.APP_DIR_NAME, 'plugins')
    os.makedirs(plugins_directory, exist_ok=True)
    sys_path_before = list(sys.path)

    yield SimpleNamespace(plugin_id=plugin_id, plugins_directory=plugins_directory, home=str(tmp_path))

    sys.modules.pop('{}.plugin'.format(plugin_id), None)
    sys.path[:] = sys_path_before
    registry.pop(config_module.Config, None)


def test_the_runtime_gate_fires_on_a_real_install(real_install):
    """
    The gate must produce its finding for a plugin installed where the
    application actually installs plugins, with the executor it builds itself.
    """
    _install_plugin(real_install.plugins_directory, real_install.plugin_id, value='')

    misconfigured = plugin_settings.check_plugins_before_run([real_install.plugin_id], library_id=None)

    assert [item['plugin_id'] for item in misconfigured] == [real_install.plugin_id], \
        "The runtime gate did not see a plugin installed in the real plugins directory"
    assert [u['key'] for u in misconfigured[0]['unset']] == ['stream_language']
    assert misconfigured[0]['unset'][0]['label'] == 'Stream language to keep'


def test_the_runtime_gate_stays_quiet_when_that_same_plugin_is_configured(real_install):
    """The other half: the gate must not fire on a configured install, or it
    is not a gate, it is a wall."""
    _install_plugin(real_install.plugins_directory, real_install.plugin_id, value='eng')

    assert plugin_settings.check_plugins_before_run([real_install.plugin_id], library_id=None) == []


def test_the_startup_validator_fires_on_a_real_install(db, real_install):
    """The startup half of the same proof, through the library tables."""
    library = make_library()
    plugin = make_plugin(real_install.plugin_id)
    enable(library, plugin)
    _install_plugin(real_install.plugins_directory, real_install.plugin_id, value='')

    findings = plugin_settings.validate_required_plugin_settings()

    assert len(findings) == 1, "The startup validator did not see a plugin installed on the real path"
    assert findings[0].code == plugin_settings.FINDING_REQUIRED_SETTING_UNSET
    assert findings[0].plugin_id == real_install.plugin_id
    assert 'stream_language' in findings[0].message


def test_the_checks_follow_the_configured_plugins_path(real_install, tmp_path, monkeypatch):
    """
    `plugins_path` is configurable, so the home-directory default is only
    right on a default install. A check that reads a different directory from
    the one the runner loads from reports nothing and passes silently - which
    is exactly the invisible no-op this feature exists to stop.
    """
    elsewhere = str(tmp_path / 'srv' / 'plugins')
    monkeypatch.setenv('plugins_path', elsewhere)
    type(config_module.Config)._instances.pop(config_module.Config, None)
    _install_plugin(elsewhere, real_install.plugin_id, value='')
    assert not os.path.exists(os.path.join(real_install.plugins_directory, real_install.plugin_id))

    misconfigured = plugin_settings.check_plugins_before_run([real_install.plugin_id], library_id=None)

    assert [item['plugin_id'] for item in misconfigured] == [real_install.plugin_id], \
        "The check ignored the configured plugins_path and looked under the home directory instead"
