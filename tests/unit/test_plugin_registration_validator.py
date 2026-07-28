#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_plugin_registration_validator.py

    Tests for trawlarr/libs/plugin_registration.py (issue #38).

    A plugin must be registered consistently in `plugins`, `enabledplugins`
    and `librarypluginflow` before it runs. When they disagree the plugin
    simply never fires and nothing is logged.

    Invariants covered:

    1. A fresh install - no plugins, no libraries, empty tables - produces no
       findings at all.
    2. A correctly registered plugin produces no findings.
    3. A plugin ENABLED WITH NO FLOW ROW produces no findings. This is the
       normal state of most plugins and the single most important false alarm
       to avoid; the issue asked for it to be reported and it must not be.
    4. A flow row with no matching enablement IS reported, names the library
       and the plugin directory id, and is an error.
    5. A row pointing at a `plugins` record that no longer exists is reported,
       from either table.
    6. A plugin registered but absent from disk is reported - the case where
       the executor drops the plugin without a word.
    7. A flow row whose `plugin_name` holds something other than the plugin
       directory id is reported as a warning and the message spells out the
       plugin_name-vs-display-name trap.
    8. Nothing is repaired: the tables are byte-for-byte unchanged after a
       validation pass, and no plugin directory is created.
    9. The result reaches a human - a UI notification is raised when there are
       findings and not when the install is clean.
    10. A validator that blows up cannot take startup down with it.
"""
import os

import pytest
from peewee import SqliteDatabase

from trawlarr.libs import plugin_registration
from trawlarr.libs.unmodels import EnabledPlugins, Libraries, LibraryPluginFlow, Plugins
from trawlarr.libs.unmodels.lib import db as db_proxy

MODELS = [Libraries, Plugins, EnabledPlugins, LibraryPluginFlow]


@pytest.fixture
def db(tmp_path):
    """
    A real SQLite database bound to the real models.

    Foreign key enforcement is deliberately OFF here. Production turns it on,
    which is exactly why an orphaned row is hard to create on purpose - but
    databases predating that pragma, partial restores and hand-edits all
    produce orphans, and the validator has to cope with them.
    """
    test_db = SqliteDatabase(str(tmp_path / 'test.db'), pragmas={'foreign_keys': 0})
    db_proxy.initialize(test_db)
    test_db.connect()
    test_db.create_tables(MODELS)
    yield test_db
    test_db.drop_tables(MODELS)
    test_db.close()
    db_proxy.initialize(None)


@pytest.fixture
def plugins_dir(tmp_path):
    path = tmp_path / 'plugins'
    path.mkdir()
    return str(path)


def install_plugin_on_disk(plugins_dir, plugin_directory_id):
    plugin_path = os.path.join(plugins_dir, plugin_directory_id)
    os.makedirs(plugin_path, exist_ok=True)
    with open(os.path.join(plugin_path, 'plugin.py'), 'w') as f:
        f.write("def on_worker_process(data):\n    return data\n")
    with open(os.path.join(plugin_path, 'info.json'), 'w') as f:
        f.write("{}\n")
    return plugin_path


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


def add_flow(library, plugin, plugin_type='worker.process', position=1, plugin_name=None):
    return LibraryPluginFlow.create(
        library_id=library,
        plugin_id=plugin,
        plugin_name=plugin.plugin_id if plugin_name is None else plugin_name,
        plugin_type=plugin_type,
        position=position,
    )


def codes(report):
    return sorted(f.code for f in report.findings)


# ---------------------------------------------------------------------------
# Quiet on healthy installs
# ---------------------------------------------------------------------------

def test_fresh_install_reports_nothing(db, plugins_dir):
    """1. Three empty tables agree with each other perfectly."""
    report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)
    assert report.ok
    assert report.findings == []


def test_library_with_no_plugins_reports_nothing(db, plugins_dir):
    make_library()
    report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)
    assert report.ok


def test_fully_registered_plugin_reports_nothing(db, plugins_dir):
    """2. Registered, enabled and ordered - the complete happy path."""
    install_plugin_on_disk(plugins_dir, 'encoder_video_h265')
    library = make_library()
    plugin = make_plugin('encoder_video_h265')
    enable(library, plugin)
    add_flow(library, plugin)

    report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)
    assert report.ok, [f.message for f in report.findings]


def test_enabled_without_a_flow_row_is_not_a_finding(db, plugins_dir):
    """
    3. THE false alarm to avoid.

    `Library.set_enabled_plugins` only writes a `librarypluginflow` row for a
    plugin whose info.json declares a non-zero priority. Every other enabled
    plugin has no flow row and runs perfectly well. Reporting this would fire
    on almost every healthy install.
    """
    install_plugin_on_disk(plugins_dir, 'encoder_video_h265')
    library = make_library()
    plugin = make_plugin('encoder_video_h265')
    enable(library, plugin)
    # Deliberately no flow row.

    report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)
    assert report.ok, [f.message for f in report.findings]


def test_two_libraries_enabling_the_same_plugin_are_not_confused(db, plugins_dir):
    """
    A plugin enabled and ordered on library A, and merely enabled on library
    B, is healthy in both. Matching must be per (library, plugin), not global.
    """
    install_plugin_on_disk(plugins_dir, 'encoder_video_h265')
    library_a = make_library(name='TV', path='/library/tv')
    library_b = make_library(name='Movies', path='/library/movies')
    plugin = make_plugin('encoder_video_h265')
    enable(library_a, plugin)
    add_flow(library_a, plugin)
    enable(library_b, plugin)

    report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)
    assert report.ok, [f.message for f in report.findings]


def test_uninstalled_plugin_not_enabled_anywhere_is_not_a_finding(db, plugins_dir):
    """
    A `plugins` row whose directory is gone but which no library enabled
    cannot fail to run, because nothing asked it to.
    """
    make_library()
    make_plugin('never_enabled')

    report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)
    assert report.ok, [f.message for f in report.findings]


# ---------------------------------------------------------------------------
# Loud on the real inconsistencies
# ---------------------------------------------------------------------------

def test_flow_row_without_enablement_is_reported(db, plugins_dir):
    """4. The issue's 'configured but not enabled'."""
    install_plugin_on_disk(plugins_dir, 'encoder_video_h265')
    library = make_library(name='TV')
    plugin = make_plugin('encoder_video_h265')
    add_flow(library, plugin)
    # Deliberately not enabled.

    report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)

    assert codes(report) == [plugin_registration.FINDING_FLOW_WITHOUT_ENABLED]
    finding = report.findings[0]
    assert finding.severity == plugin_registration.SEVERITY_ERROR
    assert finding.library_id == library.id
    assert finding.plugin_id == 'encoder_video_h265'
    # The message has to carry what the operator needs to go and look at.
    assert 'encoder_video_h265' in finding.message
    assert 'TV' in finding.message
    assert 'does not run' in finding.message


