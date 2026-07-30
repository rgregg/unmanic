#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_failure_completion_exclusivity.py

    The seam between issue #33 (the native "this file is done" record) and
    issue #25 (durable task failure state).

    A task ends either succeeded or failed, and the two features write to the
    same region of the post-processor. The invariant they share is that
    exactly one of the two records is produced:

      - success -> a done-state row for the delivered file, and a history row
        carrying no failure fields;
      - failure -> a history row carrying the reason, and NO done-state row.

    The failure mode this file exists to catch is the second one going wrong.
    A failed task recorded as done is never offered to the scanner again, so
    the file is quietly dropped from the pipeline with its original content
    still in place and nothing anywhere saying so. That is the exact shape of
    silent data loss this milestone is about, and it is a one-line mistake
    away at all times because both writes now live in the same loop body.
"""
import datetime
import logging
import os
from unittest import mock

import peewee
import pytest

from trawlarr.libs import donestate, taskfailure
from trawlarr.libs.task import TaskDataStore
from trawlarr.libs.unmodels import CompletedTasks, CompletedTasksCommandLogs, FileCompletionState
from trawlarr.libs.unmodels.lib.basemodel import db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path):
    """Both features' tables on one throwaway database, bound through the same
    proxy the models resolve at runtime."""
    previous = db.obj
    database = peewee.SqliteDatabase(str(tmp_path / 'trawlarr.db'))
    db.initialize(database)
    database.connect(reuse_if_open=True)
    database.create_tables([CompletedTasks, CompletedTasksCommandLogs, FileCompletionState])
    yield database
    database.close()
    db.initialize(previous)


@pytest.fixture
def library_file(tmp_path):
    """A real file on disk - donestate signs what it finds there."""
    path = tmp_path / 'episode.mkv'
    path.write_bytes(b'x' * 2048)
    return str(path)


def _at(offset):
    return datetime.datetime(2026, 7, 27, 12, 0, 0) + datetime.timedelta(seconds=offset)


def _build_postprocessor(task_id, abspath, success, destination_files=None):
    """A PostProcessor with just enough state to run the two write paths.

    __init__ is bypassed deliberately: it builds a real config, a real plugin
    handler and touches the database.
    """
    from trawlarr.libs.postprocessor import PostProcessor

    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger('test_failure_completion_exclusivity')
    pp._last_destination_files = list(destination_files if destination_files is not None else [abspath])
    pp._last_file_move_processes_success = success

    task = mock.Mock()
    task.task.success = success
    task.get_task_id.return_value = task_id
    task.get_task_library_id.return_value = 1
    task.get_task_type.return_value = 'local'
    task.get_task_success.return_value = success
    task.get_source_data.return_value = {'abspath': abspath}
    task.get_destination_data.return_value = {'abspath': abspath}
    task.task_dump.return_value = {
        'task_label':          os.path.basename(abspath),
        'abspath':             abspath,
        'task_success':        success,
        'start_time':          _at(1),
        'finish_time':         _at(2),
        'processed_by_worker': 'Worker-1',
        'log':                 'log text',
    }
    pp.current_task = task
    return pp


def _finish_task(pp):
    """Run the post-processor's end-of-task writes in the order run() does."""
    with mock.patch('trawlarr.libs.postprocessor.Notifications'), \
            mock.patch('trawlarr.libs.postprocessor.PluginsHandler'), \
            mock.patch('trawlarr.libs.postprocessor.TrawlarrLogging'):
        pp.write_history_log()
        pp.record_completed_file()


def _history_row(abspath):
    return CompletedTasks.select().where(CompletedTasks.abspath == abspath).get()


def _done_rows(abspath):
    return FileCompletionState.select().where(FileCompletionState.abspath == abspath).count()


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------

