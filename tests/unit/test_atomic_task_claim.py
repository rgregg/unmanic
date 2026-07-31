#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_atomic_task_claim.py

    Claiming a pending task must be a compare-and-swap, not a SELECT followed
    by a hopeful UPDATE. See issue #83.

    The old code read the next pending row and then unconditionally set it to
    'in_progress'. Anything that claimed the same row in between got the same
    task handed back to it, and two things would process one file: two writers
    on one cache path, two post-processors racing to deliver over the same
    source file.

    Today only the single Foreman thread claims tasks, so the window is not
    reachable in the shipped daemon - which is exactly why it needs a test.
    The tests below open the window explicitly, by having a competitor claim
    the selected row between the selection and the claim, and assert the
    claimer notices.
"""

import pytest
from peewee import SqliteDatabase

from trawlarr.libs import taskqueue
from trawlarr.libs.taskqueue import TaskQueue
from trawlarr.libs.unmodels import Libraries, LibraryTags, Tags, Tasks
from trawlarr.libs.unmodels.lib import db

MODELS = [Tasks, Libraries, LibraryTags, Tags]


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


def add_pending(abspath, priority):
    return Tasks.create(abspath=abspath, status='pending', priority=priority, library_id=1, type='local')


def statuses():
    return {row.abspath: row.status for row in Tasks.select()}


def steal_once(monkeypatch, worker_name='CompetingWorker'):
    """
    Claim the row the queue has just selected, exactly once.

    This is the interleaving that the non-atomic claim could not see. It is
    injected around the candidate selection query, which both the old and the
    new implementation call, so the test is fair to both.
    """
    original = taskqueue.build_tasks_query
    state = {'stolen': False}

    def selecting(*args, **kwargs):
        result = original(*args, **kwargs)
        if result is not None and not state['stolen']:
            state['stolen'] = True
            (Tasks
             .update({Tasks.status: 'in_progress', Tasks.processed_by_worker: worker_name})
             .where(Tasks.id == result.id)
             .execute())
        return result

    monkeypatch.setattr(taskqueue, 'build_tasks_query', selecting)
    return state


@pytest.mark.unittest
class TestTheClaimIsAtomic:

    def test_a_task_claimed_between_the_select_and_the_claim_is_not_handed_out_again(self, task_db, monkeypatch):
        """The defect itself: two claimers, one task."""
        add_pending('/library/first.mkv', priority=20)
        add_pending('/library/second.mkv', priority=10)
        steal_once(monkeypatch)

        claimed = TaskQueue({}).get_next_pending_tasks()

        assert claimed, "expected the queue to fall through to the task nobody else had claimed"
        assert claimed.get_source_abspath() == '/library/second.mkv', (
            "the queue handed back a task that had already been claimed by something else - "
            "two workers would now be processing the same file"
        )
        assert Tasks.get(abspath='/library/first.mkv').processed_by_worker == 'CompetingWorker', (
            "the stolen task's claim was overwritten by a claimer that had already lost the race"
        )

    def test_nothing_is_claimed_when_the_only_pending_task_is_taken(self, task_db, monkeypatch):
        """
        With nothing else queued, losing the race must return nothing at all.
        Returning the stolen task would be worse than returning nothing.
        """
        add_pending('/library/only.mkv', priority=10)
        steal_once(monkeypatch)

        claimed = TaskQueue({}).get_next_pending_tasks()

        assert not claimed
        assert Tasks.get(abspath='/library/only.mkv').processed_by_worker == 'CompetingWorker'

    def test_a_claim_gives_up_rather_than_looping_forever(self, task_db, monkeypatch):
        """
        A competitor that wins every single race must not hold the Foreman
        loop hostage.
        """
        original = taskqueue.build_tasks_query
        calls = {'count': 0}

        def always_stolen(*args, **kwargs):
            result = original(*args, **kwargs)
            if result is not None:
                calls['count'] += 1
                Tasks.update({Tasks.status: 'in_progress'}).where(Tasks.id == result.id).execute()
            return result

        monkeypatch.setattr(taskqueue, 'build_tasks_query', always_stolen)
        for index in range(taskqueue.CLAIM_ATTEMPTS + 5):
            add_pending('/library/file-{}.mkv'.format(index), priority=index)

        assert not TaskQueue({}).get_next_pending_tasks()
        assert calls['count'] == taskqueue.CLAIM_ATTEMPTS

    def test_claim_task_reports_whether_this_caller_won(self, task_db):
        row = add_pending('/library/cas.mkv', priority=1)
        assert taskqueue.claim_task(row.id) is True
        assert Tasks.get(id=row.id).status == 'in_progress'
        # Second attempt must lose: the row is no longer pending.
        assert taskqueue.claim_task(row.id) is False


@pytest.mark.unittest
class TestTheOrdinaryClaimStillWorks:
    """A check that fires on healthy work is worse than no check at all."""

    def test_the_highest_priority_pending_task_is_claimed_and_returned(self, task_db):
        add_pending('/library/low.mkv', priority=1)
        add_pending('/library/high.mkv', priority=99)

        claimed = TaskQueue({}).get_next_pending_tasks()

        assert claimed.get_source_abspath() == '/library/high.mkv'
        assert statuses() == {'/library/low.mkv': 'pending', '/library/high.mkv': 'in_progress'}

    def test_the_returned_task_reads_back_as_claimed(self, task_db):
        add_pending('/library/one.mkv', priority=1)
        claimed = TaskQueue({}).get_next_pending_tasks()
        assert claimed.task.status == 'in_progress', (
            "the caller was handed a task object still describing itself as pending"
        )

    def test_an_empty_queue_claims_nothing(self, task_db):
        assert not TaskQueue({}).get_next_pending_tasks()

    def test_consecutive_claims_take_different_tasks(self, task_db):
        add_pending('/library/a.mkv', priority=3)
        add_pending('/library/b.mkv', priority=2)
        queue = TaskQueue({})
        first = queue.get_next_pending_tasks()
        second = queue.get_next_pending_tasks()
        assert first.get_source_abspath() != second.get_source_abspath()
        assert not queue.get_next_pending_tasks()
