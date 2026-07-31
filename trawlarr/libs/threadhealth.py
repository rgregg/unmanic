#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    threadhealth.py

    One process-wide answer to "are the threads this application runs on
    still alive?", written by the supervisor and read by anything that would
    otherwise report a dead installation as a healthy one.

    It is a plain in-memory registry rather than a database table on purpose:
    the question is about *this* process, right now. A row surviving a
    restart would describe threads that no longer exist, and the first thing
    a restart does is start every thread again.

    The important consumer is trawlarr/webserver/helpers/activity.py. That
    endpoint is a gate for external maintenance tooling, and its failure mode
    is that "the pipeline is dead" and "the pipeline has nothing to do" look
    identical from the outside: no worker busy, no task moving. This registry
    is how the endpoint tells them apart.
"""
import threading
import time

#: Threads whose death makes the activity state undeterminable rather than
#: merely degraded. These three are the pipeline itself:
#:
#:   Foreman       - the only thing that ever claims a pending task
#:   PostProcessor - performs the file moves and source deletions, which is
#:                   the window an external gate most needs to know about
#:   TaskHandler   - moves discovered files onto the pending queue
#:
#: A dead LibraryScannerManager, EventMonitorManager, ScheduledTasksManager
#: or resource logger degrades the installation and is reported, but it does
#: not make "is Trawlarr touching my files right now?" unanswerable, so it
#: does not take the gate down with it.
CRITICAL_THREADS = frozenset({
    'Foreman',
    'PostProcessor',
    'TaskHandler',
})

#: The thread is running as far as the supervisor knows.
STATE_RUNNING = 'running'
#: The thread died and a replacement has been started in its place.
STATE_RESTARTED = 'restarted'
#: The thread is not running and the supervisor will not try again.
STATE_FAILED = 'failed'


class ThreadHealthRegistry(object):
    """
    Records what has happened to each supervised thread.

    Every method is safe to call from any thread. The registry is written by
    the supervisor (the main thread) and read by web request handlers, so
    that is not theoretical.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._threads = {}

    def _entry(self, name):
        entry = self._threads.get(name)
        if entry is None:
            entry = {
                'name':         name,
                'state':        STATE_RUNNING,
                'deaths':       0,
                'restarts':     [],
                'last_error':   None,
                'failure_time': None,
                'reason':       None,
            }
            self._threads[name] = entry
        return entry

    def record_started(self, name):
        """Note that `name` has been started for the first time."""
        with self._lock:
            entry = self._entry(name)
            entry['state'] = STATE_RUNNING

    def record_death(self, name, error=None):
        """Note that `name` was found not alive. Does not decide what to do."""
        with self._lock:
            entry = self._entry(name)
            entry['deaths'] += 1
            if error is not None:
                entry['last_error'] = str(error)
            return entry['deaths']

    def record_restart(self, name, timestamp=None):
        """Note that a replacement thread has been started for `name`."""
        with self._lock:
            entry = self._entry(name)
            entry['restarts'].append(time.time() if timestamp is None else timestamp)
            entry['state'] = STATE_RESTARTED
            return len(entry['restarts'])

    def record_failure(self, name, reason):
        """
        Note that `name` is not running and will not be restarted again.

        This is terminal for the life of the process. Nothing clears it,
        because nothing in the process can honestly say the thread came back.
        """
        with self._lock:
            entry = self._entry(name)
            entry['state'] = STATE_FAILED
            entry['reason'] = str(reason)
            entry['failure_time'] = time.time()

    def restarts_within(self, name, window_seconds, now=None):
        """How many restarts of `name` happened in the last `window_seconds`."""
        with self._lock:
            entry = self._entry(name)
            now = time.time() if now is None else now
            return len([t for t in entry['restarts'] if (now - t) <= window_seconds])

    def get_state(self, name):
        with self._lock:
            return self._entry(name)['state']

    def is_healthy(self, name):
        with self._lock:
            return self._entry(name)['state'] != STATE_FAILED

    def failed_threads(self):
        """Names of every thread the supervisor has given up on, sorted."""
        with self._lock:
            return sorted(name for name, entry in self._threads.items()
                          if entry['state'] == STATE_FAILED)

    def failed_critical_threads(self):
        """
        Names of the failed threads that make the pipeline state a guess.

        This is the one the activity endpoint asks.
        """
        return [name for name in self.failed_threads() if name in CRITICAL_THREADS]

    def report(self):
        """A copy of the whole registry, for logging and diagnostics."""
        with self._lock:
            return {name: dict(entry, restarts=list(entry['restarts']))
                    for name, entry in self._threads.items()}

    def reset(self):
        """Drop everything. For tests, and for a full service restart."""
        with self._lock:
            self._threads = {}


#: The registry the running application uses. A module-level object rather
#: than a SingletonType metaclass so that tests can reset it without having
#: to reach into a singleton registry.
_registry = ThreadHealthRegistry()


def get_registry():
    return _registry
