#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_foreman.py

    The Foreman is the only thing in this application that ever claims a
    pending task and the only thing that ever hands one to a worker. It was
    also the least-tested module in the application (12% line coverage) when
    issue #81 was written, which is the reason its run loop could end in
    `except Exception as e: raise Exception(e)` for years without anyone
    noticing that a single transient fault stops the installation dead.

    These tests pin behaviour, not lines:

      * the dispatch decision - who gets the next task, and the four
        conditions under which nobody does
      * worker lifecycle - spawn to the configured count, retire the
        surplus, never terminate a worker holding a task
      * tag handling, which is how a task and a worker are matched at all
      * the run loop's error handling, which is what #81 changed

    The collaborators (WorkerGroup, Library, PluginsHandler, Worker) are
    faked. They each own a database table and a thread; the Foreman's own
    decisions are what is under test here.
"""
import logging
import threading

import pytest

from trawlarr.libs import foreman as foreman_module
from trawlarr.libs.foreman import MAX_CONSECUTIVE_MONITOR_FAILURES, Foreman


class NoWaitEvent(object):
    """The shutdown event, with the waiting taken out."""

    def __init__(self):
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return False

    def is_set(self):
        return False

    def set(self):
        pass

    def clear(self):
        pass


class FakeWorker(object):
    """A worker thread that records what it was told, and never runs."""

    started = []

    def __init__(self, thread_id, name, worker_group_id, pending_queue, complete_queue, event):
        self.thread_id = thread_id
        self.name = name
        self.worker_group_id = worker_group_id
        self.pending_queue = pending_queue
        self.complete_queue = complete_queue
        self.event = event
        self.daemon = False
        self.idle = True
        self.alive = True
        self.current_task = None
        self.redundant_flag = threading.Event()
        self.paused_flag = threading.Event()

    @property
    def paused(self):
        return self.paused_flag.is_set()

    def start(self):
        FakeWorker.started.append(self)

    def is_alive(self):
        return self.alive

    def set_task(self, task):
        self.current_task = task
        self.idle = False

    def get_status(self):
        return {
            'id':     self.thread_id,
            'name':   self.name,
            'idle':   self.idle,
            'paused': self.paused_flag.is_set(),
        }


class FakeWorkerGroup(object):
    """Stands in for the worker_group table.

    `groups` is class state so that the Foreman, which only ever reaches for
    the class, sees whatever the test configured.
    """

    groups = []
    saved = []

    def __init__(self, group_id=None):
        for group in FakeWorkerGroup.groups:
            if group.get('id') == group_id:
                self._group = group
                return
        raise Exception("Worker group {} does not exist".format(group_id))

    @staticmethod
    def get_all_worker_groups():
        return [dict(group) for group in FakeWorkerGroup.groups]

    def get_id(self):
        return self._group.get('id')

    def get_tags(self):
        return list(self._group.get('tags', []))

    def get_worker_event_schedules(self):
        return list(self._group.get('schedules', []))

    def set_number_of_workers(self, value):
        self._group['number_of_workers'] = value

    def save(self):
        FakeWorkerGroup.saved.append(dict(self._group))


class FakeTaskQueue(object):

    def __init__(self, pending=None, processed=0):
        self.pending = list(pending or [])
        self.processed = processed
        self.requested_tags = []

    def task_list_pending_is_empty(self):
        return not self.pending

    def get_next_pending_tasks(self, library_tags=None):
        self.requested_tags.append(library_tags)
        for task in list(self.pending):
            if task.tags is None or not library_tags or set(task.tags) & set(library_tags):
                self.pending.remove(task)
                return task
        return None

    def list_processed_tasks(self):
        return [object()] * self.processed


class FakeTask(object):

    def __init__(self, abspath='/library/show.mkv', tags=None, task_type='local'):
        self.abspath = abspath
        self.tags = tags
        self.task_type = task_type
        self.status = None

    def get_source_abspath(self):
        return self.abspath

    def get_task_id(self):
        return 1

    def get_task_library_id(self):
        return 1

    def get_task_type(self):
        return self.task_type

    def get_source_data(self):
        return {'abspath': self.abspath}

    def set_status(self, status):
        self.status = status


class FakeFrontendMessages(object):

    def __init__(self):
        self.added = []
        self.updated = []
        self.removed = []

    def add(self, item):
        self.added.append(item)

    def update(self, item):
        self.updated.append(item)

    def remove_item(self, item_id):
        self.removed.append(item_id)


class FakePluginsHandler(object):

    incompatible = []
    events = []

    def get_incompatible_enabled_plugins(self):
        return list(FakePluginsHandler.incompatible)

    def run_event_plugins_for_plugin_type(self, plugin_type, event_data):
        FakePluginsHandler.events.append((plugin_type, event_data))


class FakeLibrary(object):

    libraries = []

    def __init__(self, library_id):
        self.library_id = library_id

    @staticmethod
    def get_all_libraries():
        return [dict(library) for library in FakeLibrary.libraries]

    @staticmethod
    def within_library_count_limits():
        return True

    def get_enabled_plugins(self, include_settings=False):
        return []

    def get_plugin_flow(self):
        return {}


@pytest.fixture
def foreman_env(monkeypatch):
    """Patch out everything the Foreman reaches for that owns a table."""
    FakeWorker.started = []
    FakeWorkerGroup.groups = [{'id': 1, 'name': 'Default', 'number_of_workers': 1, 'tags': []}]
    FakeWorkerGroup.saved = []
    FakeLibrary.libraries = [{'id': 1}]
    FakePluginsHandler.incompatible = []
    FakePluginsHandler.events = []

    frontend = FakeFrontendMessages()
    monkeypatch.setattr(foreman_module, 'Worker', FakeWorker)
    monkeypatch.setattr(foreman_module, 'WorkerGroup', FakeWorkerGroup)
    monkeypatch.setattr(foreman_module, 'Library', FakeLibrary)
    monkeypatch.setattr(foreman_module, 'PluginsHandler', FakePluginsHandler)
    monkeypatch.setattr(foreman_module, 'FrontendPushMessages', lambda: frontend)
    return frontend


@pytest.fixture
def build_foreman(foreman_env):
    def _build(task_queue=None):
        instance = Foreman({}, object(), task_queue or FakeTaskQueue(), NoWaitEvent())
        instance.logger = logging.getLogger('test_foreman')
        return instance

    return _build


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.unittest
class TestWorkerLifecycle:

    def test_it_spawns_the_configured_number_of_workers(self, build_foreman):
        FakeWorkerGroup.groups[0]['number_of_workers'] = 3
        instance = build_foreman()

        instance.init_worker_threads()

        assert sorted(instance.worker_threads) == ['Default-0', 'Default-1', 'Default-2']
        assert [w.name for w in FakeWorker.started] == ['Default-Worker-1', 'Default-Worker-2',
                                                        'Default-Worker-3']
        assert instance.get_total_worker_count() == 3

    def test_it_does_not_respawn_workers_it_already_has(self, build_foreman):
        FakeWorkerGroup.groups[0]['number_of_workers'] = 2
        instance = build_foreman()

        instance.init_worker_threads()
        instance.init_worker_threads()

        assert len(FakeWorker.started) == 2, "the second pass started a duplicate set of workers"

    def test_a_dead_worker_is_dropped_and_replaced(self, build_foreman):
        instance = build_foreman()
        instance.init_worker_threads()
        instance.worker_threads['Default-0'].alive = False

        instance.init_worker_threads()

        assert instance.worker_threads['Default-0'].alive is True
        assert len(FakeWorker.started) == 2

    def test_reducing_the_worker_count_retires_an_idle_worker(self, build_foreman):
        FakeWorkerGroup.groups[0]['number_of_workers'] = 2
        instance = build_foreman()
        instance.init_worker_threads()

        FakeWorkerGroup.groups[0]['number_of_workers'] = 1
        instance.init_worker_threads()

        assert instance.worker_threads['Default-1'].redundant_flag.is_set()
        assert not instance.worker_threads['Default-0'].redundant_flag.is_set()

    def test_a_worker_holding_a_task_is_never_retired(self, build_foreman):
        """Terminating a busy worker to reduce the worker count would kill a
        transcode mid-file."""
        FakeWorkerGroup.groups[0]['number_of_workers'] = 2
        instance = build_foreman()
        instance.init_worker_threads()
        instance.worker_threads['Default-1'].idle = False

        FakeWorkerGroup.groups[0]['number_of_workers'] = 1
        instance.init_worker_threads()

        assert not instance.worker_threads['Default-1'].redundant_flag.is_set(), (
            "a worker holding a task was marked redundant to satisfy a worker count change"
        )

    def test_workers_of_a_deleted_group_are_retired(self, build_foreman):
        instance = build_foreman()
        instance.init_worker_threads()
        FakeWorkerGroup.groups = []

        instance.init_worker_threads()

        assert instance.worker_threads['Default-0'].redundant_flag.is_set()

    def test_stop_marks_every_worker_redundant(self, build_foreman):
        FakeWorkerGroup.groups[0]['number_of_workers'] = 2
        instance = build_foreman()
        instance.init_worker_threads()

        instance.stop()

        assert instance.abort_flag.is_set()
        assert all(w.redundant_flag.is_set() for w in instance.worker_threads.values())


# ---------------------------------------------------------------------------
# Availability, pausing and tags
# ---------------------------------------------------------------------------

@pytest.mark.unittest
class TestWorkerAvailability:

    def test_only_idle_live_unpaused_workers_are_available(self, build_foreman):
        FakeWorkerGroup.groups[0]['number_of_workers'] = 4
        instance = build_foreman()
        instance.init_worker_threads()
        instance.worker_threads['Default-1'].idle = False
        instance.worker_threads['Default-2'].alive = False
        instance.worker_threads['Default-3'].paused_flag.set()

        assert instance.check_for_idle_workers() is True
        assert instance.fetch_available_worker_ids() == ['Default-0']

    def test_no_available_workers_is_reported_as_such(self, build_foreman):
        instance = build_foreman()
        instance.init_worker_threads()
        instance.worker_threads['Default-0'].idle = False

        assert instance.check_for_idle_workers() is False
        assert instance.fetch_available_worker_ids() == []

    def test_pausing_and_resuming_a_worker(self, build_foreman):
        instance = build_foreman()
        instance.init_worker_threads()

        assert instance.pause_worker_thread('Default-0', record_paused=True) is True
        assert instance.worker_threads['Default-0'].paused_flag.is_set()
        assert instance.paused_worker_threads == ['Default-0']

        assert instance.resume_worker_thread('Default-0') is True
        assert not instance.worker_threads['Default-0'].paused_flag.is_set()
        assert instance.paused_worker_threads == []

    def test_acting_on_an_unknown_worker_reports_failure(self, build_foreman):
        instance = build_foreman()

        assert instance.pause_worker_thread('nope') is False
        assert instance.resume_worker_thread('nope') is False
        assert instance.terminate_worker_thread('nope') is False

    def test_pause_and_resume_can_be_limited_to_one_worker_group(self, build_foreman):
        FakeWorkerGroup.groups.append({'id': 2, 'name': 'GPU', 'number_of_workers': 1, 'tags': []})
        instance = build_foreman()
        instance.init_worker_threads()

        instance.pause_all_worker_threads(worker_group_id=2)

        assert not instance.worker_threads['Default-0'].paused_flag.is_set()
        assert instance.worker_threads['GPU-0'].paused_flag.is_set()

    def test_resume_can_be_limited_to_the_workers_the_foreman_paused(self, build_foreman):
        """A worker an operator paused by hand must not be resumed just
        because the Foreman is resuming its own pauses."""
        FakeWorkerGroup.groups[0]['number_of_workers'] = 2
        instance = build_foreman()
        instance.init_worker_threads()
        instance.pause_worker_thread('Default-0', record_paused=True)
        instance.pause_worker_thread('Default-1', record_paused=False)

        instance.resume_all_worker_threads(recorded_paused_only=True)

        assert not instance.worker_threads['Default-0'].paused_flag.is_set()
        assert instance.worker_threads['Default-1'].paused_flag.is_set()

    def test_worker_tags_come_from_the_workers_own_group(self, build_foreman):
        FakeWorkerGroup.groups = [
            {'id': 1, 'name': 'Default', 'number_of_workers': 1, 'tags': ['cpu']},
            {'id': 2, 'name': 'GPU', 'number_of_workers': 1, 'tags': ['nvenc']},
        ]
        instance = build_foreman()
        instance.init_worker_threads()

        assert instance.get_tags_configured_for_worker('Default-0') == ['cpu']
        assert instance.get_tags_configured_for_worker('GPU-0') == ['nvenc']

    def test_tags_for_a_worker_whose_group_was_deleted_raise(self, build_foreman):
        """The run loop relies on this raising to notice the group is gone."""
        instance = build_foreman()
        instance.init_worker_threads()
        FakeWorkerGroup.groups = []

        with pytest.raises(Exception):
            instance.get_tags_configured_for_worker('Default-0')

    def test_worker_status_is_reported_for_every_worker(self, build_foreman):
        FakeWorkerGroup.groups[0]['number_of_workers'] = 2
        instance = build_foreman()
        instance.init_worker_threads()

        statuses = instance.get_all_worker_status()

        assert len(statuses) == 2
        assert {status['name'] for status in statuses} == {'Default-Worker-1', 'Default-Worker-2'}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

@pytest.mark.unittest
class TestTaskDispatch:

    def test_a_pending_task_is_claimed_and_handed_to_an_idle_worker(self, build_foreman):
        task = FakeTask()
        queue = FakeTaskQueue(pending=[task])
        instance = build_foreman(task_queue=queue)

        instance.run_monitor_pass()

        worker = instance.worker_threads['Default-0']
        assert worker.current_task is task, (
            "the Foreman completed a pass with a pending task and an idle worker and handed "
            "over nothing. Nothing else in the application claims a task."
        )
        assert queue.pending == []
        assert ('events.task_scheduled', {
            'library_id':               1,
            'task_id':                  1,
            'task_type':                'local',
            'task_schedule_type':       'local',
            'remote_installation_info': {},
            'source_data':              {'abspath': '/library/show.mkv'},
        }) in FakePluginsHandler.events

    def test_the_claim_is_filtered_by_the_workers_tags(self, build_foreman):
        FakeWorkerGroup.groups[0]['tags'] = ['nvenc']
        queue = FakeTaskQueue(pending=[FakeTask(tags=['nvenc'])])
        instance = build_foreman(task_queue=queue)

        instance.run_monitor_pass()

        assert queue.requested_tags == [['nvenc']], (
            "the Foreman asked the queue for a task without passing the worker's tags, so a "
            "worker can be handed a task its library is not tagged for"
        )

    def test_no_matching_task_backs_off_instead_of_spinning(self, build_foreman):
        """The tags matched nothing. Re-checking idle workers immediately
        would busy-loop over the same rejection."""
        FakeWorkerGroup.groups[0]['tags'] = ['nvenc']
        queue = FakeTaskQueue(pending=[FakeTask(tags=['cpu-only'])])
        instance = build_foreman(task_queue=queue)

        instance.run_monitor_pass()

        assert instance.allow_local_idle_worker_check is False
        assert instance.worker_threads['Default-0'].current_task is None

        # ... and the next pass re-enables the check rather than latching off
        instance.run_monitor_pass()
        assert instance.allow_local_idle_worker_check is True

    def test_nothing_is_claimed_when_no_worker_is_free(self, build_foreman):
        queue = FakeTaskQueue(pending=[FakeTask()])
        instance = build_foreman(task_queue=queue)
        instance.init_worker_threads()
        instance.worker_threads['Default-0'].idle = False

        instance.run_monitor_pass()

        assert queue.pending, "a task was claimed with no idle worker to give it to"

    def test_nothing_is_claimed_while_a_handover_is_outstanding(self, build_foreman):
        """workers_pending_task_queue holds one task. Claiming a second while
        the first is still sitting there is how two workers end up racing for
        one task."""
        queue = FakeTaskQueue(pending=[FakeTask()])
        instance = build_foreman(task_queue=queue)
        instance.workers_pending_task_queue.put(object())

        instance.run_monitor_pass()

        assert queue.pending

    def test_nothing_is_claimed_while_the_post_processor_is_backed_up(self, build_foreman, foreman_env):
        queue = FakeTaskQueue(pending=[FakeTask()], processed=99)
        instance = build_foreman(task_queue=queue)

        instance.run_monitor_pass()

        assert queue.pending, "tasks were claimed while the post-processor queue was full"
        assert any(item['id'] == 'pendingTaskHaltedPostProcessorQueueFull'
                   for item in foreman_env.updated)

    def test_the_backed_up_status_is_withdrawn_when_the_queue_drains(self, build_foreman, foreman_env):
        instance = build_foreman(task_queue=FakeTaskQueue(processed=0))

        assert instance.postprocessor_queue_full() is False
        assert 'pendingTaskHaltedPostProcessorQueueFull' in foreman_env.removed

    def test_a_task_that_cannot_report_its_path_is_not_handed_over(self, build_foreman):
        class BrokenTask(FakeTask):
            def get_source_abspath(self):
                raise RuntimeError('row is gone')

        queue = FakeTaskQueue(pending=[BrokenTask()])
        instance = build_foreman(task_queue=queue)

        instance.run_monitor_pass()

        assert instance.worker_threads['Default-0'].current_task is None

    def test_a_task_is_not_handed_to_a_dead_worker(self, build_foreman):
        instance = build_foreman()
        instance.init_worker_threads()
        instance.worker_threads['Default-0'].alive = False
        task = FakeTask()

        instance.hand_task_to_workers(task, worker_id='Default-0')

        assert instance.worker_threads['Default-0'].current_task is None
        assert FakePluginsHandler.events == []

    def test_completed_work_is_moved_on_to_post_processing(self, build_foreman):
        """The worker drops the finished task on the complete queue; only the
        Foreman moves it to 'processed', which is what the PostProcessor
        picks up."""
        instance = build_foreman()
        task = FakeTask()
        instance.complete_queue.put(task)

        instance.run_monitor_pass()

        assert task.status == 'processed'

    def test_an_invalid_configuration_pauses_every_worker(self, build_foreman):
        FakePluginsHandler.incompatible = ['some-old-plugin']
        queue = FakeTaskQueue(pending=[FakeTask()])
        instance = build_foreman(task_queue=queue)

        instance.run_monitor_pass()

        assert instance.worker_threads['Default-0'].paused_flag.is_set()
        assert instance.paused_worker_threads == ['Default-0']
        assert queue.pending, "a task was claimed while the plugin configuration was invalid"

    def test_workers_resume_once_the_configuration_is_valid_again(self, build_foreman):
        FakePluginsHandler.incompatible = ['some-old-plugin']
        instance = build_foreman()
        instance.run_monitor_pass()

        FakePluginsHandler.incompatible = []
        instance.run_monitor_pass()

        assert not instance.worker_threads['Default-0'].paused_flag.is_set()
        assert instance.paused_worker_threads == []


# ---------------------------------------------------------------------------
# Scheduled worker events
# ---------------------------------------------------------------------------

@pytest.mark.unittest
class TestScheduledWorkerEvents:

    def test_a_scheduled_count_change_is_persisted(self, build_foreman):
        instance = build_foreman()
        worker_group = FakeWorkerGroup(group_id=1)

        instance.run_task('00:00', 'count', 4, worker_group)

        assert FakeWorkerGroup.groups[0]['number_of_workers'] == 4
        assert FakeWorkerGroup.saved, (
            "a scheduled worker count change was applied but never saved, so it is lost on "
            "the next restart"
        )

    def test_a_scheduled_pause_pauses_only_its_own_group(self, build_foreman):
        FakeWorkerGroup.groups.append({'id': 2, 'name': 'GPU', 'number_of_workers': 1, 'tags': []})
        instance = build_foreman()
        instance.init_worker_threads()

        instance.run_task('00:00', 'pause', None, FakeWorkerGroup(group_id=2))

        assert instance.worker_threads['GPU-0'].paused_flag.is_set()
        assert not instance.worker_threads['Default-0'].paused_flag.is_set()

        instance.run_task('00:01', 'resume', None, FakeWorkerGroup(group_id=2))
        assert not instance.worker_threads['GPU-0'].paused_flag.is_set()

    def test_a_schedule_due_now_runs(self, build_foreman, monkeypatch):
        instance = build_foreman()
        instance.last_schedule_run = 'never'
        now = foreman_module.datetime.today().strftime('%H:%M')
        FakeWorkerGroup.groups[0]['schedules'] = [{
            'schedule_time':         now,
            'repetition':            'daily',
            'schedule_task':         'count',
            'schedule_worker_count': 7,
        }]

        instance.manage_event_schedules()

        assert FakeWorkerGroup.groups[0]['number_of_workers'] == 7

    def test_a_schedule_for_another_time_does_not_run(self, build_foreman):
        instance = build_foreman()
        instance.last_schedule_run = 'never'
        FakeWorkerGroup.groups[0]['schedules'] = [{
            'schedule_time':         '00:01' if foreman_module.datetime.today().strftime(
                '%H:%M') != '00:01' else '00:02',
            'repetition':            'daily',
            'schedule_task':         'count',
            'schedule_worker_count': 7,
        }]

        instance.manage_event_schedules()

        assert FakeWorkerGroup.groups[0]['number_of_workers'] == 1


# ---------------------------------------------------------------------------
# Configuration change detection
# ---------------------------------------------------------------------------

@pytest.mark.unittest
class TestConfigurationChangeDetection:

    def test_an_unchanged_configuration_is_not_reported_as_changed(self, build_foreman):
        instance = build_foreman()

        assert instance.configuration_changed() is False

    def test_a_changed_library_configuration_invalidates_the_worker_config(self, build_foreman, foreman_env):
        """A configuration change mid-flight pauses the workers, so that a
        half-edited plugin flow is never picked up by a task."""
        instance = build_foreman()
        FakeLibrary.libraries = [{'id': 1}, {'id': 2}]

        assert instance.validate_worker_config() is False
        assert any(item['id'] == 'pluginSettingsChangeWorkersStopped' for item in foreman_env.added)

        # The change has been absorbed; the next pass is valid again
        assert instance.validate_worker_config() is True


# ---------------------------------------------------------------------------
# The run loop's error handling (issue #81)
# ---------------------------------------------------------------------------

class ExplodingPass(Exception):
    pass


@pytest.mark.unittest
class TestTheRunLoopErrorHandling:
    """`except Exception as e: raise Exception(e)` did two things wrong: it
    killed the only thread that claims tasks on the first exception of any
    kind, and it replaced the original exception - and its traceback - with a
    bare Exception raised from the handler."""

    def _foreman_with_failing_pass(self, build_foreman, failures, total_passes):
        instance = build_foreman()
        passes = {'count': 0}

        def failing_pass():
            passes['count'] += 1
            if passes['count'] >= total_passes:
                instance.abort_flag.set()
            if passes['count'] <= failures:
                raise ExplodingPass('the database is locked')

        instance.run_monitor_pass = failing_pass
        return instance, passes

    def test_a_transient_failure_is_absorbed_and_the_loop_continues(self, build_foreman):
        instance, passes = self._foreman_with_failing_pass(build_foreman, failures=1, total_passes=4)

        instance.run()

        assert passes['count'] == 4, (
            "one failed pass ended the Foreman monitor loop. The Foreman is the only thing "
            "that claims a task, so the installation stops processing files entirely."
        )

    def test_a_persistent_failure_stops_the_thread(self, build_foreman):
        """Absorbing forever is the other half of the bug: a fault that
        recurs every pass would spin here claiming nothing, and look exactly
        like an idle installation."""
        instance, passes = self._foreman_with_failing_pass(
            build_foreman, failures=99, total_passes=99)

        with pytest.raises(ExplodingPass):
            instance.run()

        assert passes['count'] == MAX_CONSECUTIVE_MONITOR_FAILURES

    def test_the_original_exception_and_traceback_survive(self, build_foreman):
        """`raise Exception(e)` threw away the type and the traceback, so the
        log said 'Exception' and pointed at the handler."""
        instance, _passes = self._foreman_with_failing_pass(
            build_foreman, failures=99, total_passes=99)

        with pytest.raises(ExplodingPass) as excinfo:
            instance.run()

        assert type(excinfo.value) is ExplodingPass, (
            "the Foreman re-raised a different exception than the one that was thrown"
        )
        frames = [frame.name for frame in excinfo.traceback]
        assert 'failing_pass' in frames, (
            "the traceback no longer reaches the code that actually failed"
        )

    def test_a_successful_pass_resets_the_failure_count(self, build_foreman):
        """Otherwise a fault every hour for a day is treated as a crash
        loop."""
        instance = build_foreman()
        passes = {'count': 0}

        # Fail, succeed, fail, succeed... never MAX in a row.
        def alternating_pass():
            passes['count'] += 1
            if passes['count'] >= (MAX_CONSECUTIVE_MONITOR_FAILURES * 4):
                instance.abort_flag.set()
            if passes['count'] % 2:
                raise ExplodingPass('transient')

        instance.run_monitor_pass = alternating_pass

        instance.run()  # must not raise

        assert passes['count'] == MAX_CONSECUTIVE_MONITOR_FAILURES * 4

    def test_a_clean_loop_exits_when_asked_to_stop(self, build_foreman):
        instance = build_foreman()
        passes = {'count': 0}

        def counting_pass():
            passes['count'] += 1
            if passes['count'] >= 3:
                instance.abort_flag.set()

        instance.run_monitor_pass = counting_pass
        instance.run()

        assert passes['count'] == 3