def test_enablement_on_one_library_does_not_excuse_a_flow_row_on_another(db, plugins_dir):
    """
    Matching is per (library, plugin). A plugin enabled on the TV library does
    not make a stale flow row on the Movies library harmless - it still never
    runs there.
    """
    install_plugin_on_disk(plugins_dir, 'encoder_video_h265')
    tv = make_library(name='TV', path='/library/tv')
    movies = make_library(name='Movies', path='/library/movies')
    plugin = make_plugin('encoder_video_h265')
    enable(tv, plugin)
    add_flow(tv, plugin)
    add_flow(movies, plugin)

    report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)

    assert codes(report) == [plugin_registration.FINDING_FLOW_WITHOUT_ENABLED]
    assert report.findings[0].library_id == movies.id
    assert 'Movies' in report.findings[0].message


def test_flow_row_pointing_at_a_deleted_plugin_record_is_reported(db, plugins_dir):
    """5a. Dangling foreign key from librarypluginflow."""
    install_plugin_on_disk(plugins_dir, 'encoder_video_h265')
    library = make_library()
    plugin = make_plugin('encoder_video_h265')
    enable(library, plugin)
    add_flow(library, plugin)
    Plugins.delete().where(Plugins.id == plugin.id).execute()

    report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)

    # Both tables are left holding the dangling id, and both are reported -
    # reporting only the enabledplugins side would leave the fossil flow row
    # invisible.
    assert codes(report) == [plugin_registration.FINDING_MISSING_PLUGIN_RECORD] * 2
    assert all(f.severity == plugin_registration.SEVERITY_ERROR for f in report.findings)
    # The flow row still carries the directory id, so the operator gets a name
    # to search for; the enablement row only ever held the display name.
    assert 'encoder_video_h265' in [f.plugin_id for f in report.findings]
    assert 'Plugin flow position' in ' '.join(f.message for f in report.findings)


