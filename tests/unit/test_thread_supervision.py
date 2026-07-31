#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_thread_supervision.py

    Issue #81: nothing supervised the seven threads this application runs on.
    A dead Foreman left the process up, the web server answering, and
    /activity/status reporting a tidy `busy: false` - which is the answer an
    external maintenance script reads as "safe to touch the library".

    The tests are grouped by the thing that can go wrong:

      1. the supervisor does not notice, or notices and does nothing
      2. the supervisor restarts a thread forever (a crash loop wearing a
         health check as a disguise)
      3. the failure is recorded but never surfaces anywhere a human or a
         script can see it
      4. /activity/status goes on answering 200 over a dead pipeline - the
         defect that makes this issue worse than a lost feature
      5. the scheduler thread dies on the first exception a scheduled job
         raises, taking completed-task cleanup with it

    Where a real object could be driven it was: the health registry, the
    supervisor and the activity helper are the production classes throughout.
    The threads are fakes, because the point is what happens when a thread is
    dead, and dead is easier to arrange than real.
"""
import threading
import time

import pytest

from trawlarr.libs import supervisor as supervisor_module
from trawlarr.libs import threadhealth
from trawlarr.libs.supervisor import ThreadSupervisor, supervise_until_shutdown
from trawlarr.libs.threadhealth import STATE_FAILED, STATE_RESTARTED, ThreadHealthRegistry
from trawlarr.webserver.helpers import activity


class FakeThread(object):
    """A thread that is exactly as alive as the test says it is."""

    def __init__(self, name='FakeThread', alive=True):
        self.name = name
        self.alive = alive
        self.stopped = False

    def is_alive(self):
        return self.alive

    def stop(self):
        self.stopped = True


class RecordingNotifications(object):
    """Stands in for the Notifications singleton."""

    def __init__(self):
        self.items = []

    def update(self, item):
        self.items.append(item)

    def by_uuid(self, uuid):
        return [item for item in self.items if item.get('uuid') == uuid]


@pytest.fixture
def registry():
    return ThreadHealthRegistry()


@pytest.fixture
def notifications():
    return RecordingNotifications()


@pytest.fixture(autouse=True)
def clean_shared_registry():
    """The activity helper reads the process-wide registry. Do not leak."""
    threadhealth.get_registry().reset()
    yield
    threadhealth.get_registry().reset()


def build_supervisor(threads, restarters, registry, notifications, **kwargs):
    return ThreadSupervisor(threads, restarters=restarters, notifications=notifications,
                            registry=registry, **kwargs)


# ---------------------------------------------------------------------------
# 1. Noticing at all
# ---------------------------------------------------------------------------

@pytest.mark.unittest
class TestTheSupervisorNoticesADeadThread:

    def test_a_healthy_pass_changes_nothing(self, registry, notifications):
        threads = [{'name': 'Foreman', 'thread': FakeThread(alive=True)}]
        restarts = []
        sup = build_supervisor(threads, {'Foreman': lambda: restarts.append(1)}, registry, notifications)

        assert sup.run_pass() == []
        assert restarts == [], "a living thread was restarted"
        assert notifications.items == [], "a living thread raised a notification"
        assert registry.is_healthy('Foreman')

    def test_a_dead_thread_is_reported_by_name(self, registry, notifications):
        threads = [{'name': 'Foreman', 'thread': FakeThread(alive=False)}]
        sup = build_supervisor(threads, {}, registry, notifications)

        assert sup.run_pass() == ['Foreman']

    def test_a_dead_restartable_thread_is_replaced_in_the_registry(self, registry, notifications):
        """The replacement has to end up in the list the supervisor reads, or
        the next pass finds the same corpse and restarts again forever."""
        dead = FakeThread(alive=False)
        threads = [{'name': 'Foreman', 'thread': dead}]

        def restart():
            replacement = FakeThread(alive=True)
            threads[0]['thread'] = replacement
            return replacement

        sup = build_supervisor(threads, {'Foreman': restart}, registry, notifications)
        sup.run_pass()

        assert threads[0]['thread'] is not dead
        assert registry.get_state('Foreman') == STATE_RESTARTED
        assert sup.run_pass() == [], "the replacement was not picked up on the next pass"

    def test_the_dead_thread_is_asked_to_release_its_workers_before_the_restart(self, registry, notifications):
        """A dead Foreman leaves worker threads alive waiting on a queue
        nothing feeds. Restarting without stopping them leaks a full set of
        workers per restart."""
        dead = FakeThread(alive=False)
        threads = [{'name': 'Foreman', 'thread': dead}]
        order = []

        def restart():
            order.append('restart')
            return FakeThread(alive=True)

        dead_stop = dead.stop

        def record_stop():
            order.append('stop')
            dead_stop()

        dead.stop = record_stop

        sup = build_supervisor(threads, {'Foreman': restart}, registry, notifications)
        sup.run_pass()

        assert dead.stopped, "the dead thread was never told to release its resources"
        assert order == ['stop', 'restart']

    def test_a_thread_that_cannot_be_inspected_does_not_take_the_loop_down(self, registry, notifications):
        """run_pass() runs on the main thread. If it can raise, a malformed
        entry ends the process."""

        class Exploding(object):
            def is_alive(self):
                raise RuntimeError('boom')

        threads = [{'name': 'Broken', 'thread': Exploding()},
                   {'name': 'Foreman', 'thread': FakeThread(alive=False)}]
        sup = build_supervisor(threads, {}, registry, notifications)

        assert sup.run_pass() == ['Foreman'], (
            "one bad entry stopped the supervisor from checking the rest"
        )


# ---------------------------------------------------------------------------
# 2. The restart budget
# ---------------------------------------------------------------------------

@pytest.mark.unittest
class TestTheRestartPolicyIsBounded:

    def _crash_looping_supervisor(self, registry, notifications, max_restarts=3):
        """A thread that is dead every time it is looked at."""
        threads = [{'name': 'Foreman', 'thread': FakeThread(alive=False)}]
        attempts = []

        def restart():
            attempts.append(time.time())
            replacement = FakeThread(alive=False)
            threads[0]['thread'] = replacement
            return replacement

        sup = build_supervisor(threads, {'Foreman': restart}, registry, notifications,
                               max_restarts=max_restarts)
        return sup, attempts

    def test_it_stops_restarting_after_the_budget_is_spent(self, registry, notifications):
        sup, attempts = self._crash_looping_supervisor(registry, notifications, max_restarts=3)

        for _ in range(10):
            sup.run_pass()

        assert len(attempts) == 3, (
            "the supervisor restarted a thread that dies immediately {} times. That is a crash "
            "loop, not a recovery.".format(len(attempts))
        )
        assert registry.get_state('Foreman') == STATE_FAILED

    def test_the_budget_is_a_rolling_window_not_a_lifetime_total(self, registry, notifications):
        """A single transient fault a month ago must not use up the budget
        for a fault today - that would be 'never restart' with extra steps."""
        sup, attempts = self._crash_looping_supervisor(registry, notifications, max_restarts=3)
        old = time.time() - (supervisor_module.RESTART_WINDOW_SECONDS * 2)
        for _ in range(5):
            registry.record_restart('Foreman', timestamp=old)

        sup.run_pass()

        assert len(attempts) == 1, (
            "restarts from outside the window counted against the budget"
        )

    def test_a_thread_with_no_restart_policy_is_failed_immediately(self, registry, notifications):
        """UIServer binds a port; a replacement racing the old socket is
        worse than the failure. It must still be reported."""
        threads = [{'name': 'UIServer', 'thread': FakeThread(alive=False)}]
        sup = build_supervisor(threads, {}, registry, notifications)

        sup.run_pass()

        assert registry.get_state('UIServer') == STATE_FAILED

    def test_a_restart_that_raises_is_a_failure_not_a_crash(self, registry, notifications):
        def restart():
            raise RuntimeError('cannot bind')

        threads = [{'name': 'Foreman', 'thread': FakeThread(alive=False)}]
        sup = build_supervisor(threads, {'Foreman': restart}, registry, notifications)

        sup.run_pass()

        assert registry.get_state('Foreman') == STATE_FAILED
        assert 'cannot bind' in registry.report()['Foreman']['reason']

    def test_a_restart_that_starts_nothing_is_a_failure(self, registry, notifications):
        """start_inotify_watch_manager() returns None when no event monitor
        module is available. Silently counting that as recovered would leave
        the thread permanently absent and permanently 'restarted'."""
        threads = [{'name': 'EventMonitorManager', 'thread': FakeThread(alive=False)}]
        sup = build_supervisor(threads, {'EventMonitorManager': lambda: None}, registry, notifications)

        sup.run_pass()

        assert registry.get_state('EventMonitorManager') == STATE_FAILED


# ---------------------------------------------------------------------------
# 3. Where the failure surfaces
# ---------------------------------------------------------------------------

@pytest.mark.unittest
class TestAFailureIsVisible:

    def test_a_restart_raises_a_warning_notification(self, registry, notifications):
        threads = [{'name': 'Foreman', 'thread': FakeThread(alive=False)}]
        sup = build_supervisor(threads, {'Foreman': lambda: FakeThread(alive=True)},
                               registry, notifications)

        sup.run_pass()

        raised = notifications.by_uuid('threadSupervisorForeman')
        assert raised, "a restarted thread produced no UI notification at all"
        assert raised[-1]['type'] == 'warning'
        assert 'Foreman' in raised[-1]['message']

    def test_giving_up_raises_an_error_notification(self, registry, notifications):
        threads = [{'name': 'Foreman', 'thread': FakeThread(alive=False)}]
        sup = build_supervisor(threads, {}, registry, notifications)

        sup.run_pass()

        raised = notifications.by_uuid('threadSupervisorForeman')
        assert raised and raised[-1]['type'] == 'error', (
            "an unrecoverable thread was not reported to the UI as an error"
        )

    def test_the_notification_uuid_is_stable_per_thread(self, registry, notifications):
        """Stable so repeated failures replace the notification rather than
        stacking a hundred copies of it in the tray."""
        threads = [{'name': 'Foreman', 'thread': FakeThread(alive=False)}]
        sup = build_supervisor(threads, {}, registry, notifications)

        sup.run_pass()
        sup.run_pass()

        uuids = {item['uuid'] for item in notifications.items}
        assert uuids == {'threadSupervisorForeman'}

    def test_a_broken_notification_sink_does_not_stop_supervision(self, registry):
        class BrokenNotifications(object):
            def update(self, item):
                raise RuntimeError('queue is broken')

        threads = [{'name': 'Foreman', 'thread': FakeThread(alive=False)}]
        sup = build_supervisor(threads, {}, registry, BrokenNotifications())

        sup.run_pass()

        assert registry.get_state('Foreman') == STATE_FAILED, (
            "the failure was not recorded because the notification failed"
        )


# ---------------------------------------------------------------------------
# 4. The activity gate must not read a dead pipeline as idle
# ---------------------------------------------------------------------------

class IdleForeman(object):
    """A Foreman with no work, of the shape the activity helper reads."""

    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive

    def get_all_worker_status(self):
        return [{'id': 'W0', 'name': 'Worker-W0', 'idle': True, 'paused': False}]


@pytest.fixture
def idle_installation(monkeypatch):
    """Nothing running, nothing queued: the state that reads `busy: false`."""
    from trawlarr.libs.uiserver import TrawlarrRunningTreads

    def _install(foreman):
        monkeypatch.setattr(
            TrawlarrRunningTreads, 'get_unmanic_running_thread',
            lambda self, name: foreman if name == 'foreman' else None)
        monkeypatch.setattr(activity, 'count_tasks_by_status',
                            lambda: {'pending': 0, 'in_progress': 0, 'processed': 0})
        return foreman

    return _install


@pytest.mark.unittest
class TestTheActivityGateRefusesToCallADeadPipelineIdle:

    def test_an_idle_installation_is_still_reported_as_idle(self, idle_installation):
        """The control. Everything below must not be achieved by simply
        breaking the endpoint."""
        idle_installation(IdleForeman(alive=True))

        assert activity.get_activity_status()['busy'] is False

    def test_a_dead_foreman_thread_is_an_error_not_an_idle(self, idle_installation):
        """The exact shape of the bug: no worker busy, no task queued, and
        the one thread that could change either of those is gone."""
        idle_installation(IdleForeman(alive=False))

        with pytest.raises(activity.ActivityStateUnavailableError):
            activity.get_activity_status()

    def test_a_failed_pipeline_thread_is_an_error_even_with_a_live_foreman(self, idle_installation):
        """The PostProcessor is the thread that performs the file moves and
        the source deletions. Dead, it takes the answer with it."""
        idle_installation(IdleForeman(alive=True))
        threadhealth.get_registry().record_failure('PostProcessor', 'crash loop')

        with pytest.raises(activity.ActivityStateUnavailableError) as excinfo:
            activity.get_activity_status()
        assert 'PostProcessor' in str(excinfo.value), (
            "the error does not name the thread, so an operator reading the "
            "maintenance script's log cannot tell what broke"
        )

    def test_a_failed_non_pipeline_thread_still_answers(self, idle_installation):
        """Conservative, not paranoid. A dead resource logger does not make
        'is Trawlarr touching my files' unanswerable, and turning every
        degradation into a 500 would train operators to ignore the gate."""
        idle_installation(IdleForeman(alive=True))
        threadhealth.get_registry().record_failure('RootServiceResourceLogger', 'crash loop')

        assert activity.get_activity_status()['busy'] is False

    def test_supervision_of_a_dead_foreman_closes_the_gate(self):
        """End to end over the real objects: a Foreman that will not stay up,
        supervised to exhaustion, must leave /activity/status refusing to
        answer."""
        threads = [{'name': 'Foreman', 'thread': FakeThread(alive=False)}]
        sup = ThreadSupervisor(threads, restarters={}, notifications=RecordingNotifications())

        sup.run_pass()

        assert threadhealth.get_registry().failed_critical_threads() == ['Foreman']


# ---------------------------------------------------------------------------
# 5. The scheduler's unguarded run_pending()
# ---------------------------------------------------------------------------

class CountingEvent(object):
    """An event that ends the loop under test after a few waits."""

    def __init__(self, stop_after, abort_flag):
        self.waits = 0
        self.stop_after = stop_after
        self.abort_flag = abort_flag

    def wait(self, _timeout=None):
        self.waits += 1
        if self.waits >= self.stop_after:
            self.abort_flag.set()
        return True

    def is_set(self):
        return False


@pytest.mark.unittest
class TestTheSchedulerSurvivesAFailingJob:
    """ScheduledTasksManager.run() had no try/except around
    scheduler.run_pending() at all. The jobs it runs hit the database and the
    network; the first exception ended the thread, and with it every future
    plugin repo update and completed-task cleanup, silently."""

    def _manager_with_failing_scheduler(self, ticks):
        import logging

        from trawlarr.libs.scheduler import ScheduledTasksManager

        manager = ScheduledTasksManager.__new__(ScheduledTasksManager)
        manager.logger = logging.getLogger('test_thread_supervision')
        manager.abort_flag = threading.Event()
        manager.event = CountingEvent(stop_after=ticks, abort_flag=manager.abort_flag)
        manager.manage_completed_tasks = lambda: None

        calls = []

        class FailingScheduler(object):
            def every(self, *args, **kwargs):
                return self

            def hours(self):
                return self

            def do(self, job):
                return job

            def run_pending(self):
                calls.append(1)
                raise RuntimeError('the scheduled job exploded')

            def clear(self):
                pass

        # `every(3).hours.do(...)` - hours is a property on the real Job
        FailingScheduler.hours = property(lambda self: self)
        manager.scheduler = FailingScheduler()
        return manager, calls

    def test_a_failing_scheduled_job_does_not_kill_the_thread(self):
        manager, calls = self._manager_with_failing_scheduler(ticks=6)

        manager.run()

        assert len(calls) > 1, (
            "the scheduler loop ran run_pending() once and stopped. An "
            "exception from a scheduled job still kills the thread, and with "
            "it completed-task cleanup for the life of the process."
        )

    def test_the_loop_still_exits_when_asked_to_stop(self):
        manager, _calls = self._manager_with_failing_scheduler(ticks=4)

        manager.run()

        assert manager.abort_flag.is_set()


# ---------------------------------------------------------------------------
# 6. The supervision loop itself
# ---------------------------------------------------------------------------

@pytest.mark.unittest
class TestTheSupervisionLoop:

    def test_it_runs_passes_until_the_service_stops(self):
        passes = []
        running = {'value': True}

        class CountingSupervisor(object):
            logger = None

            def run_pass(self):
                passes.append(1)
                if len(passes) >= 3:
                    running['value'] = False

        supervise_until_shutdown(CountingSupervisor(), threading.Event(),
                                 lambda: running['value'], interval=0)

        assert len(passes) == 3

    def test_it_returns_immediately_when_the_shutdown_event_is_set(self):
        """The signal handler sets the event; shutdown must not wait out the
        rest of the supervision interval."""
        passes = []

        class CountingSupervisor(object):
            logger = None

            def run_pass(self):
                passes.append(1)

        shutdown = threading.Event()
        shutdown.set()

        started = time.time()
        supervise_until_shutdown(CountingSupervisor(), shutdown, lambda: True, interval=30)

        assert len(passes) == 1
        assert (time.time() - started) < 5, "shutdown waited out the supervision interval"

    def test_a_failing_pass_does_not_end_supervision(self):
        import logging

        attempts = []
        running = {'value': True}

        class BrokenSupervisor(object):
            logger = logging.getLogger('test_thread_supervision')

            def run_pass(self):
                attempts.append(1)
                if len(attempts) >= 3:
                    running['value'] = False
                raise RuntimeError('supervision itself broke')

        supervise_until_shutdown(BrokenSupervisor(), threading.Event(),
                                 lambda: running['value'], interval=0)

        assert len(attempts) == 3, (
            "an exception from one supervision pass ended supervision, which "
            "leaves the threads unwatched again"
        )


# ---------------------------------------------------------------------------
# 5. Giving up means giving up
# ---------------------------------------------------------------------------

@pytest.mark.unittest
class TestGivingUpIsActuallyTerminal:
    """`threadhealth.record_failure()` documents STATE_FAILED as "terminal for
    the life of the process. Nothing clears it."

    That was not true when written. `handle_dead_thread()` never consulted the
    current state, and the restart budget is a ROLLING window -- so once the
    old restart timestamps aged out of it, the budget refilled and a thread the
    supervisor had permanently failed quietly started being restarted again. A
    docstring making a false promise about a safety mechanism is worse than no
    docstring: it is the thing a reader checks instead of the code.

    The same early-out is what stops give_up() re-firing on every pass. At the
    5s supervision interval that was 720 notification updates an hour about a
    thread whose state had not changed since the first one.
    """

    def _crash_looping_supervisor(self, registry, notifications, max_restarts=3):
        threads = [{'name': 'Foreman', 'thread': FakeThread(alive=False)}]
        attempts = []

        def restart():
            attempts.append(time.time())
            replacement = FakeThread(alive=False)
            threads[0]['thread'] = replacement
            return replacement

        sup = build_supervisor(threads, {'Foreman': restart}, registry, notifications,
                               max_restarts=max_restarts)
        return sup, attempts

    def test_a_failed_thread_stays_failed_when_the_budget_window_rolls(self, registry, notifications):
        sup, attempts = self._crash_looping_supervisor(registry, notifications, max_restarts=3)
        for _ in range(10):
            sup.run_pass()
        assert registry.get_state('Foreman') == STATE_FAILED
        spent = len(attempts)

        # Age every recorded restart out of the rolling window. This is exactly
        # what the passage of an hour does, with no other state change.
        registry.forget_restarts('Foreman')

        for _ in range(10):
            sup.run_pass()

        assert len(attempts) == spent, (
            "a thread the supervisor permanently failed was restarted again once "
            "the rolling budget window refilled, so STATE_FAILED is not terminal"
        )
        assert registry.get_state('Foreman') == STATE_FAILED

    def test_giving_up_is_announced_once_not_on_every_pass(self, registry, notifications):
        sup, _ = self._crash_looping_supervisor(registry, notifications, max_restarts=3)
        for _ in range(10):
            sup.run_pass()
        after_give_up = len(notifications.items)

        for _ in range(50):
            sup.run_pass()

        assert len(notifications.items) == after_give_up, (
            "give_up() re-fired on later supervision passes. At a 5s interval that is "
            "720 notification updates an hour saying nothing new."
        )
