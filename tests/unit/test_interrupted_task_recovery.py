#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_interrupted_task_recovery.py

    A task claimed by a process that then stopped must not stay claimed
    forever. See issue #83.

    Moving a task to 'in_progress' used to be a one-way door: only the worker
    holding it ever moved it on, so a container restart mid-transcode left the
    row claimed with nothing left to unclaim it. The file then sits in a gap -
    #33's done-state says it never completed, the task row says it is already
    being handled - so no scan offers it again, and #42's busy/idle gate,
    which counts 'in_progress', never reports idle again.

    The two properties that matter here:

      - every claimed row is dealt with at startup, and dealt with LOUDLY:
        it lands in the completed-tasks history with #25's vocabulary, not
        silently back on the queue;

      - a file that is interrupted over and over is not re-queued forever.
        A transcode that reliably OOM-kills the container would otherwise be
        resurrected on every boot to kill it again.

    And the property that makes it safe at all: this runs before the Foreman
    exists, so it can never be looking at a task a live worker is holding.
    That ordering is tested too - it is the entire correctness argument.
"""

import os

import pytest
from peewee import SqliteDatabase

from trawlarr.libs import taskfailure, taskrecovery
from trawlarr.libs.unmodels import CompletedTasks, CompletedTasksCommandLogs, Libraries, LibraryTags, Tags, Tasks
from trawlarr.libs.unmodels.lib import db

MODELS = [Tasks, CompletedTasks, CompletedTasksCommandLogs, Libraries, LibraryTags, Tags]


@pytest.fixture
def task_db():
    test_db = SqliteDatabase(':memory:')
    db.initialize(test_db)
    test_db.connect()
    test_db.create_tables(MODELS)
    Libraries.create(id=1, name='Library', path='/library')
    yield test_db
    test_db.drop_tables(MODELS)
    test_db.close()


@pytest.fixture
def source_file(tmp_path):
    """A real file on disk, because a task whose source is gone is a
    different case with different handling."""
    path = tmp_path / 'interrupted.mkv'
    path.write_bytes(b'video')
    return str(path)


def add_task(abspath, status, worker='Worker-1'):
    return Tasks.create(abspath=abspath, status=status, priority=1, library_id=1, type='local',
                        processed_by_worker=worker)


def add_history(abspath, success):
    return CompletedTasks.create(task_label=os.path.basename(abspath), abspath=abspath, task_success=success,
                                 processed_by_worker='Worker-1',
                                 failure_category=None if success else taskfailure.CATEGORY_INTERRUPTED)


def history_for(abspath):
    return list(CompletedTasks.select().where(CompletedTasks.abspath == abspath))


@pytest.mark.unittest
class TestAClaimedTaskIsRecovered:

    def test_a_stranded_task_does_not_stay_claimed(self, task_db, source_file):
        add_task(source_file, 'in_progress')

        taskrecovery.reconcile_interrupted_tasks()

        remaining = [row.status for row in Tasks.select()]
        assert 'in_progress' not in remaining, (
            "the task is still claimed after startup reconciliation - it will never be retried "
            "and the installation will never report idle again"
        )
        assert remaining == ['pending']

    def test_the_interruption_is_recorded_in_the_task_history(self, task_db, source_file):
        add_task(source_file, 'in_progress')

        taskrecovery.reconcile_interrupted_tasks()

        rows = history_for(source_file)
        assert len(rows) == 1, "the interruption was not recorded anywhere the user can see it"
        assert rows[0].task_success is False
        assert rows[0].failure_category == taskfailure.CATEGORY_INTERRUPTED
        assert 'stopped while this task was being processed' in rows[0].failure_message

    def test_the_interrupted_category_survives_the_health_view(self, task_db, source_file):
        """
        normalise_category() degrades anything it does not know to 'unknown',
        so a category missing from KNOWN_CATEGORIES would collapse silently in
        the summary the operator actually reads.
        """
        add_task(source_file, 'in_progress')

        taskrecovery.reconcile_interrupted_tasks()

        summary = taskfailure.outstanding_failure_summary()
        assert summary['total'] == 1
        assert summary['categories'] == {taskfailure.CATEGORY_INTERRUPTED: 1}

    def test_the_worker_that_lost_the_task_is_cleared_from_the_requeued_row(self, task_db, source_file):
        add_task(source_file, 'in_progress', worker='Worker-3')

        taskrecovery.reconcile_interrupted_tasks()

        assert Tasks.get(abspath=source_file).processed_by_worker is None

    def test_the_attempt_counter_continues_the_file_history(self, task_db, source_file):
        add_history(source_file, success=False)
        add_task(source_file, 'in_progress')

        taskrecovery.reconcile_interrupted_tasks()

        recorded = [row for row in history_for(source_file) if row.failure_attempt is not None]
        assert [row.failure_attempt for row in recorded] == [2]

    def test_the_summary_reports_what_it_did(self, task_db, source_file):
        add_task(source_file, 'in_progress')
        summary = taskrecovery.reconcile_interrupted_tasks()
        assert summary['found'] == 1
        assert summary['requeued'] == [source_file]
        assert summary['failed'] == []
        assert summary['errors'] == 0


@pytest.mark.unittest
class TestARepeatedlyInterruptedFileIsNotResurrectedForever:

    def test_a_file_past_the_retry_threshold_is_left_failed(self, task_db, source_file):
        """
        Two interruptions already recorded; this is the third. The same guard
        that governs every other repeated failure (#25) refuses to queue it
        again, so a transcode that kills the container cannot loop on boot.
        """
        add_history(source_file, success=False)
        add_history(source_file, success=False)
        add_task(source_file, 'in_progress')

        summary = taskrecovery.reconcile_interrupted_tasks()

        assert Tasks.select().count() == 0, "the task row was left behind"
        assert summary['failed'] == [source_file]
        assert summary['requeued'] == []
        rows = sorted(history_for(source_file), key=lambda row: row.id)
        assert rows[-1].failure_category == taskfailure.CATEGORY_INTERRUPTED
        assert 'has not been queued again' in rows[-1].failure_message

    def test_an_earlier_success_resets_the_count_and_the_file_is_queued_again(self, task_db, source_file):
        """The mirror case: a file that failed twice and has processed cleanly
        since is not a problem file, and must not be refused."""
        add_history(source_file, success=False)
        add_history(source_file, success=False)
        add_history(source_file, success=True)
        add_task(source_file, 'in_progress')

        summary = taskrecovery.reconcile_interrupted_tasks()

        assert summary['requeued'] == [source_file]
        assert Tasks.get(abspath=source_file).status == 'pending'

    def test_a_task_whose_source_file_is_gone_is_not_queued_again(self, task_db, tmp_path):
        missing = str(tmp_path / 'deleted.mkv')
        add_task(missing, 'in_progress')

        summary = taskrecovery.reconcile_interrupted_tasks()

        assert summary['failed'] == [missing]
        assert Tasks.select().count() == 0
        assert 'no longer at that path' in history_for(missing)[0].failure_message


@pytest.mark.unittest
class TestItOnlyTouchesClaimedTasks:
    """A check that fires on healthy work is worse than no check at all."""

    def test_pending_and_processed_tasks_are_left_alone(self, task_db, tmp_path):
        pending = str(tmp_path / 'queued.mkv')
        processed = str(tmp_path / 'awaiting-postprocessing.mkv')
        add_task(pending, 'pending')
        add_task(processed, 'processed')

        summary = taskrecovery.reconcile_interrupted_tasks()

        assert summary['found'] == 0
        assert {row.abspath: row.status for row in Tasks.select()} == {
            pending:   'pending',
            processed: 'processed',
        }
        assert CompletedTasks.select().count() == 0, (
            "a task that was not interrupted was recorded as a failure"
        )

    def test_an_empty_task_table_is_a_no_op(self, task_db):
        summary = taskrecovery.reconcile_interrupted_tasks()
        assert summary == {'found': 0, 'requeued': [], 'failed': [], 'errors': 0}
        assert CompletedTasks.select().count() == 0

    def test_recovery_never_raises_out_into_startup(self, task_db, source_file, monkeypatch):
        """A daemon that refuses to boot because recovery broke helps nobody."""
        add_task(source_file, 'in_progress')
        monkeypatch.setattr(taskrecovery.history, 'History', lambda: (_ for _ in ()).throw(RuntimeError('boom')))
        summary = taskrecovery.reconcile_interrupted_tasks()
        assert summary['errors'] == 1


@pytest.mark.unittest
class TestItRunsBeforeAnyWorkerExists:
    """
    The reason this reconciliation is allowed to assume every claimed row is
    stranded is that it runs before the only thing that claims rows exists. If
    that ordering ever inverts, it starts yanking files out from under live
    workers - which is worse than the bug it fixes.
    """

    def test_reconciliation_happens_before_the_foreman_is_started(self, monkeypatch):
        from trawlarr import service

        calls = []

        class FakeSettings:
            @staticmethod
            def get_cache_path():
                return '/tmp/does-not-matter'

        service_instance = service.RootService.__new__(service.RootService)
        service_instance.threads = []
        service_instance.logger = service.TrawlarrLogging.get_logger(name='TestRootService')

        monkeypatch.setattr(service.common, 'clean_files_in_cache_dir', lambda path: None)
        monkeypatch.setattr(service.plugin_registration, 'report_plugin_registration', lambda **kwargs: None)
        monkeypatch.setattr(service.plugin_settings, 'validate_required_plugin_settings', lambda: [])
        monkeypatch.setattr(service.taskrecovery, 'reconcile_interrupted_tasks',
                            lambda settings=None: calls.append('reconcile'))
        monkeypatch.setattr(service, 'TaskQueue', lambda data_queues: object())
        for name in ('initial_register_unmanic', 'start_handler', 'start_post_processor',
                     'start_library_scanner_manager', 'start_inotify_watch_manager', 'start_ui_server',
                     'start_scheduled_tasks_manager', 'start_resource_logger'):
            monkeypatch.setattr(service.RootService, name,
                                lambda self, *args, _name=name, **kwargs: calls.append(_name))
        monkeypatch.setattr(service.RootService, 'start_foreman',
                            lambda self, *args, **kwargs: calls.append('start_foreman'))

        service_instance.start_threads(FakeSettings())

        assert 'reconcile' in calls, "startup no longer reconciles interrupted tasks at all"
        assert 'start_foreman' in calls
        assert calls.index('reconcile') < calls.index('start_foreman'), (
            "interrupted-task recovery now runs after the Foreman has started, so it can see - and "
            "re-queue - a task a live worker is legitimately holding"
        )