def test_enabled_row_pointing_at_a_deleted_plugin_record_is_reported(db, plugins_dir):
    """5b. Dangling foreign key from enabledplugins."""
    library = make_library()
    plugin = make_plugin('encoder_video_h265')
    enable(library, plugin)
    Plugins.delete().where(Plugins.id == plugin.id).execute()

    report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)

    assert codes(report) == [plugin_registration.FINDING_MISSING_PLUGIN_RECORD]
    assert report.findings[0].plugin_row_id == plugin.id


def test_plugin_missing_from_disk_is_reported(db, plugins_dir):
    """
    6. The loudest case: the executor cannot import the module and returns a
    runner list one entry shorter, without a word.
    """
    library = make_library(name='TV')
    plugin = make_plugin('encoder_video_h265')
    enable(library, plugin)
    # Deliberately never installed on disk.

    report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)

    assert codes(report) == [plugin_registration.FINDING_PLUGIN_NOT_INSTALLED]
    finding = report.findings[0]
    assert finding.severity == plugin_registration.SEVERITY_ERROR
    assert finding.plugin_id == 'encoder_video_h265'
    assert 'silently does not run' in finding.message


def test_plugin_directory_without_plugin_py_is_reported(db, plugins_dir):
    """An interrupted uninstall leaves a directory the executor cannot load."""
    os.makedirs(os.path.join(plugins_dir, 'encoder_video_h265'))
    library = make_library()
    plugin = make_plugin('encoder_video_h265')
    enable(library, plugin)

    report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)

    assert codes(report) == [plugin_registration.FINDING_PLUGIN_NOT_INSTALLED]
    assert 'plugin.py' in report.findings[0].message


def test_flow_row_recording_the_display_name_is_reported_as_a_warning(db, plugins_dir):
    """
    7. The plugin_name trap. `librarypluginflow.plugin_name` holds the
    DIRECTORY id; `enabledplugins.plugin_name` holds the DISPLAY name.
    """
    install_plugin_on_disk(plugins_dir, 'encoder_video_h265')
    library = make_library()
    plugin = make_plugin('encoder_video_h265', display_name='Video Encoder H265')
    enable(library, plugin)
    add_flow(library, plugin, plugin_name='Video Encoder H265')

    report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)

    assert codes(report) == [plugin_registration.FINDING_FLOW_NAME_MISMATCH]
    finding = report.findings[0]
    # Not an error: execution follows the foreign key, not this column.
    assert finding.severity == plugin_registration.SEVERITY_WARNING
    assert report.severity == plugin_registration.SEVERITY_WARNING
    assert 'DIRECTORY id' in finding.message
    assert 'display name' in finding.message


def test_findings_are_deterministically_ordered(db, plugins_dir):
    library = make_library(name='TV')
    other = make_library(name='Movies', path='/library/movies')
    plugin_a = make_plugin('aaa_plugin')
    plugin_b = make_plugin('zzz_plugin')
    add_flow(library, plugin_b)
    add_flow(other, plugin_a)

    report = plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)
    first_run = [f.message for f in report.sorted_findings()]
    second = plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)
    assert [f.message for f in second.sorted_findings()] == first_run


# ---------------------------------------------------------------------------
# It reports; it does not repair
# ---------------------------------------------------------------------------

def test_validation_changes_nothing(db, plugins_dir):
    """
    8. Every finding here has more than one correct repair, so the validator
    must make none of them.
    """
    library = make_library()
    plugin = make_plugin('encoder_video_h265')
    add_flow(library, plugin)

    before = (
        list(Plugins.select().dicts()),
        list(EnabledPlugins.select().dicts()),
        list(LibraryPluginFlow.select().dicts()),
    )

    plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)

    after = (
        list(Plugins.select().dicts()),
        list(EnabledPlugins.select().dicts()),
        list(LibraryPluginFlow.select().dicts()),
    )
    assert before == after