class TestSuccessAndFailureAreExclusive:

    def test_a_successful_task_is_recorded_done_and_carries_no_failure_state(self, temp_db, library_file):
        task_id = 200010
        TaskDataStore.clear_task(task_id)

        pp = _build_postprocessor(task_id, library_file, success=True)
        _finish_task(pp)

        row = _history_row(library_file)
        assert row.task_success is True
        assert row.failure_category is None
        assert row.failure_message is None
        assert row.failure_time is None
        assert row.failure_attempt is None
        assert donestate.file_is_already_completed(library_file)[0] is True

        TaskDataStore.clear_task(task_id)

    def test_a_failed_task_is_never_recorded_as_done(self, temp_db, library_file):
        """The data-loss-shaped bug: a done record for a failed task means the
        scanner never offers the file again, and the file on disk is still the
        untouched original."""
        task_id = 200020
        TaskDataStore.clear_task(task_id)
        taskfailure.record(task_id, taskfailure.CATEGORY_COMMAND_FAILED, 'ffmpeg exited 1')

        pp = _build_postprocessor(task_id, library_file, success=False)
        _finish_task(pp)

        row = _history_row(library_file)
        assert row.task_success is False
        assert row.failure_category == 'command_failed'
        assert 'ffmpeg exited 1' in row.failure_message
        assert _done_rows(library_file) == 0
        assert donestate.file_is_already_completed(library_file)[0] is False

        TaskDataStore.clear_task(task_id)

    def test_a_failure_with_no_recorded_reason_is_still_kept_out_of_the_done_state(self, temp_db, library_file):
        """Nothing on the way down said why. The history row says 'unknown'
        rather than nothing, and the file is still not marked done."""
        task_id = 200030
        TaskDataStore.clear_task(task_id)

        pp = _build_postprocessor(task_id, library_file, success=False)
        _finish_task(pp)

        row = _history_row(library_file)
        assert row.failure_category == 'unknown'
        assert row.failure_message
        assert _done_rows(library_file) == 0

        TaskDataStore.clear_task(task_id)

    def test_a_success_leaves_no_failure_state_behind(self, temp_db, library_file):
        """#15's stall detector can record a reason for a task that then
        completes anyway - a slow-but-alive command, say. The task succeeded,
        so the history row must carry no failure and the file must be done.
        A check that fires on healthy work is worse than no check."""
        task_id = 200040
        TaskDataStore.clear_task(task_id)
        taskfailure.record(task_id, taskfailure.CATEGORY_STALLED, 'no sign of life for 300s')

        pp = _build_postprocessor(task_id, library_file, success=True)
        _finish_task(pp)

        row = _history_row(library_file)
        assert row.failure_category is None
        assert row.failure_message is None
        assert donestate.file_is_already_completed(library_file)[0] is True

        TaskDataStore.clear_task(task_id)


class TestSanityRejectionLandsInTheFailurePath:
    """#35 refuses to deliver output it thinks is damaged, by calling
    set_success(False) after the worker has already declared success. That
    late flip is what decides which of the two records this task gets."""

    def _reject(self, pp, source_abspath, cache_path):
        failing = mock.Mock()
        failing.checked = True
        failing.failed = True
        failing.state = {}
        failing.failures = [{'id': 'output_grew', 'message': 'output is 3.2x the size of the source'}]
        failing.report.return_value = 'sanity report'

        check_settings = mock.Mock()
        check_settings.enabled = True

        with mock.patch('trawlarr.libs.postprocessor.sanity') as fake_sanity, \
                mock.patch('trawlarr.libs.postprocessor.TrawlarrLogging'):
            fake_sanity.SanityCheckSettings.from_settings.return_value = check_settings
            fake_sanity.check_task_output.return_value = failing
            return pp.run_output_sanity_checks(
                {'abspath': source_abspath}, {'abspath': source_abspath}, cache_path)

    def test_a_sanity_rejected_task_is_failed_and_not_recorded_done(self, temp_db, library_file, tmp_path):
        task_id = 200050
        TaskDataStore.clear_task(task_id)
        cache_path = str(tmp_path / 'cache' / 'episode.mkv')

        pp = _build_postprocessor(task_id, library_file, success=True)
        pp.settings = mock.Mock()

        # The worker handed this over as a success; the sanity check overrules.
        def _set_success(value):
            pp.current_task.task.success = value
            pp.current_task.get_task_success.return_value = value
            pp.current_task.task_dump.return_value['task_success'] = value

        pp.current_task.set_success.side_effect = _set_success

        assert self._reject(pp, library_file, cache_path) is False
        assert pp.current_task.get_task_success() is False

        # The output was refused, so nothing was delivered.
        pp._last_destination_files = []
        pp._last_file_move_processes_success = False
        _finish_task(pp)

        row = _history_row(library_file)
        assert row.task_success is False
        assert row.failure_category == 'sanity_check'
        assert '3.2x' in row.failure_message
        assert _done_rows(library_file) == 0
        assert donestate.file_is_already_completed(library_file)[0] is False

        TaskDataStore.clear_task(task_id)

    def test_the_sanity_reason_wins_over_an_earlier_one(self, temp_db, library_file, tmp_path):
        """The worker may already have recorded a reason for a task it then
        returned as successful. The refusal is what decided this outcome, so
        it is the reason that is persisted."""
        task_id = 200060
        TaskDataStore.clear_task(task_id)
        taskfailure.record(task_id, taskfailure.CATEGORY_PLUGIN_ERROR, 'a plugin grumbled earlier')

        pp = _build_postprocessor(task_id, library_file, success=True)
        pp.settings = mock.Mock()
        pp.current_task.set_success.side_effect = lambda value: None

        self._reject(pp, library_file, str(tmp_path / 'cache' / 'episode.mkv'))

        record = taskfailure.read(task_id)
        assert record['category'] == 'sanity_check'

        TaskDataStore.clear_task(task_id)
