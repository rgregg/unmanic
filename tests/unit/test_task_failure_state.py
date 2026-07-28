#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_task_failure_state.py

    Tests for durable task failure state (issue #25).

    The failure this suite exists to catch is the original one: a task that
    fails, is recorded as a bare `task_success = False`, and leaves the user
    with no reason, no count and nothing that survives a restart.
"""
import datetime
import logging
import os
import threading
from unittest import mock

import peewee
import pytest

from trawlarr.libs import taskfailure
from trawlarr.libs.task import TaskDataStore
from trawlarr.libs.unmodels import CompletedTasks, CompletedTasksCommandLogs
from trawlarr.libs.unmodels.lib.basemodel import db
from trawlarr.libs.workers import TASK_FAILURE_STATE_KEY, Worker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path):
    """Bind the model proxy to a throwaway SQLite file."""
    previous = db.obj
    database = peewee.SqliteDatabase(str(tmp_path / 'trawlarr.db'))
    db.initialize(database)
    database.connect(reuse_if_open=True)
    database.create_tables([CompletedTasks, CompletedTasksCommandLogs])
    yield database
    database.close()
    db.initialize(previous)


def _run_schema_update(db_file):
    """Run the real startup schema update against a database file.

    The models resolve their database through the proxy, exactly as they do
    at runtime, so the proxy has to be bound for the duration.
    """
    from trawlarr.libs.db_migrate import Migrations

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    migrations = Migrations({
        'TYPE':                       'SQLITE',
        'FILE':                       str(db_file),
        'MIGRATIONS_DIR':             os.path.join(repo_root, 'trawlarr', 'migrations_v1'),
        'MIGRATIONS_HISTORY_VERSION': 'test',
    })
    previous = db.obj
    db.initialize(migrations.database)
    try:
        migrations.update_schema()
    finally:
        migrations.database.close()
        db.initialize(previous)


def _completed(abspath, success, finish, **kwargs):
    """Create a completed_tasks row."""
    values = {
        'task_label':          os.path.basename(abspath),
        'abspath':             abspath,
        'task_success':        success,
        'start_time':          finish,
        'finish_time':         finish,
        'processed_by_worker': 'Worker-1',
    }
    values.update(kwargs)
    return CompletedTasks.create(**values)


def _at(minute):
    return datetime.datetime(2026, 7, 27, 12, minute, 0)


# ---------------------------------------------------------------------------
# The record itself, and its coordination with #15
# ---------------------------------------------------------------------------

class TestFailureRecord:

    def test_record_is_stored_under_the_key_15_left(self):
        """#15 left TASK_FAILURE_STATE_KEY as the seam. #25 must use it, not
        invent a parallel one - two notions of 'failed' would be the silent
        failure bug this milestone is about."""
        task_id = 100001
        TaskDataStore.clear_task(task_id)
        taskfailure.record(task_id, taskfailure.CATEGORY_PLUGIN_ERROR, 'plugin blew up')
        raw = TaskDataStore.get_task_state(TASK_FAILURE_STATE_KEY, task_id=task_id)
        TaskDataStore.clear_task(task_id)
        assert raw is not None
        assert raw['category'] == 'plugin_error'
        assert raw['message'] == 'plugin blew up'
        assert raw['timestamp'] > 0

    def test_the_stall_survives_the_non_zero_exit_it_causes(self):
        """The stall detector kills the subprocess; the command then exits
        non-zero and the ordinary path records 'command_failed'. The stall is
        the cause and the exit code is its symptom, so the first record must
        win, or every stall is reported as a generic command failure."""
        task_id = 100002
        TaskDataStore.clear_task(task_id)
        taskfailure.record(task_id, taskfailure.CATEGORY_STALLED, 'no sign of life for 300s')
        taskfailure.record(task_id, taskfailure.CATEGORY_COMMAND_FAILED, 'exited with status 137')
        record = taskfailure.read(task_id)
        TaskDataStore.clear_task(task_id)
        assert record['category'] == 'stalled', \
            "The stall was overwritten by the non-zero exit it caused"
        assert 'no sign of life' in record['message']

    def test_a_later_reason_is_still_visible_in_the_log(self):
        """Keeping the first record must not throw the second reason away."""
        task_id = 100003
        TaskDataStore.clear_task(task_id)
        log = []
        taskfailure.record(task_id, taskfailure.CATEGORY_STALLED, 'wedged', worker_log=log)
        taskfailure.record(task_id, taskfailure.CATEGORY_COMMAND_FAILED, 'exited 137', worker_log=log)
        TaskDataStore.clear_task(task_id)
        joined = ''.join(log)
        assert 'TASK FAILED [STALLED]' in joined
        assert 'exited 137' in joined

    def test_overwrite_replaces_the_record(self):
        """The sanity check decides the outcome of a task the worker thought
        had succeeded, so it is allowed to overwrite."""
        task_id = 100004
        TaskDataStore.clear_task(task_id)
        taskfailure.record(task_id, taskfailure.CATEGORY_COMMAND_FAILED, 'first')
        taskfailure.record(task_id, taskfailure.CATEGORY_SANITY_CHECK, 'output grew again', overwrite=True)
        record = taskfailure.read(task_id)
        TaskDataStore.clear_task(task_id)
        assert record['category'] == 'sanity_check'

    def test_unknown_categories_are_normalised(self):
        assert taskfailure.normalise_category('not-a-real-category') == 'unknown'
        assert taskfailure.normalise_category(None) == 'unknown'
        assert taskfailure.normalise_category('STALLED') == 'stalled'

    def test_long_messages_are_truncated_not_dropped(self):
        message = 'x' * (taskfailure.MAX_MESSAGE_LENGTH + 500)
        result = taskfailure.truncate_message(message)
        assert len(result) == taskfailure.MAX_MESSAGE_LENGTH
        assert result.endswith('...')

    def test_recording_never_raises(self):
        """Called from the subprocess monitor thread. A failure while
        recording a failure must not become a larger failure."""
        with mock.patch.object(TaskDataStore, 'set_task_state', side_effect=RuntimeError('boom')):
            record = taskfailure.record(100005, taskfailure.CATEGORY_STALLED, 'wedged')
        assert record['category'] == 'stalled'


class TestWorkerSeam:

    def _bare_worker(self, task_id):
        worker = Worker.__new__(Worker)
        object.__setattr__(worker, '_name', 'Worker-Test')
        worker._initialized = True
        worker.logger = logging.getLogger('test_task_failure_state')
        worker.worker_log = []
        worker.current_task = mock.Mock()
        worker.current_task.get_task_id.return_value = task_id
        return worker

    def test_worker_record_task_failure_delegates_to_the_shared_store(self):
        task_id = 100010
        TaskDataStore.clear_task(task_id)
        worker = self._bare_worker(task_id)
        worker.record_task_failure(taskfailure.CATEGORY_PLUGIN_ERROR, 'runner returned False')
        record = taskfailure.read(task_id)
        TaskDataStore.clear_task(task_id)
        assert record['category'] == 'plugin_error'
        assert 'TASK FAILED [PLUGIN_ERROR]' in ''.join(worker.worker_log)

    def test_stall_reporting_still_records_the_stalled_category(self):
        """Regression guard on #15's behaviour after this refactor."""
        task_id = 100011
        TaskDataStore.clear_task(task_id)
        worker = self._bare_worker(task_id)
        worker.report_subprocess_stalled('subprocess PID 1 showed no sign of life')
        record = taskfailure.read(task_id)
        TaskDataStore.clear_task(task_id)
        assert record['category'] == 'stalled'


# ---------------------------------------------------------------------------
# The worker call sites
# ---------------------------------------------------------------------------

def _runner_worker(task_id, tmp_path):
    worker = Worker.__new__(Worker)
    object.__setattr__(worker, '_name', 'Worker-Test')
    worker._initialized = True
    worker.logger = logging.getLogger('test_task_failure_state')
    worker.worker_log = []
    worker.worker_runners_info = {}
    worker.event = threading.Event()
    worker.event.set()
    worker.redundant_flag = threading.Event()
    worker.paused_flag = threading.Event()
    worker.current_command_ref = None
    worker.worker_group_id = 1
    worker.worker_subprocess_monitor = mock.Mock()
    worker.worker_subprocess_monitor.default_progress_parser = None

    source = tmp_path / 'source.mkv'
    source.write_bytes(b'source')

    worker.current_task = mock.Mock()
    worker.current_task.get_task_id.return_value = task_id
    worker.current_task.get_task_library_id.return_value = 1
    worker.current_task.get_task_library_name.return_value = 'TV'
    worker.current_task.get_source_abspath.return_value = str(source)
    worker.current_task.get_cache_path.return_value = str(tmp_path / 'cache' / 'out.mkv')
    return worker


def _run_runners(worker, plugin_runner_result, exec_command=None, command_success=True):
    """Drive Worker.__exec_worker_runners_on_set_task with one fake plugin."""
    plugin_module = {
        'plugin_id':   'fake_plugin',
        'name':        'Fake Plugin',
        'author':      'test',
        'version':     '1',
        'icon':        '',
        'description': '',
    }
    handler = mock.Mock()
    handler.get_enabled_plugin_modules_by_type.return_value = [plugin_module]
    handler.run_event_plugins_for_plugin_type.return_value = None

    def _exec_plugin_runner(data, runner_id, runner_type):
        if exec_command is not None:
            data['exec_command'] = list(exec_command)
        return plugin_runner_result

    handler.exec_plugin_runner.side_effect = _exec_plugin_runner

    with mock.patch('trawlarr.libs.workers.PluginsHandler', return_value=handler), \
            mock.patch('trawlarr.libs.workers.Library') as fake_library, \
            mock.patch.object(Worker, '_Worker__exec_command_subprocess', return_value=command_success):
        fake_library.return_value.get_path.return_value = '/library'
        return worker._Worker__exec_worker_runners_on_set_task()


class TestWorkerCallSites:

    def test_a_failed_plugin_records_a_plugin_error(self, tmp_path):
        task_id = 100020
        TaskDataStore.clear_task(task_id)
        worker = _runner_worker(task_id, tmp_path)
        result = _run_runners(worker, plugin_runner_result=False)
        record = taskfailure.read(task_id)
        TaskDataStore.clear_task(task_id)
        assert result is False
        assert record is not None, "A failed plugin left no durable reason"
        assert record['category'] == 'plugin_error'
        assert 'Fake Plugin' in record['message']

    def test_a_non_zero_command_records_a_command_failure(self, tmp_path):
        task_id = 100021
        TaskDataStore.clear_task(task_id)
        worker = _runner_worker(task_id, tmp_path)
        result = _run_runners(worker, plugin_runner_result=True,
                              exec_command=['ffmpeg', '-i', 'in.mkv'], command_success=False)
        record = taskfailure.read(task_id)
        TaskDataStore.clear_task(task_id)
        assert result is False
        assert record is not None, "A non-zero command left no durable reason"
        assert record['category'] == 'command_failed'

    def test_a_stall_is_not_relabelled_by_the_command_failure_it_causes(self, tmp_path):
        """End to end version of the coordination with #15: the monitor
        records the stall, then the killed command's non-zero exit runs
        through the ordinary failure path."""
        task_id = 100022
        TaskDataStore.clear_task(task_id)
        worker = _runner_worker(task_id, tmp_path)
        worker.report_subprocess_stalled('no sign of life for 300 seconds')
        _run_runners(worker, plugin_runner_result=True,
                     exec_command=['ffmpeg', '-i', 'in.mkv'], command_success=False)
        record = taskfailure.read(task_id)
        TaskDataStore.clear_task(task_id)
        assert record['category'] == 'stalled'

    def test_a_successful_task_records_no_failure(self, tmp_path):
        """A check that fires on healthy work is worse than no check."""
        task_id = 100023
        TaskDataStore.clear_task(task_id)
        worker = _runner_worker(task_id, tmp_path)
        _run_runners(worker, plugin_runner_result=True)
        record = taskfailure.read(task_id)
        TaskDataStore.clear_task(task_id)
        assert record is None, "A task that did not fail was given a failure reason"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestHistoryFields:

    def test_success_carries_no_failure_state(self, temp_db):
        fields = taskfailure.build_history_fields(100030, True, '/library/a.mkv')
        assert fields['failure_category'] is None
        assert fields['failure_message'] is None
        assert fields['failure_attempt'] is None

    def test_failure_with_a_record_carries_it(self, temp_db):
        task_id = 100031
        TaskDataStore.clear_task(task_id)
        taskfailure.record(task_id, taskfailure.CATEGORY_STALLED, 'no sign of life for 300s')
        fields = taskfailure.build_history_fields(task_id, False, '/library/a.mkv')
        TaskDataStore.clear_task(task_id)
        assert fields['failure_category'] == 'stalled'
        assert 'no sign of life' in fields['failure_message']
        assert isinstance(fields['failure_time'], datetime.datetime)
        assert fields['failure_attempt'] == 1

    def test_failure_without_a_record_says_so_rather_than_staying_silent(self, temp_db):
        fields = taskfailure.build_history_fields(100032, False, '/library/a.mkv')
        assert fields['failure_category'] == 'unknown'
        assert 'No component reported a reason' in fields['failure_message']

    def test_attempt_counts_consecutive_failures_of_the_same_file(self, temp_db):
        path = '/library/a.mkv'
        _completed(path, False, _at(1))
        _completed(path, False, _at(2))
        fields = taskfailure.build_history_fields(100033, False, path)
        assert fields['failure_attempt'] == 3

    def test_a_success_resets_the_attempt_count(self, temp_db):
        """A file that failed twice last month and has processed cleanly
        since is not a problem file. Counting it as one would put healthy
        work in the health view."""
        path = '/library/a.mkv'
        _completed(path, False, _at(1))
        _completed(path, False, _at(2))
        _completed(path, True, _at(3))
        fields = taskfailure.build_history_fields(100034, False, path)
        assert fields['failure_attempt'] == 1

    def test_other_files_do_not_contribute(self, temp_db):
        _completed('/library/other.mkv', False, _at(1))
        fields = taskfailure.build_history_fields(100035, False, '/library/a.mkv')
        assert fields['failure_attempt'] == 1


class TestPersistedAcrossRestart:

    def test_the_reason_is_read_back_from_the_database(self, temp_db, tmp_path):
        """'Preserve diagnostics across restart' - the record must be in the
        database, not only in the in-memory task data store."""
        from trawlarr.libs import history

        task_id = 100040
        TaskDataStore.clear_task(task_id)
        taskfailure.record(task_id, taskfailure.CATEGORY_SANITY_CHECK, 'output grew for the third time')

        task_data = {
            'task_label':          'a.mkv',
            'abspath':             '/library/a.mkv',
            'task_success':        False,
            'start_time':          _at(1),
            'finish_time':         _at(2),
            'processed_by_worker': 'Worker-1',
            'log':                 'some log',
        }
        task_data.update(taskfailure.build_history_fields(task_id, False, '/library/a.mkv'))
        assert history.History().save_task_history(task_data) is True

        # Simulate the restart: drop every scrap of in-memory state.
        TaskDataStore.clear_task(task_id)
        temp_db.close()
        temp_db.connect(reuse_if_open=True)

        row = CompletedTasks.select().where(CompletedTasks.abspath == '/library/a.mkv').get()
        assert row.failure_category == 'sanity_check'
        assert 'grew for the third time' in row.failure_message
        assert row.failure_attempt == 1
        assert row.failure_time is not None


class TestPostprocessorWiring:

    def test_write_history_log_persists_the_failure_fields(self, temp_db):
        """The postprocessor is the only place the in-memory record can be
        turned into a database row; if it does not do it, nothing does."""
        from trawlarr.libs import postprocessor

        task_id = 100050
        TaskDataStore.clear_task(task_id)
        taskfailure.record(task_id, taskfailure.CATEGORY_COMMAND_FAILED, 'ffmpeg exited 1')

        pp = postprocessor.PostProcessor.__new__(postprocessor.PostProcessor)
        pp.logger = logging.getLogger('test_task_failure_state')
        pp.current_task = mock.Mock()
        pp.current_task.task.success = False
        pp.current_task.get_task_id.return_value = task_id
        pp.current_task.get_task_library_id.return_value = 1
        pp.current_task.get_task_library_name.return_value = 'TV'
        pp.current_task.get_task_type.return_value = 'local'
        pp.current_task.get_source_data.return_value = {'abspath': '/library/a.mkv'}
        pp.current_task.get_destination_data.return_value = {'abspath': '/library/a.mkv'}
        pp.current_task.task_dump.return_value = {
            'task_label':          'a.mkv',
            'abspath':             '/library/a.mkv',
            'task_success':        False,
            'start_time':          _at(1),
            'finish_time':         _at(2),
            'processed_by_worker': 'Worker-1',
            'log':                 'log text',
        }
        pp._last_destination_files = []
        pp._last_file_move_processes_success = True

        with mock.patch('trawlarr.libs.postprocessor.Notifications'), \
                mock.patch('trawlarr.libs.postprocessor.PluginsHandler'), \
                mock.patch('trawlarr.libs.postprocessor.TrawlarrLogging'):
            pp.write_history_log()

        TaskDataStore.clear_task(task_id)
        row = CompletedTasks.select().where(CompletedTasks.abspath == '/library/a.mkv').get()
        assert row.failure_category == 'command_failed'
        assert 'ffmpeg exited 1' in row.failure_message


# ---------------------------------------------------------------------------
# Retry guard
# ---------------------------------------------------------------------------

class _Settings(object):
    def __init__(self, limit):
        self._limit = limit

    def get_max_consecutive_task_failures(self):
        return self._limit


class TestRetryGuard:

    def test_a_first_failure_may_be_retried(self, temp_db):
        """A check that fires on healthy work is worse than no check: a
        one-off failure must retry without argument."""
        path = '/library/a.mkv'
        _completed(path, False, _at(1))
        allowed, reason = taskfailure.evaluate_retry(path, settings=_Settings(3))
        assert allowed is True
        assert reason is None

    def test_a_file_that_keeps_failing_is_refused(self, temp_db):
        path = '/library/a.mkv'
        for minute in (1, 2, 3):
            _completed(path, False, _at(minute), failure_category='plugin_error',
                       failure_message='plugin blew up', failure_attempt=minute)
        allowed, reason = taskfailure.evaluate_retry(path, settings=_Settings(3))
        assert allowed is False, "A file that has failed identically 3 times was silently re-queued"
        assert '3 times in a row' in reason
        assert 'plugin blew up' in reason, "The refusal did not say why the file keeps failing"

    def test_the_refusal_can_be_forced(self, temp_db):
        """The user may have just fixed the plugin. Refusing permanently
        would be its own trap."""
        path = '/library/a.mkv'
        for minute in (1, 2, 3):
            _completed(path, False, _at(minute))
        allowed, reason = taskfailure.evaluate_retry(path, settings=_Settings(3), force=True)
        assert allowed is True
        assert reason is not None

    def test_a_success_clears_the_refusal(self, temp_db):
        path = '/library/a.mkv'
        for minute in (1, 2, 3):
            _completed(path, False, _at(minute))
        _completed(path, True, _at(4))
        allowed, _ = taskfailure.evaluate_retry(path, settings=_Settings(3))
        assert allowed is True

    def test_a_zero_limit_is_clamped(self, temp_db):
        """A mistyped limit of 0 would refuse every retry ever."""
        assert taskfailure.max_consecutive_failures(_Settings(0)) == 1

    def test_missing_settings_fall_back_to_the_default(self, temp_db):
        assert taskfailure.max_consecutive_failures(None) == taskfailure.DEFAULT_MAX_CONSECUTIVE_FAILURES

    def test_the_helper_refuses_and_reports_the_reason(self, temp_db, tmp_path):
        """The API helper must return the refusal rather than dropping it."""
        from trawlarr.webserver.helpers import completed_tasks

        real_file = tmp_path / 'a.mkv'
        real_file.write_bytes(b'x')
        path = str(real_file)
        rows = [_completed(path, False, _at(m), failure_category='plugin_error',
                           failure_message='plugin blew up') for m in (1, 2, 3)]

        with mock.patch.object(completed_tasks.config, 'Config', return_value=_Settings(3)), \
                mock.patch.object(completed_tasks.task, 'Task') as fake_task:
            errors = completed_tasks.add_historic_tasks_to_pending_tasks_list([rows[-1].id])
            assert fake_task.return_value.create_task_by_absolute_path.call_count == 0, \
                "A file past the failure limit was re-queued anyway"
        assert rows[-1].id in errors
        assert 'failed 3 times in a row' in errors[rows[-1].id]

    def test_forced_retry_reaches_the_queue(self, temp_db, tmp_path):
        from trawlarr.webserver.helpers import completed_tasks

        real_file = tmp_path / 'a.mkv'
        real_file.write_bytes(b'x')
        path = str(real_file)
        rows = [_completed(path, False, _at(m)) for m in (1, 2, 3)]

        with mock.patch.object(completed_tasks.config, 'Config', return_value=_Settings(3)), \
                mock.patch.object(completed_tasks.task, 'Task') as fake_task:
            fake_task.return_value.create_task_by_absolute_path.return_value = True
            errors = completed_tasks.add_historic_tasks_to_pending_tasks_list([rows[-1].id], force=True)
            assert fake_task.return_value.create_task_by_absolute_path.call_count == 1
        assert errors == {}


# ---------------------------------------------------------------------------
# Dismissal and the health view
# ---------------------------------------------------------------------------

class TestDismissalAndHealthView:

    def test_outstanding_summary_counts_undismissed_failures_by_category(self, temp_db):
        _completed('/library/a.mkv', False, _at(1), failure_category='stalled')
        _completed('/library/b.mkv', False, _at(2), failure_category='plugin_error')
        _completed('/library/c.mkv', False, _at(3), failure_category='plugin_error')
        _completed('/library/d.mkv', True, _at(4))
        summary = taskfailure.outstanding_failure_summary()
        assert summary['total'] == 3
        assert summary['categories'] == {'stalled': 1, 'plugin_error': 2}

    def test_dismissing_removes_a_failure_from_the_health_view(self, temp_db):
        row = _completed('/library/a.mkv', False, _at(1), failure_category='stalled')
        assert taskfailure.set_dismissed([row.id]) == 1
        assert taskfailure.outstanding_failure_summary()['total'] == 0

    def test_dismissing_keeps_every_diagnostic(self, temp_db):
        """Dismissal means 'I have seen this', not 'this did not happen'."""
        row = _completed('/library/a.mkv', False, _at(1), failure_category='stalled',
                         failure_message='no sign of life', failure_attempt=2)
        taskfailure.set_dismissed([row.id])
        refreshed = CompletedTasks.get_by_id(row.id)
        assert refreshed.failure_category == 'stalled'
        assert refreshed.failure_message == 'no sign of life'
        assert refreshed.failure_attempt == 2
        assert refreshed.task_success is False

    def test_a_dismissed_failure_still_counts_towards_the_retry_guard(self, temp_db):
        """Otherwise dismissing would be a way to hot-loop a broken file."""
        path = '/library/a.mkv'
        rows = [_completed(path, False, _at(m)) for m in (1, 2, 3)]
        taskfailure.set_dismissed([r.id for r in rows])
        allowed, _ = taskfailure.evaluate_retry(path, settings=_Settings(3))
        assert allowed is False

    def test_a_successful_task_cannot_be_dismissed(self, temp_db):
        row = _completed('/library/a.mkv', True, _at(1))
        assert taskfailure.set_dismissed([row.id]) == 0

    def test_undismissing_restores_it(self, temp_db):
        row = _completed('/library/a.mkv', False, _at(1), failure_category='stalled')
        taskfailure.set_dismissed([row.id])
        taskfailure.set_dismissed([row.id], dismissed=False)
        assert taskfailure.outstanding_failure_summary()['total'] == 1


class TestApiShaping:

    def test_failed_rows_always_report_a_category(self, temp_db):
        from trawlarr.webserver.helpers import completed_tasks

        row = {'task_success': False, 'failure_category': None, 'failure_message': None,
               'failure_time': None, 'failure_attempt': None, 'failure_dismissed': None}
        shaped = completed_tasks._failure_fields_for_result(row)
        assert shaped['failure_category'] == 'unknown'
        assert 'No reason was recorded' in shaped['failure_message']
        assert shaped['failure_dismissed'] is False

    def test_successful_rows_report_nothing(self, temp_db):
        from trawlarr.webserver.helpers import completed_tasks

        shaped = completed_tasks._failure_fields_for_result(
            {'task_success': True, 'failure_category': 'stalled'})
        assert shaped['failure_category'] is None

    def test_failure_time_is_serialised_as_a_timestamp(self, temp_db):
        from trawlarr.webserver.helpers import completed_tasks

        shaped = completed_tasks._failure_fields_for_result(
            {'task_success': False, 'failure_category': 'stalled', 'failure_message': 'm',
             'failure_time': _at(1), 'failure_attempt': 1, 'failure_dismissed': False})
        assert shaped['failure_time'] == _at(1).timestamp()


# ---------------------------------------------------------------------------
# Upgrade path for an existing install
# ---------------------------------------------------------------------------

class TestExistingInstallUpgrade:

    def test_an_existing_database_gains_the_columns_and_keeps_its_rows(self, tmp_path):
        """What an EXISTING install does on upgrade.

        The auto-sync in db_migrate.update_schema() adds nullable columns with
        a plain ALTER TABLE, which is why these fields are nullable and why no
        hand-written migration is needed. The rows written before the upgrade
        must survive with NULL failure state and read back as 'unknown'
        rather than as a fabricated reason.
        """
        from trawlarr.webserver.helpers import completed_tasks

        db_file = tmp_path / 'trawlarr.db'
        legacy = peewee.SqliteDatabase(str(db_file))
        legacy.connect()
        # The completed_tasks table exactly as it was before this change.
        legacy.execute_sql(
            "CREATE TABLE completedtasks ("
            "  id INTEGER NOT NULL PRIMARY KEY,"
            "  task_label TEXT NOT NULL,"
            "  abspath TEXT NOT NULL DEFAULT '',"
            "  task_success INTEGER NOT NULL,"
            "  start_time DATETIME NOT NULL,"
            "  finish_time DATETIME NOT NULL,"
            "  processed_by_worker TEXT NOT NULL)")
        legacy.execute_sql(
            "INSERT INTO completedtasks (task_label, abspath, task_success, start_time,"
            " finish_time, processed_by_worker) VALUES (?, ?, ?, ?, ?, ?)",
            ('old.mkv', '/library/old.mkv', 0, '2026-01-01T00:00:00', '2026-01-01T00:10:00', 'Worker-1'))
        pre_upgrade_columns = {c.name for c in legacy.get_columns('completedtasks')}
        assert 'failure_category' not in pre_upgrade_columns
        legacy.close()

        _run_schema_update(db_file)

        upgraded = peewee.SqliteDatabase(str(db_file))
        upgraded.connect()
        columns = {c.name for c in upgraded.get_columns('completedtasks')}
        assert {'failure_category', 'failure_message', 'failure_time',
                'failure_attempt', 'failure_dismissed'}.issubset(columns), \
            "An existing install did not gain the failure columns on upgrade"

        index_columns = {tuple(idx.columns) for idx in upgraded.get_indexes('completedtasks')}
        assert ('failure_dismissed',) in index_columns, \
            "The health-view index was not created on an existing install"
        assert ('failure_category',) in index_columns

        rows = list(upgraded.execute_sql(
            "SELECT task_label, task_success, failure_category, failure_message"
            " FROM completedtasks"))
        upgraded.close()
        assert len(rows) == 1, "The upgrade lost the pre-existing history"
        assert rows[0][0] == 'old.mkv'
        assert rows[0][2] is None

        # And the pre-upgrade row is presented honestly, not with an invented
        # reason.
        shaped = completed_tasks._failure_fields_for_result(
            {'task_success': False, 'failure_category': rows[0][2], 'failure_message': rows[0][3]})
        assert shaped['failure_category'] == 'unknown'

    def test_a_fresh_install_gets_the_same_schema(self, tmp_path):
        """The other half of the upgrade proof: the columns and indexes must
        also be right when there is no database at all."""
        db_file = tmp_path / 'fresh.db'
        _run_schema_update(db_file)

        fresh = peewee.SqliteDatabase(str(db_file))
        fresh.connect()
        columns = {c.name for c in fresh.get_columns('completedtasks')}
        index_columns = {tuple(idx.columns) for idx in fresh.get_indexes('completedtasks')}
        fresh.close()
        assert {'failure_category', 'failure_message', 'failure_time',
                'failure_attempt', 'failure_dismissed'}.issubset(columns)
        assert ('failure_dismissed',) in index_columns
