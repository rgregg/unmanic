#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_pipeline_end_to_end.py

    The one test that crosses every seam (issue #84).

    WHY THIS EXISTS
    ---------------
    Every other test in this repository exercises a piece. This one starts the
    application against a throwaway HOME and a throwaway library, drops a file
    in the library, and waits for that file to come out the other end changed,
    recorded in history, and marked done.

    Two bugs shipped in this milestone that no unit test could have caught,
    because both lived in the wiring rather than in a piece:

      * the per-library extension allow-list, whose entire chain from the
        saved setting to the queueing decision was untested. Four separate
        mutations, each disabling the feature outright, left the suite green.
      * ``PluginExecutor()`` defaulting to ``~/.unmanic/plugins`` while the
        application installs plugins to ``~/.trawlarr/plugins``, across ten
        call sites. Tests either passed an explicit directory or ran with no
        plugins at all, where both paths are equally empty.

    This test would have failed on both, and that is checked rather than
    asserted: see the "verified red" notes on the individual tests.

    WHAT IS REAL AND WHAT IS NOT
    ----------------------------
    Real: a temp ``$HOME``; ``config.Config`` resolved from it; the on-disk
    SQLite database built by ``Migrations.update_schema()``; a plugin loaded
    off disk through the ordinary plugin loader; ``RootService.start_threads()``
    itself, so the TaskHandler / Foreman / Worker / PostProcessor /
    LibraryScannerManager threads are constructed and wired exactly the way
    ``service.run()`` wires them, including the stranded-task reconciliation
    and the plugin-registration report that run before them.

    Not real, and deliberately so:

      * **The UI server.** ``RootService.start_ui_server`` is replaced with a
        stub. It binds a TCP port, which in CI is a source of failures that
        say nothing about the pipeline. Nothing downstream of a queued file
        goes through it.
      * **Transcoding.** The stub plugin asks Unmanic to run a two-line Python
        program that rewrites the file rather than ffmpeg. The point is the
        pipeline, not the codec - but note that it is a genuine
        ``exec_command`` dispatched through ``__exec_command_subprocess``, not
        a plugin that quietly returns ``data`` untouched, so the worker's
        subprocess path is exercised too.
      * **Media probes.** There is no ffprobe on the test files, so the output
        sanity checks (#35) report "not checked" and pass through. That is
        their documented behaviour when a file cannot be probed.

    ON FLAKINESS
    ------------
    Threads plus polling is how integration tests become 3am pages and then
    get deleted. Every wait here is bounded by ``_wait_for``, which fails with
    a description of what never happened rather than hanging, and the whole
    module tears its threads down in a fixture ``finally``.
"""

import inspect
import json
import os
import sys
import textwrap
import threading
import time

import pytest
from peewee import Model, SqliteDatabase

#: Bound on the whole discovery -> queue -> process -> post-process path.
#: The loops involved poll on 1-2 second intervals, so the real figure is
#: 10-20 seconds on an idle machine. This is a "something is wrong" bound,
#: not an expectation.
PIPELINE_TIMEOUT = 120

#: Marker bytes written into the library file. The stub plugin's command
#: rewrites SOURCE_MARKER into PROCESSED_MARKER; they are the same length so
#: the file neither grows nor shrinks.
SOURCE_MARKER = b'RAWFILE'
PROCESSED_MARKER = b'DONEFIL'

PLUGIN_ID = 'e2e_stub_processor'

#: The stub plugin, written to the plugins directory as a real plugin would be
#: installed. It votes to queue any file it is shown, then asks Unmanic to run
#: a command that rewrites it.
PLUGIN_SOURCE = textwrap.dedent(
    '''
    import sys

    REWRITE = (
        "import sys;"
        "src, dst = sys.argv[1], sys.argv[2];"
        "data = open(src, 'rb').read();"
        "open(dst, 'wb').write(data.replace({source!r}, {processed!r}))"
    )


    def on_library_management_file_test(data):
        """Vote to queue every file that reaches the plugin tier."""
        data['add_file_to_pending_tasks'] = True
        return data


    def on_worker_process(data):
        """Ask Unmanic to run a real subprocess that rewrites the file."""
        data['exec_command'] = [
            sys.executable, '-c', REWRITE, data.get('file_in'), data.get('file_out'),
        ]
        return data
    '''
).format(source=SOURCE_MARKER, processed=PROCESSED_MARKER)

PLUGIN_INFO = {
    'id':          PLUGIN_ID,
    'name':        'End-to-end stub processor',
    'author':      'trawlarr-tests',
    'version':     '0.0.1',
    'description': 'Rewrites a marker in the file. Used by the end-to-end pipeline test.',
    'icon':        '',
    'tags':        'testing',
}


def _wait_for(predicate, what, timeout=PIPELINE_TIMEOUT, interval=0.25):
    """
    Poll `predicate` until it returns something truthy, or fail loudly.

    Every wait in this module goes through here. A test that hangs tells you
    nothing and blocks CI; a test that fails after a bounded wait names the
    step of the pipeline that never happened.

    :param predicate:
    :param what: description of the step, used in the failure message
    :param timeout:
    :param interval:
    :return:
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = predicate()
        except Exception:
            result = None
        if result:
            return result
        time.sleep(interval)
    pytest.fail("Timed out after {}s waiting for {}".format(timeout, what))


def _all_models():
    # Imported for the side effect of populating the module, which is what is
    # then introspected.
    import trawlarr.libs.unmodels  # noqa: F401

    discovered = inspect.getmembers(sys.modules['trawlarr.libs.unmodels'], inspect.isclass)
    return [model for _name, model in discovered if issubclass(model, Model)]


def _reset_singletons():
    """
    Drop every application singleton so the next construction reads the
    patched environment.

    Config in particular caches the config/plugins/userdata paths it resolved
    from ``$HOME`` at construction, and a Config built by an earlier test
    points at an earlier test's directory.
    """
    from trawlarr import config
    from trawlarr.libs import frontend_push_messages, notifications, plugins, session, system, uiserver

    for cls in (config.Config, plugins.PluginsHandler, session.Session, system.System,
                notifications.Notifications, frontend_push_messages.FrontendPushMessages,
                uiserver.TrawlarrDataQueues, uiserver.TrawlarrRunningTreads):
        type(cls)._instances.pop(cls, None)


def _install_stub_plugin(plugins_path):
    """
    Put the stub plugin on disk the way an installed plugin sits on disk.

    Deliberately NOT via a mock or an injected module: the plugin has to be
    found by ``PluginExecutor``'s own directory resolution, which is where the
    ``~/.unmanic/plugins`` bug lived.
    """
    from trawlarr.libs.plugins import PluginsHandler

    plugin_dir = os.path.join(plugins_path, PLUGIN_ID)
    os.makedirs(plugin_dir, exist_ok=True)
    # The compatibility list is not decoration. A plugin that does not declare
    # the running PluginsHandler version is treated as incompatible, and both
    # the library scanner and the post-processor then refuse to do anything at
    # all. Correct behaviour, and precisely the sort of thing a mocked plugin
    # would have hidden.
    with open(os.path.join(plugin_dir, 'info.json'), 'w') as f:
        json.dump(dict(PLUGIN_INFO, compatibility=[PluginsHandler.version]), f)
    with open(os.path.join(plugin_dir, 'plugin.py'), 'w') as f:
        f.write(PLUGIN_SOURCE)
    return plugin_dir


class _StubUIServer(threading.Thread):
    """Stands in for the UIServer so no port is bound. See the module docstring."""

    def __init__(self):
        super(_StubUIServer, self).__init__(name='StubUIServer', daemon=True)
        self._stop = threading.Event()

    def run(self):
        self._stop.wait(PIPELINE_TIMEOUT * 2)

    def stop(self):
        self._stop.set()


class Pipeline:
    """Handles the test needs on the running service."""

    def __init__(self, service_instance, settings, library_path, library_id, cache_path):
        self.service = service_instance
        self.settings = settings
        self.library_path = library_path
        self.library_id = library_id
        self.cache_path = cache_path


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """
    A started Trawlarr service against a temp HOME and a temp library.

    Yields once the threads are running. Everything is torn down on the way
    out, including the model bindings the migrator rewires as a side effect -
    without that, the rest of the integration suite would inherit this test's
    database file.
    """
    from trawlarr import config, service
    from trawlarr.libs import runtimepaths
    from trawlarr.libs.library import Library
    from trawlarr.libs.unmodels.lib.basemodel import db as db_proxy
    from trawlarr.libs.worker_group import WorkerGroup

    home = tmp_path / 'home'
    library_path = tmp_path / 'library'
    cache_path = tmp_path / 'cache'
    for path in (home, library_path, cache_path):
        path.mkdir(parents=True, exist_ok=True)

    # common.get_home_dir() reads HOME_DIR first, then expanduser('~').
    monkeypatch.delenv('HOME_DIR', raising=False)
    monkeypatch.setenv('HOME', str(home))
    _reset_singletons()

    settings = config.Config()
    # Guard the guard: if this ever fails, the test is about to write into the
    # developer's real ~/.trawlarr and everything below it is meaningless.
    assert str(home) in settings.get_config_path(), \
        "Config did not resolve against the temp HOME; refusing to run"

    settings.set_config_item('cache_path', str(cache_path), save_settings=False)
    settings.set_config_item('library_path', str(library_path), save_settings=False)
    settings.set_config_item('enable_library_scanner', True, save_settings=False)
    settings.set_config_item('run_full_scan_on_start', True, save_settings=False)
    settings.set_config_item('schedule_full_scan_minutes', 1440, save_settings=False)
    settings.set_config_item('concurrent_file_testers', 1, save_settings=False)
    settings.set_config_item('clear_pending_tasks_on_restart', True, save_settings=False)

    _install_stub_plugin(settings.get_plugins_path())

    # peewee-migrate rebinds every model's Meta.database as a side effect of
    # creating tables, and the models are module-level globals shared with the
    # rest of the suite. bind_ctx records the original binding and restores it
    # on the way out. It has to be bound to a REAL database on the same file,
    # not a deferred one: Model.bind() also sets the model's schema manager
    # database, which the migrator does not update, so a deferred placeholder
    # would leave DDL with nothing to execute against.
    db_file = os.path.join(settings.get_config_path(), runtimepaths.DATABASE_FILE_NAME)
    os.makedirs(settings.get_config_path(), exist_ok=True)
    previous_db = db_proxy.obj
    scratch_db = SqliteDatabase(db_file)
    models = _all_models()
    db_connection = None
    service_instance = None
    binding = scratch_db.bind_ctx(models)
    binding.__enter__()
    try:
        # Exactly what service.run() does: open the database, run the real
        # migrations, then start the threads.
        db_connection = service.init_db(settings.get_config_path())

        from trawlarr.libs.plugins import PluginsHandler

        plugin_handler = PluginsHandler()
        assert plugin_handler.write_plugin_data_to_db(
            dict(PLUGIN_INFO, plugin_id=PLUGIN_ID),
            os.path.join(settings.get_plugins_path(), PLUGIN_ID)), "Could not register the stub plugin"

        WorkerGroup.create({'name': 'e2e', 'locked': False, 'number_of_workers': 1,
                            'tags': [], 'worker_event_schedules': []})

        library = Library.create({'name': 'E2E', 'path': str(library_path)})
        library.set_enable_scanner(True)
        library.save()
        library.set_enabled_plugins([{'plugin_id': PLUGIN_ID}])

        service_instance = service.RootService()
        monkeypatch.setattr(service.RootService, 'start_ui_server',
                            lambda self, data_queues, foreman: self.register_thread('UIServer', _StubUIServer()))
        service_instance.start_threads(settings)

        yield Pipeline(service_instance, settings, str(library_path), library.get_id(), str(cache_path))
    finally:
        if service_instance is not None:
            try:
                service_instance.stop_threads()
            except Exception:
                pass
        if db_connection is not None:
            try:
                db_connection.stop()
            except Exception:
                pass
        binding.__exit__(None, None, None)
        scratch_db.close()
        db_proxy.initialize(previous_db)
        for module_name in [m for m in sys.modules if m == PLUGIN_ID or m.startswith(PLUGIN_ID + '.')]:
            sys.modules.pop(module_name, None)
        plugins_path = settings.get_plugins_path()
        if plugins_path in sys.path:
            sys.path.remove(plugins_path)
        _reset_singletons()


def _library_file(pipeline_ctx, name, marker=SOURCE_MARKER):
    path = os.path.join(pipeline_ctx.library_path, name)
    with open(path, 'wb') as f:
        f.write(b'header;' + marker + b';trailer')
    return path


@pytest.mark.integrationtest
class TestTheWholePipeline:
    """
    Discovery -> queue -> worker -> post-processor -> done, in one process.
    """

    def test_a_file_in_the_library_is_discovered_queued_processed_and_recorded_done(self, pipeline):
        """
        The headline test of issue #84.

        Verified red before being accepted, each mutation run against THIS
        file alone (`devops/mutation_check.py`, see the PR body for output):

          * deleting ``self.record_completed_file()`` from
            ``PostProcessor.run()`` - fails at the done-state assertion below.
          * deleting ``self.start_handler(...)`` from
            ``RootService.start_threads()`` - nothing turns a discovered path
            into a task and it fails at "queued as a task".

        Two of the mutations the issue leads with are caught by the OTHER
        tests in this file, not by this one, and it is worth being exact
        about which:

          * the plugins-directory bug (``PluginExecutor`` resolving
            ``.unmanic``) is caught by
            ``test_the_running_application_loads_plugins_from_the_configured_directory``.
            It is a call-site pin, not a pipeline assertion - the pipeline
            resolves plugin modules from the path stored in the database, so
            this path never crosses that seam. Measured: with the mutation
            applied, this test alone passes.
          * the allow-list mutation is caught by the next test rather than this
        one; the numbers are in its docstring.
        """
        from trawlarr.libs import donestate
        from trawlarr.libs.unmodels import CompletedTasks, Tasks

        target = _library_file(pipeline, 'episode.mkv')

        # 1. Discovered by the library scanner and queued by the task handler.
        _wait_for(lambda: Tasks.select().where(Tasks.abspath == target).count() > 0,
                  "the file to be queued as a task")

        # 2. Processed by a worker and post-processed back into the library.
        #    The command the stub plugin asked for is what rewrote the marker,
        #    so this asserts the subprocess ran AND that its output was moved
        #    into the library, not just that a task existed.
        _wait_for(lambda: open(target, 'rb').read().find(PROCESSED_MARKER) >= 0,
                  "the processed output to be delivered into the library")

        # 3. Recorded in history as a successful task.
        completed = _wait_for(
            lambda: CompletedTasks.select().where(CompletedTasks.abspath == target).first(),
            "the completed task to be written to history")
        assert completed.task_success is True, (
            "The task finished but was recorded as a failure. Worker or "
            "post-processor rejected the output.")

        # 4. Recorded as done, so the next library scan does not offer it back
        #    to the plugins for ever (issue #33).
        already_done, message = donestate.file_is_already_completed(target)
        assert already_done is True, (
            "The pipeline delivered and historised the file but never recorded "
            "it as done. The next library scan queues it again, and the one "
            "after that.")
        assert message

        # 5. The in-flight task row is gone - the post-processor deleted it.
        _wait_for(lambda: Tasks.select().where(Tasks.abspath == target).count() == 0,
                  "the finished task to be removed from the pending task list")

    def test_a_file_outside_the_library_allow_list_is_never_queued(self, pipeline):
        """
        The allow-list, end to end, through the running application.

        This is the bug the issue leads with. With ``mkv`` allowed, a ``.txt``
        in the same directory must never become a task even though the stub
        plugin votes to queue everything it is shown - tier 0 stops it before
        the plugin tier is reached.

        Verified red: making ``Library.get_file_extension_allowlist`` return
        ``[]`` unconditionally - one of the four mutations that left the unit
        suite green at 450/450 - run against this file alone gives
        `1 failed, 2 passed`, and the one that fails is this test. The .txt
        file is queued and processed alongside the .mkv.
        """
        from trawlarr.libs.library import Library
        from trawlarr.libs.unmodels import Tasks

        library = Library(pipeline.library_id)
        library.set_file_extension_allowlist(['mkv'])
        library.save()

        allowed = _library_file(pipeline, 'in-scope.mkv')
        excluded = _library_file(pipeline, 'out-of-scope.txt')

        # The .mkv going all the way through is the synchronisation point:
        # by the time it is processed, the scanner has walked the whole
        # directory and the file testers have seen both files.
        _wait_for(lambda: open(allowed, 'rb').read().find(PROCESSED_MARKER) >= 0,
                  "the in-scope file to be processed")

        assert Tasks.select().where(Tasks.abspath == excluded).count() == 0, (
            "A file whose extension is not in the library's allow-list was "
            "queued anyway. The allow-list is not reaching the queueing "
            "decision in the running application.")
        with open(excluded, 'rb') as f:
            assert SOURCE_MARKER in f.read(), \
                "The out-of-scope file was modified by the pipeline"

    def test_the_running_application_loads_plugins_from_the_configured_directory(self, pipeline):
        """
        The second escaped bug, pinned at the seam rather than at the unit.

        ``PluginExecutor()`` is constructed with no argument in ten places. If
        its default resolves anywhere other than the directory the application
        installs plugins into, every one of those call sites silently sees an
        empty plugin set - which is indistinguishable, to a unit test, from an
        installation that has no plugins.
        """
        from trawlarr.libs.plugins import PluginsHandler
        from trawlarr.libs.unplugins import PluginExecutor

        assert PluginExecutor().plugins_directory == pipeline.settings.get_plugins_path()

        modules = PluginsHandler().get_enabled_plugin_modules_by_type(
            'worker.process', library_id=pipeline.library_id)
        assert [m.get('plugin_id') for m in modules] == [PLUGIN_ID], (
            "The running application cannot see the plugin that is installed "
            "and enabled on its library.")