def test_validation_does_not_create_the_plugin_directory(db, plugins_dir):
    """
    `PluginsHandler.get_plugin_path` creates the directory as a side effect.
    A diagnostic must not alter what it inspects - and creating an empty
    directory here would make the next run report a different problem.
    """
    library = make_library()
    plugin = make_plugin('encoder_video_h265')
    enable(library, plugin)

    plugin_registration.validate_plugin_registration(plugins_directory=plugins_dir)

    assert os.listdir(plugins_dir) == []


# ---------------------------------------------------------------------------
# The result reaches a human
# ---------------------------------------------------------------------------

class FakeNotifications(object):
    def __init__(self):
        self.items = []

    def update(self, item):
        self.items.append(item)


@pytest.fixture
def fake_notifications(monkeypatch):
    fake = FakeNotifications()
    monkeypatch.setattr('trawlarr.libs.notifications.Notifications', lambda: fake)
    return fake


def test_findings_raise_a_ui_notification(db, plugins_dir, fake_notifications):
    """9. 'The log says nothing' is the complaint; the log alone is not enough."""
    library = make_library(name='TV')
    plugin = make_plugin('encoder_video_h265')
    add_flow(library, plugin)

    report = plugin_registration.report_plugin_registration(plugins_directory=plugins_dir)

    assert not report.ok
    assert len(fake_notifications.items) == 1
    item = fake_notifications.items[0]
    assert item['uuid'] == plugin_registration.NOTIFICATION_UUID
    assert item['type'] == 'error'
    assert item['label'] == 'pluginRegistrationLabel'
    assert item['navigation']['push'] == '/ui/settings-library'
    # The notification must be specific enough to act on without the log.
    assert 'encoder_video_h265' in item['message']
    assert 'has NOT' in item['message'] or 'not changed anything' in item['message']


def test_clean_install_raises_no_notification(db, plugins_dir, fake_notifications):
    install_plugin_on_disk(plugins_dir, 'encoder_video_h265')
    library = make_library()
    plugin = make_plugin('encoder_video_h265')
    enable(library, plugin)

    report = plugin_registration.report_plugin_registration(plugins_directory=plugins_dir)

    assert report.ok
    assert fake_notifications.items == []


def test_notification_truncates_but_says_how_many_were_left_out(db, plugins_dir, fake_notifications):
    library = make_library()
    for i in range(plugin_registration.NOTIFICATION_DETAIL_LIMIT + 3):
        plugin_directory_id = 'plugin_{:02d}'.format(i)
        install_plugin_on_disk(plugins_dir, plugin_directory_id)
        plugin = make_plugin(plugin_directory_id)
        add_flow(library, plugin)

    plugin_registration.report_plugin_registration(plugins_directory=plugins_dir)

    message = fake_notifications.items[0]['message']
    assert 'and 3 more' in message


# ---------------------------------------------------------------------------
# The check cannot take the service down
# ---------------------------------------------------------------------------

def test_a_broken_check_does_not_raise(db, plugins_dir, monkeypatch):
    """10. A diagnostic that crashes startup is worse than the silence."""

    def boom(**kwargs):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(plugin_registration, 'validate_plugin_registration', boom)
    assert plugin_registration.report_plugin_registration(plugins_directory=plugins_dir) is None


def test_a_broken_notification_does_not_raise(db, plugins_dir, monkeypatch):
    library = make_library()
    plugin = make_plugin('encoder_video_h265')
    add_flow(library, plugin)

    def boom():
        raise RuntimeError("no queue")

    monkeypatch.setattr('trawlarr.libs.notifications.Notifications', boom)
    report = plugin_registration.report_plugin_registration(plugins_directory=plugins_dir)
    assert not report.ok


def test_startup_runs_the_check(monkeypatch):
    """
    The validator is worthless if nothing calls it. Pin the wiring in
    RootService.start_threads.
    """
    import inspect

    from trawlarr import service

    source = inspect.getsource(service.RootService.start_threads)
    assert 'plugin_registration.report_plugin_registration()' in source
