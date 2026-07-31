#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_safety_mechanism_call_sites.py

    Six safety mechanisms in this application were each well tested as a
    unit and, at the same time, could be disconnected from the running
    program by editing a single line without one test going red:

      * the worker stall detector, invoked from WorkerSubprocessMonitor.run()
      * the #33 done-state write, invoked from PostProcessor.run()
      * the legacy config directory start-up guard, invoked from main()
      * the legacy UNMANIC_* env var warning, invoked from main()
      * FileTest.__init__, which is where ignore_completed_files (#41) is
        actually wired to the object
      * plugin_registration._default_plugins_directory(), which is where the
        ~/.unmanic/plugins path bug reproduced, unseen, for a third time

    A mechanism nothing calls is a mechanism that does not exist, and the
    failure is silent by construction: hung workers are never killed, files
    are reprocessed forever, an unmigrated install comes up looking empty.
    None of that raises. These tests exist to make the disconnection loud.

    Where the machinery could be driven for a real behavioural assertion it
    was - one iteration of each loop, real objects, real database rows.
    Where standing it up would have cost more than it proved (main(), which
    parses argv and starts the whole application), the call is pinned by
    reading main()'s AST for the call itself, which a comment or a dead
    string cannot satisfy.
"""
import ast
import inspect
import logging
import os
import textwrap
import threading
from unittest import mock

import pytest

from trawlarr import config as config_module
from trawlarr.config import Config
from trawlarr.libs import donestate, plugin_registration, runtimepaths
from trawlarr.libs.filetest import FileTest
from trawlarr.libs.postprocessor import PostProcessor
from trawlarr.libs.workers import WorkerSubprocessMonitor


# ---------------------------------------------------------------------------
# 1. The stall detector is actually consulted by the monitor loop
# ---------------------------------------------------------------------------

class _BoundedEvent:
    """threading.Event whose wait() stops the loop under test.

    The loop is a `while True`. If the call site being pinned is deleted,
    nothing else in the loop will ever set the stop flag, and the test would
    hang rather than fail. Counting waits bounds the loop from the outside,
    so a disconnected call site produces a failed assertion instead.
    """

    def __init__(self, stop_after, stop_flag):
        self.waits = 0
        self.stop_after = stop_after
        self.stop_flag = stop_flag

    def wait(self, _timeout=None):
        self.waits += 1
        if self.waits >= self.stop_after:
            self.stop_flag.set()
        return True

    def is_set(self):
        return False

    def set(self):
        pass

    def clear(self):
        pass


def _monitor_for_run_loop(stall_result):
    """A WorkerSubprocessMonitor whose run() can be driven for a few ticks.

    __init__ needs a real parent Worker, so build it bare and set only the
    attributes run() touches - the same approach the stall detector tests
    take.
    """
    monitor = WorkerSubprocessMonitor.__new__(WorkerSubprocessMonitor)
    monitor.logger = logging.getLogger('test_safety_mechanism_call_sites')
    monitor.parent_worker = mock.Mock()
    monitor._stop_event = threading.Event()
    monitor.event = _BoundedEvent(stop_after=4, stop_flag=monitor._stop_event)
    monitor.redundant_flag = threading.Event()
    monitor.paused_flag = threading.Event()
    monitor.paused = False
    monitor._pause_time_counter = None
    monitor.subprocess_pause_time = 0
    monitor.subprocess_pid = 4242
    monitor.subprocess = mock.Mock()
    monitor.subprocess.is_running.return_value = True
    monitor.terminate_proc = mock.Mock()
    monitor.suspend_proc = mock.Mock()
    monitor.resume_proc = mock.Mock()
    monitor.set_proc_resources_in_parent_worker = mock.Mock()

    proc = mock.Mock()
    proc.cpu_percent.return_value = 0.0
    proc.memory_info.return_value = mock.Mock(rss=1024, vms=2048)
    monitor.get_tracked_processes = mock.Mock(return_value=[proc])

    calls = []

    def fake_check_for_stall(tracked_procs):
        calls.append(tracked_procs)
        return stall_result

    monitor._WorkerSubprocessMonitor__check_for_stall = fake_check_for_stall
    return monitor, calls, proc


@pytest.mark.unittest
class TestTheMonitorLoopConsultsTheStallDetector:
    """`if self.__check_for_stall(tracked_procs):` in
    WorkerSubprocessMonitor.run(). Replace it with `if False:` and every
    stall detector test still passes while no hung worker is ever killed
    again."""

    def test_each_pass_of_the_loop_checks_for_a_stall(self):
        monitor, calls, proc = _monitor_for_run_loop(stall_result=False)

        monitor.run()

        assert calls, (
            "WorkerSubprocessMonitor.run() completed several passes without "
            "once asking the stall detector about the running subprocess. "
            "Hung workers are never killed."
        )
        assert calls[0] == [proc], (
            "The stall detector was called, but not with the tracked process "
            "tree the loop just measured."
        )

    def test_a_detected_stall_short_circuits_the_rest_of_the_pass(self):
        """The detector's return value has to be honoured, not just the call
        made: it kills and reports the subprocess itself, so the loop must
        skip straight to the next pass rather than fall through into the
        pause/resume handling for a process that is on its way out."""
        monitor, calls, _proc = _monitor_for_run_loop(stall_result=True)
        monitor.paused_flag.set()

        monitor.run()

        assert calls, "The stall detector was never consulted"
        monitor.suspend_proc.assert_not_called()


# ---------------------------------------------------------------------------
# 2. PostProcessor.run() records the completed file (#33)
# ---------------------------------------------------------------------------

@pytest.fixture
def completion_db():
    """Bind FileCompletionState to a throwaway in-memory database."""
    from peewee import SqliteDatabase

    from trawlarr.libs.unmodels import FileCompletionState

    database = SqliteDatabase(':memory:')
    with database.bind_ctx([FileCompletionState]):
        database.create_tables([FileCompletionState])
        yield database
    database.close()


class _LocalTask:
    """Minimal stand-in for the task run() pulls off the processed queue."""

    def __init__(self, abspath):
        self.abspath = abspath
        self.deleted = False

    def get_task_id(self):
        return 7

    def get_task_library_id(self):
        return 1

    def get_task_type(self):
        return 'local'

    def get_task_success(self):
        return True

    def get_source_abspath(self):
        return self.abspath

    def get_source_data(self):
        return {'abspath': self.abspath}

    def get_destination_data(self):
        return {'abspath': self.abspath}

    def get_cache_path(self):
        return '/tmp/cache/{}'.format(os.path.basename(self.abspath))

    def delete(self):
        self.deleted = True


class _OneTaskQueue:
    """Delivers exactly one processed task, then reports itself empty."""

    def __init__(self, task):
        self.task = task
        self.handed_out = False

    def task_list_processed_is_empty(self):
        return self.handed_out

    def get_next_processed_tasks(self):
        self.handed_out = True
        return self.task


@pytest.mark.unittest
class TestThePostProcessorRecordsCompletedFiles:
    """`self.record_completed_file()` in PostProcessor.run(). Replace it with
    `pass` and the whole suite stays green while the #33 reprocess loop comes
    back: every file is offered to the plugins again on the next scan,
    forever, with nothing in the logs to say so."""

    def _drive_one_task(self, tmp_path, monkeypatch, delivered_file):
        """Run one full pass of PostProcessor.run() over one local task."""
        task = _LocalTask(str(delivered_file))

        pp = PostProcessor.__new__(PostProcessor)
        pp.logger = logging.getLogger('test_safety_mechanism_call_sites')
        pp.abort_flag = threading.Event()
        pp.event = _BoundedEvent(stop_after=25, stop_flag=pp.abort_flag)
        pp.task_queue = _OneTaskQueue(task)
        pp.current_task = None
        pp._last_destination_files = []
        pp._last_file_move_processes_success = False
        pp.system_configuration_is_valid = lambda: True

        # post_process_file() is the writer of _last_destination_files and of
        # the file-move verdict. The real one copies files around and runs
        # plugins; what matters here is only what it leaves behind for
        # record_completed_file() to read. This test is about the call site
        # existing at all, so it stands in for a delivery that worked - the
        # failed-move case is tested in test_postprocessor_failed_move.py.
        def fake_post_process_file():
            pp._last_destination_files = [str(delivered_file)]
            pp._last_file_move_processes_success = True

        pp.post_process_file = fake_post_process_file
        pp.write_history_log = lambda: None
        pp.commit_task_metadata = lambda: None

        # The event plugin runners at the top of the loop reach for the
        # plugin tables.
        monkeypatch.setattr('trawlarr.libs.postprocessor.PluginsHandler',
                            lambda *a, **kw: mock.Mock())

        pp.run()
        return task

    def test_a_completed_task_leaves_the_file_marked_done(self, tmp_path, monkeypatch, completion_db):
        """The behavioural assertion: after the loop has post-processed a
        local task, the file it delivered answers "already completed" to the
        very check that keeps it out of the next scan."""
        delivered = tmp_path / 'delivered.mkv'
        delivered.write_bytes(b'a delivered library file')

        assert donestate.file_is_already_completed(str(delivered))[0] is False

        task = self._drive_one_task(tmp_path, monkeypatch, delivered)

        assert task.deleted, "The task was never processed - the loop did not run"
        completed, message = donestate.file_is_already_completed(str(delivered))
        assert completed is True, (
            "PostProcessor.run() finished a successful task without recording "
            "its delivered file as completed. The next library scan will queue "
            "it again, and the one after that (#33)."
        )
        assert message, "A completed file must carry a message explaining why it was skipped"


# ---------------------------------------------------------------------------
# 3 & 4. main() runs the legacy-install guards
# ---------------------------------------------------------------------------

def _calls_made_by(func):
    """Names of the functions called directly in `func`'s body.

    Read from the AST rather than the source text: a call that has been
    commented out, or moved into a docstring, is not a call, and a
    substring check cannot tell the difference.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.append(node.func.attr)
    return names


@pytest.mark.unittest
@pytest.mark.unittest
class TestThePostProcessorRunsTheConvergenceCheck:
    """Issue #34's check is only worth having if the loop calls it.

    Added because deleting `self.run_convergence_check()` from
    PostProcessor.run() left the whole suite green at 739 passed. The
    convergence module itself is covered thoroughly -- 34 tests, and
    breaking `evaluate_path` fails 15 of them -- but nothing asserted the
    application ever asks the question. That is the same shape as the four
    mechanisms this module was written to pin, arriving in the very PR that
    adds a new one.

    Source-level rather than behavioural: driving a real convergence
    verdict needs a bound state database, a library, patched file-test
    plugins and a probe, all of which
    tests/unit/test_convergence_detection.py already sets up properly. The
    only thing missing was the wiring, so that is what this asserts.
    """

    def test_the_run_loop_calls_the_convergence_check(self):
        source = inspect.getsource(PostProcessor.run)
        assert 'self.run_convergence_check()' in source, (
            "PostProcessor.run() no longer runs the convergence check, so a "
            "task that completes without satisfying the library's criteria "
            "goes unnoticed again -- which is issue #34 in full."
        )

    def test_the_convergence_check_runs_after_completion_is_recorded(self):
        """Order matters. #33 records the file as done; convergence asks
        whether that file actually satisfies the criteria. Asking first
        would evaluate a file the post-processor has not finished with."""
        source = inspect.getsource(PostProcessor.run)
        assert source.index('self.record_completed_file()') < source.index('self.run_convergence_check()'), (
            "the convergence check runs before completion is recorded; it "
            "should evaluate the delivered file, not the in-flight one"
        )


class TestStartUpRunsTheLegacyInstallGuards:
    """Both guards are thoroughly tested as functions (test_runtime_paths.py,
    test_env_vars.py) and both were called from exactly one unpinned line in
    main(). Deleting either line is invisible to the suite, and invisible in
    production too - that is the entire failure mode being defended against
    here.

    main() parses argv, builds a Config and starts the application, so it is
    pinned at its call sites rather than driven.
    """

    def test_main_refuses_to_start_against_an_unmigrated_unmanic_install(self):
        from trawlarr import service

        assert 'guard_against_legacy_config_directory' in _calls_made_by(service.main), (
            "main() no longer calls guard_against_legacy_config_directory(). "
            "An upgrade with a populated ~/.{} and no ~/.{} will start "
            "cleanly and present itself as a fresh install, with every "
            "library, plugin and setting apparently gone.".format(
                runtimepaths.LEGACY_APP_DIR_NAME, runtimepaths.APP_DIR_NAME)
        )

    def test_main_warns_about_ignored_legacy_environment_variables(self):
        from trawlarr import service

        assert 'warn_about_legacy_env_vars' in _calls_made_by(service.main), (
            "main() no longer calls warn_about_legacy_env_vars(). Every "
            "UNMANIC_* variable an operator still sets is then ignored in "
            "total silence."
        )

    def test_the_guards_run_before_the_configuration_is_built(self):
        """Ordering is part of the mechanism: Config() creates the new
        configuration directory, so a guard that runs after it can no longer
        tell an unmigrated install from a fresh one."""
        from trawlarr import service

        calls = _calls_made_by(service.main)
        assert calls.index('guard_against_legacy_config_directory') < calls.index('Config')
        assert calls.index('warn_about_legacy_env_vars') < calls.index('Config')


# ---------------------------------------------------------------------------
# 5. FileTest.__init__ wires up ignore_completed_files (#41)
# ---------------------------------------------------------------------------

@pytest.fixture
def constructible_file_test(monkeypatch, tmp_path):
    """Make FileTest.__init__ runnable off a real database and plugin set.

    Every existing FileTest test builds the object with __new__ and sets the
    attributes by hand, which is why the constructor - the only place the
    ignore_completed_files argument is ever wired to anything - had no test
    executing it at all.
    """
    monkeypatch.setenv('HOME', str(tmp_path))
    registry = type(config_module.Config)._instances
    registry.pop(config_module.Config, None)

    handler = mock.Mock()
    handler.get_enabled_plugin_modules_by_type.return_value = []
    monkeypatch.setattr('trawlarr.libs.filetest.PluginsHandler', lambda *a, **kw: handler)

    library = mock.Mock()
    library.get_file_extension_allowlist.return_value = ['mkv']
    monkeypatch.setattr('trawlarr.libs.library.Library', lambda *a, **kw: library)

    yield
    registry.pop(config_module.Config, None)


@pytest.mark.unittest
class TestFileTestConstructorWiring:
    """`self.ignore_completed_files = ignore_completed_files` is the whole of
    the #41 reprocess seam. Nothing executed FileTest.__init__, so the
    argument could be dropped on the floor - callers asking for a
    reprocess would be silently refused, which is precisely the direction
    #33's done-state check makes unrecoverable without it."""

    def test_the_constructor_argument_reaches_the_completed_check(self, constructible_file_test, tmp_path):
        target = tmp_path / 'already-done.mkv'
        target.write_bytes(b'processed last week')

        with mock.patch.object(donestate, 'file_is_already_completed',
                               return_value=(True, 'already completed')):
            default = FileTest(library_id=1)
            reprocessing = FileTest(library_id=1, ignore_completed_files=True)

            assert default.file_already_completed_successfully(str(target)) == (True, 'already completed'), (
                "A file recorded as completed is no longer being skipped"
            )
            assert reprocessing.file_already_completed_successfully(str(target)) == (False, ''), (
                "FileTest(ignore_completed_files=True) still refused the file. "
                "The #41 reprocess seam is not wired to the constructor "
                "argument, and a deliberate reprocess is impossible."
            )

    def test_the_default_is_to_honour_completed_state(self, constructible_file_test):
        assert FileTest(library_id=1).ignore_completed_files is False


# ---------------------------------------------------------------------------
# 6. The registration validator's default plugins directory
# ---------------------------------------------------------------------------

@pytest.mark.unittest
class TestValidatorDefaultPluginsDirectory:
    """_default_plugins_directory() was never executed by any test, and the
    ~/.unmanic/plugins path bug reproduces inside it. That bug has shipped
    three times; the cross-check that catches it is comparing against what
    the application actually uses."""

    def test_it_matches_where_the_application_installs_plugins(self, tmp_path, monkeypatch):
        """Config is a singleton, so an earlier test may have built one
        against a different HOME. Drop it from the registry before and after
        so this measures the patched environment and does not leak."""
        monkeypatch.setenv('HOME', str(tmp_path))
        registry = type(config_module.Config)._instances
        registry.pop(config_module.Config, None)
        try:
            assert plugin_registration._default_plugins_directory() == Config().get_plugins_path()
        finally:
            registry.pop(config_module.Config, None)

    def test_it_is_under_the_current_app_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv('HOME', str(tmp_path))
        registry = type(config_module.Config)._instances
        registry.pop(config_module.Config, None)
        try:
            directory = plugin_registration._default_plugins_directory()
        finally:
            registry.pop(config_module.Config, None)

        assert directory, "The validator could not resolve a plugins directory at all"
        parts = directory.split(os.sep)
        assert runtimepaths.APP_DIR_NAME in parts
        assert runtimepaths.LEGACY_APP_DIR_NAME not in parts, (
            "The validator is inspecting ~/.{}/plugins - the bug that has "
            "already shipped three times. It would report every installed "
            "plugin as missing from disk.".format(runtimepaths.LEGACY_APP_DIR_NAME)
        )

    def test_it_reports_no_directory_rather_than_raising(self, monkeypatch):
        """Callers treat None as 'no directory check possible'. A diagnostic
        that raises takes the startup path down with it."""
        def boom():
            raise RuntimeError('no config')

        monkeypatch.setattr(config_module, 'Config', boom)
        assert plugin_registration._default_plugins_directory() is None
