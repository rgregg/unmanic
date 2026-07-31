#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    supervisor.py

    Notices when one of the threads this application runs on has died, and
    makes that visible (see issue #81).

    THE PROBLEM
    -----------
    RootService starts seven threads and then waits for a signal. Every one
    of them is a daemon, so if one dies the process stays up, the web server
    keeps answering, and the installation quietly stops doing the thing it
    exists to do. There is no crash, no non-zero exit, and - worst of all -
    /activity/status goes on reporting a tidy `busy: false`, which is the
    answer an external maintenance script reads as "safe to proceed".

    THE POLICY, AND WHY
    -------------------
    Restart, bounded. Never restarting means one transient fault degrades
    the installation until a human happens to look; restarting forever means
    a thread that fails on its first statement burns a core doing it. So:
    up to MAX_RESTARTS_PER_WINDOW restarts of a given thread within
    RESTART_WINDOW_SECONDS. Exceed that and the supervisor stops trying and
    marks the thread failed for the life of the process - a crash loop is
    reported as a failure, not hidden by a restart that will not hold.

    Every one of those transitions is loud: an ERROR in the log, a UI
    notification, and an entry in trawlarr.libs.threadhealth that
    /activity/status refuses to answer over.

    Not every thread is restartable. UIServer is deliberately not: it binds
    a port, and a replacement racing the old one's socket is a worse failure
    than the one being repaired. It also does not need to be, because a dead
    web server is the one failure in this application that is already
    obvious from outside - the gate documented in docs/AUTOMATION.md gets a
    refused connection, and its documented behaviour there is to treat it as
    busy and not proceed.

    WHO WATCHES THE SUPERVISOR
    --------------------------
    Nothing, because it is not a thread. This class is driven synchronously
    by RootService.run() on the main thread - the thread that was previously
    parked in signal.pause() doing nothing at all. An unsupervised
    supervisor thread would be exactly the bug this file exists to fix, one
    level up. The main thread cannot fail silently: if it dies the process
    exits, because every other thread in the application is a daemon.
"""
from trawlarr.libs import threadhealth
from trawlarr.libs.logs import TrawlarrLogging
from trawlarr.libs.notifications import Notifications

#: How many times a single thread may be restarted inside the window below
#: before the supervisor concludes it is not coming back.
MAX_RESTARTS_PER_WINDOW = 3

#: The rolling window the restart budget is measured over (one hour). Long
#: enough that a fault repeating every few minutes exhausts the budget
#: instead of being papered over indefinitely; short enough that a genuinely
#: isolated fault months apart is still absorbed.
RESTART_WINDOW_SECONDS = 3600

#: Prefix for the per-thread notification uuid. Stable per thread, so a
#: second restart replaces the first notification rather than stacking.
NOTIFICATION_UUID_PREFIX = 'threadSupervisor'


class ThreadSupervisor(object):
    """
    Watches a list of ``{'name': ..., 'thread': ...}`` entries.

    The list is the one RootService already maintains, passed by reference:
    the supervisor reads it every pass, so a thread replaced by a restart is
    picked up without anything having to tell the supervisor about it.
    """

    def __init__(self, threads, restarters=None, notifications=None, registry=None,
                 max_restarts=MAX_RESTARTS_PER_WINDOW, window_seconds=RESTART_WINDOW_SECONDS):
        """
        :param threads:       the live list of {'name', 'thread'} dicts
        :param restarters:    name -> callable that starts a replacement and
                              re-registers it. A name absent from this map is
                              report-only: its death is recorded and notified,
                              but nothing is restarted.
        :param notifications: notification sink; defaults to Notifications()
        :param registry:      thread health registry; defaults to the shared one
        """
        self.threads = threads
        self.restarters = restarters or {}
        self.logger = TrawlarrLogging.get_logger(name=__class__.__name__)
        self._notifications = notifications
        self.registry = registry if registry is not None else threadhealth.get_registry()
        self.max_restarts = max_restarts
        self.window_seconds = window_seconds

    @property
    def notifications(self):
        # Resolved lazily: Notifications() is a singleton whose construction
        # should not be forced by merely building a supervisor.
        if self._notifications is None:
            self._notifications = Notifications()
        return self._notifications

    def run_pass(self):
        """
        Check every supervised thread once.

        Never raises: this runs on the main loop, and a supervisor that can
        take the process down over a bad entry is not a safety mechanism.

        :return: list of names found dead on this pass
        """
        died = []
        # Snapshot: restarting a thread re-registers it, which mutates the list.
        for entry in list(self.threads):
            try:
                name = entry.get('name')
                thread = entry.get('thread')
                if thread is not None and thread.is_alive():
                    continue
                died.append(name)
                self.handle_dead_thread(name, thread)
            except Exception:
                self.logger.exception("The thread supervisor failed while checking a thread")
        return died

    def handle_dead_thread(self, name, thread):
        """
        Deal with one thread found not alive: restart it, or give up loudly.

        :return: True if a replacement was started
        """
        # A thread we have already given up on stays given up on. Without this
        # the claim in threadhealth.record_failure() -- "terminal for the life
        # of the process" -- is simply untrue: the restart budget is a ROLLING
        # window, so once the old restart timestamps age out it refills and a
        # thread we permanently failed quietly starts being restarted again.
        # It also stops the supervisor re-running give_up() on every pass,
        # which at a 5s interval is 720 notification updates an hour for a
        # thread whose state has not changed since the first one.
        if self.registry.get_state(name) == threadhealth.STATE_FAILED:
            return False

        deaths = self.registry.record_death(name)
        self.logger.error(
            "Supervised thread '%s' is no longer running (death %s). Trawlarr cannot do its "
            "job with this thread missing.", name, deaths)
        TrawlarrLogging.metric("supervised_thread_died", thread_name=name, deaths=deaths)

        restarter = self.restarters.get(name)
        if restarter is None:
            self.give_up(name, "'{}' has no restart policy and is not running.".format(name))
            return False

        recent_restarts = self.registry.restarts_within(name, self.window_seconds)
        if recent_restarts >= self.max_restarts:
            self.give_up(
                name,
                "'{}' has died {} times in the last {} minutes and will not be restarted "
                "again.".format(name, recent_restarts + 1, int(self.window_seconds / 60)))
            return False

        # Best effort: let the dead thread release what it was holding. The
        # Foreman in particular owns worker threads that would otherwise be
        # left alive, waiting on a queue nothing feeds any more.
        self.quiesce(name, thread)

        try:
            replacement = restarter()
        except Exception as e:
            self.logger.exception("Failed to restart supervised thread '%s'", name)
            self.give_up(name, "'{}' could not be restarted: {}".format(name, e))
            return False

        if replacement is None:
            self.give_up(name, "'{}' could not be restarted: nothing was started.".format(name))
            return False

        attempt = self.registry.record_restart(name)
        self.logger.warning("Restarted supervised thread '%s' (restart %s of %s allowed in this window)",
                            name, attempt, self.max_restarts)
        TrawlarrLogging.metric("supervised_thread_restarted", thread_name=name, attempt=attempt)
        self.notify(
            name,
            'warning',
            "The '{}' thread stopped unexpectedly and has been restarted ({} of {} restarts "
            "allowed in an hour). Check the logs for the exception that killed it.".format(
                name, attempt, self.max_restarts))
        return True

    def quiesce(self, name, thread):
        """Ask a dead thread to release its resources. Failure is not fatal."""
        if thread is None:
            return
        stop = getattr(thread, 'stop', None)
        if not callable(stop):
            return
        try:
            stop()
        except Exception:
            self.logger.exception("Failed to stop the dead '%s' thread cleanly", name)

    def give_up(self, name, reason):
        """
        Mark a thread as permanently failed, and say so everywhere.

        This is the state the whole issue is about: from here on the
        installation must never present itself as healthy or idle.
        """
        self.registry.record_failure(name, reason)
        self.logger.error("Thread supervision has given up on '%s': %s", name, reason)
        TrawlarrLogging.metric("supervised_thread_failed", thread_name=name, reason=reason)
        self.notify(
            name,
            'error',
            "{} Trawlarr is running in a degraded state and will not process files correctly. "
            "Restart Trawlarr once you have dealt with the cause.".format(reason))

    def notify(self, name, notification_type, message):
        """Raise (or replace) the UI notification for this thread."""
        try:
            self.notifications.update({
                'uuid':       '{}{}'.format(NOTIFICATION_UUID_PREFIX, name),
                'type':       notification_type,
                'icon':       'report_problem',
                'label':      'threadSupervisorLabel',
                'message':    message,
                'navigation': {},
            })
        except Exception:
            self.logger.exception("Failed to raise the thread supervision notification for '%s'", name)


def supervise_until_shutdown(supervisor, shutdown_event, keep_running, interval=5):
    """
    Run supervision passes on the calling thread until asked to stop.

    Factored out of RootService.run() so it can be driven by a test without
    starting the application. `keep_running` is a callable rather than a flag
    so the loop sees the service's own run state.

    :param supervisor:     a ThreadSupervisor
    :param shutdown_event: threading.Event set by the signal handler
    :param keep_running:   callable returning False when the service should exit
    :param interval:       seconds between passes
    """
    while keep_running():
        try:
            supervisor.run_pass()
        except Exception:
            supervisor.logger.exception("Thread supervision pass failed")
        try:
            if shutdown_event.wait(interval):
                # The signal handler set the event; stop promptly rather than
                # sitting out the rest of the interval.
                break
        except (KeyboardInterrupt, SystemExit):
            break
