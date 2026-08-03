#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_activity_api.py

    These tests pin the two guarantees docs/AUTOMATION.md makes to external
    callers: that `busy` is false only when every worker is idle and every
    count is zero, and that an undeterminable state is a 500 rather than
    `{"busy": false}`. An external maintenance script gates on both, so if
    they change here, that document is wrong and must change with them.
    See devops/doc_claims.py.

    Contract coverage for GET /trawlarr/api/v2/activity/status, see #42.

    This endpoint exists to replace a maintenance script that decided whether
    Trawlarr was working by walking /proc for an ffmpeg process without
    `-nostdin`. Something is going to gate destructive library maintenance on
    the answer, so the two properties that actually matter are:

      - it is busy-biased. A file is still being written for the whole
        post-processing window, long after every worker has gone back to
        idle; and a task sitting in 'pending' can be claimed by a worker
        between the gate check and the maintenance run. Both count as busy.

      - an unknown state is an error, not an idle. If the Foreman is not
        running we cannot see the workers, and answering "not busy" there is
        how two processes end up writing the same file.

    The shape is pinned too. An external script is going to hard-code these
    key names; they are a contract now, not an implementation detail.
"""

import asyncio
import json

import pytest
from peewee import SqliteDatabase

from trawlarr.libs.unmodels import Tasks
from trawlarr.libs.unmodels.lib import db
from trawlarr.libs.uiserver import TrawlarrRunningTreads
from trawlarr.webserver.api_v2.activity_api import ApiActivityHandler
from trawlarr.webserver.api_v2.schema.schemas import ActivityStatusSuccessSchema
from trawlarr.webserver.helpers import activity


def worker(idle=True, paused=False):
    """A worker status dict of the shape Foreman.get_all_worker_status() returns"""
    return {
        'id':     'W0',
        'name':  'Worker-W0',
        'idle':   idle,
        'paused': paused,
    }


class FakeForeman:
    def __init__(self, workers):
        self._workers = workers

    def get_all_worker_status(self):
        return list(self._workers)


@pytest.fixture
def running_foreman(monkeypatch):
    """Install a fake Foreman into the running-threads singleton"""

    def _install(workers):
        foreman = FakeForeman(workers)
        monkeypatch.setattr(
            TrawlarrRunningTreads,
            'get_unmanic_running_thread',
            lambda self, name: foreman if name == 'foreman' else None,
        )
        return foreman

    return _install


@pytest.fixture
def no_foreman(monkeypatch):
    monkeypatch.setattr(
        TrawlarrRunningTreads,
        'get_unmanic_running_thread',
        lambda self, name: None,
    )


@pytest.fixture
def empty_task_queue(monkeypatch):
    monkeypatch.setattr(
        activity, 'count_tasks_by_status',
        lambda: {'pending': 0, 'in_progress': 0, 'processed': 0},
    )


@pytest.fixture
def task_queue(monkeypatch):
    def _install(pending=0, in_progress=0, processed=0):
        counts = {'pending': pending, 'in_progress': in_progress, 'processed': processed}
        monkeypatch.setattr(activity, 'count_tasks_by_status', lambda: counts)
        return counts

    return _install


class TestBusyIsBiasedTowardsBusy:

    def test_all_workers_idle_and_nothing_queued_is_not_busy(self, running_foreman, empty_task_queue):
        running_foreman([worker(idle=True), worker(idle=True)])
        assert activity.get_activity_status()['busy'] is False

    def test_a_working_worker_is_busy(self, running_foreman, empty_task_queue):
        running_foreman([worker(idle=False), worker(idle=True)])
        status = activity.get_activity_status()
        assert status['busy'] is True
        assert status['workers']['busy'] == 1

    def test_post_processing_is_busy_even_though_every_worker_is_idle(self, running_foreman, task_queue):
        """
        The blind spot that makes /workers/status unsafe to gate on.

        A task in 'processed' has finished transcoding and been handed to the
        PostProcessor, which is where the multi-gigabyte copy back over the
        original file happens. Every worker reports idle throughout.
        """
        running_foreman([worker(idle=True), worker(idle=True)])
        task_queue(processed=1)
        assert activity.get_activity_status()['busy'] is True

    def test_a_pending_task_is_busy_even_with_every_worker_idle(self, running_foreman, task_queue):
        """A queued task can be claimed by a worker at any moment."""
        running_foreman([worker(idle=True)])
        task_queue(pending=7)
        status = activity.get_activity_status()
        assert status['busy'] is True
        assert status['tasks']['pending'] == 7

    def test_an_in_progress_task_is_busy(self, running_foreman, task_queue):
        running_foreman([worker(idle=True)])
        task_queue(in_progress=1)
        assert activity.get_activity_status()['busy'] is True

    def test_a_paused_worker_still_holding_a_task_is_busy(self, running_foreman, empty_task_queue):
        running_foreman([worker(idle=False, paused=True)])
        status = activity.get_activity_status()
        assert status['busy'] is True
        assert status['workers']['paused'] == 1

    def test_no_workers_configured_and_nothing_queued_is_not_busy(self, running_foreman, empty_task_queue):
        running_foreman([])
        status = activity.get_activity_status()
        assert status['busy'] is False
        assert status['workers'] == {'total': 0, 'busy': 0, 'paused': 0}


class TestUnknownIsNeverIdle:

    def test_missing_foreman_raises_rather_than_reporting_idle(self, no_foreman, empty_task_queue):
        with pytest.raises(activity.ActivityStateUnavailableError):
            activity.get_activity_status()

    def test_the_endpoint_returns_500_when_the_state_is_unknown(self, no_foreman, empty_task_queue):
        handler = build_handler()
        asyncio.run(handler.activity_status())
        assert handler.status == 500
        assert handler.successes_written == 0
        assert handler.errors_written == 1


class TestTheResponseShapeIsAContract:

    def test_the_helper_output_validates_against_the_published_schema(self, running_foreman, task_queue):
        running_foreman([worker(idle=False), worker(idle=True)])
        task_queue(pending=3, in_progress=1, processed=2)
        assert ActivityStatusSuccessSchema().validate(activity.get_activity_status()) == {}

    def test_the_endpoint_writes_the_documented_keys(self, running_foreman, task_queue):
        running_foreman([worker(idle=False), worker(idle=True), worker(idle=True)])
        task_queue(pending=3, in_progress=1, processed=2)

        handler = build_handler()
        asyncio.run(handler.activity_status())

        assert handler.status == 200
        assert handler.written == {
            'busy':    True,
            'workers': {'total': 3, 'busy': 1, 'paused': 0},
            'tasks':   {'pending': 3, 'in_progress': 1, 'processed': 2},
        }

    def test_the_route_is_advertised_in_the_checked_in_contract(self):
        from trawlarr.webserver.api_v2.schema.swagger import get_swagger_file_location

        with open('{}.json'.format(get_swagger_file_location()), encoding='utf-8') as file:
            spec = json.load(file)

        get = spec['paths']['/activity/status']['get']
        assert list(get['responses']['200']['content']['application/json']['schema'].values()) == [
            '#/components/schemas/ActivityStatusSuccess'
        ]
        assert '500' in get['responses']


class TestCountTasksByStatus:
    """The counts come out of the task table, so exercise them against a real one."""

    @pytest.fixture(autouse=True)
    def in_memory_tasks_table(self):
        test_db = SqliteDatabase(':memory:')
        db.initialize(test_db)
        test_db.connect()
        test_db.create_tables([Tasks])
        yield test_db
        test_db.drop_tables([Tasks])
        test_db.close()

    @staticmethod
    def add_task(abspath, status):
        Tasks.create(abspath=abspath, status=status, priority=1, library_id=1)

    def test_an_empty_table_reports_every_status_as_zero(self):
        assert activity.count_tasks_by_status() == {'pending': 0, 'in_progress': 0, 'processed': 0}

    def test_tasks_are_counted_per_status(self):
        self.add_task('/library/a.mkv', 'pending')
        self.add_task('/library/b.mkv', 'pending')
        self.add_task('/library/c.mkv', 'in_progress')
        self.add_task('/library/d.mkv', 'processed')
        assert activity.count_tasks_by_status() == {'pending': 2, 'in_progress': 1, 'processed': 1}

    def test_the_count_is_a_real_count_and_not_a_capped_existence_check(self):
        """
        taskqueue.build_tasks_count_query() answers "any?" with a LIMIT 1. Queue
        depth has to be the actual depth, or an operator watching the number go
        down learns nothing.
        """
        for index in range(25):
            self.add_task('/library/file-{}.mkv'.format(index), 'pending')
        assert activity.count_tasks_by_status()['pending'] == 25

    def test_statuses_outside_the_active_set_are_ignored(self):
        self.add_task('/library/done.mkv', 'complete')
        self.add_task('/library/waiting.mkv', 'pending')
        assert activity.count_tasks_by_status() == {'pending': 1, 'in_progress': 0, 'processed': 0}


def build_handler():
    """
    Build an ApiActivityHandler that is callable without a Tornado application.

    Only the response plumbing is stubbed; activity_status() runs unmodified.
    """
    handler = ApiActivityHandler.__new__(ApiActivityHandler)
    handler.route = {'call_method': 'activity_status'}
    handler.error_messages = {}
    handler.status = None
    handler.reason = None
    handler.written = None
    handler.errors_written = 0
    handler.successes_written = 0

    def set_status(status_code, reason=None):
        handler.status = status_code
        handler.reason = reason

    def write_error(status_code=None, **kwargs):
        handler.errors_written += 1

    def write_success(response=None):
        handler.successes_written += 1
        handler.status = 200
        handler.written = json.loads(response) if isinstance(response, str) else response

    handler.set_status = set_status
    handler.write_error = write_error
    handler.write_success = write_success
    return handler
