#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    activity.py

    "Is Trawlarr doing anything right now?", answered once, in one place.

    External maintenance tooling that shares storage or a GPU with Trawlarr
    needs to gate on this. Before this helper existed the only way to ask was
    to walk /proc looking for an ffmpeg process that lacked ``-nostdin``,
    which is a dependency on a private implementation detail of how Trawlarr
    invokes its own subprocesses.

    Two deliberate choices, both of which err towards reporting *busy*:

    1. Worker idleness alone is not enough. When a worker finishes a file the
       task moves to the 'processed' status and the PostProcessor picks it up,
       and *that* is when the large file copies and source deletions happen.
       For the entire post-processing window every worker reports idle while
       Trawlarr is still writing to the library. A gate built on worker state
       alone would green-light a maintenance run in exactly that window.

    2. If the state cannot be determined, that is an error, never an "idle".
       An unknown answer reported as False here becomes two processes writing
       to the same file out in the world.

    The second of those is why this helper asks about thread health at all
    (issue #81). "Idle because there is no work" and "idle because the thing
    that does the work is dead" produce identical numbers: no busy worker, no
    task moving out of pending. Reported as `busy: false`, the second is the
    worst answer this endpoint can give - the gate opens precisely because
    the application has broken. So a dead pipeline thread is an unknown
    state, and an unknown state is a 500.
"""
from peewee import fn

from trawlarr.libs import threadhealth
from trawlarr.libs.uiserver import TrawlarrRunningTreads
from trawlarr.libs.unmodels import Tasks

# The task statuses that mean "Trawlarr owns this file right now".
#   pending     - queued, a worker may claim it at any moment
#   in_progress - assigned to a worker, being transcoded
#   processed   - transcoded, awaiting/undergoing post-processing file moves
ACTIVE_TASK_STATUSES = ('pending', 'in_progress', 'processed')


class ActivityStateUnavailableError(Exception):
    """Raised when the activity state cannot be determined at all."""


def count_tasks_by_status():
    """
    Return a count of task queue entries for each active status.

    Every status in ACTIVE_TASK_STATUSES is always present in the result, so
    callers never have to distinguish "zero" from "key missing".

    :return: dict of status name -> int
    """
    counts = {status: 0 for status in ACTIVE_TASK_STATUSES}
    query = (Tasks
             .select(Tasks.status, fn.COUNT(Tasks.id).alias('count'))
             .where(Tasks.status.in_(list(ACTIVE_TASK_STATUSES)))
             .group_by(Tasks.status))
    for row in query:
        # Defensive: only count statuses we advertise.
        if row.status in counts:
            counts[row.status] = int(row.count)
    return counts


def get_activity_status():
    """
    Return the current pipeline activity of this installation.

    :return: dict
    :raises ActivityStateUnavailableError: if the Foreman is not running, and
        the answer would therefore be a guess.
    """
    # Threads the supervisor has given up on. Asked first, because a thread
    # that will not stay up makes every number below a description of a
    # pipeline that is not running.
    failed = threadhealth.get_registry().failed_critical_threads()
    if failed:
        raise ActivityStateUnavailableError(
            "Trawlarr is running in a degraded state: the {} thread(s) are not running and could "
            "not be restarted. The activity state cannot be determined, and this installation is "
            "not processing files.".format(', '.join(failed))
        )

    urt = TrawlarrRunningTreads()
    foreman = urt.get_unmanic_running_thread('foreman')
    if foreman is None:
        raise ActivityStateUnavailableError(
            "The Foreman thread is not running. Trawlarr's activity state cannot be determined."
        )

    # Asked directly as well as through the registry, so that the window
    # between a thread dying and the next supervision pass is not a window in
    # which this endpoint reports a tidy `busy: false`.
    is_alive = getattr(foreman, 'is_alive', None)
    if callable(is_alive) and not is_alive():
        raise ActivityStateUnavailableError(
            "The Foreman thread has died. Nothing is claiming tasks, so the workers below are "
            "idle for a reason that is not safe to read as idle."
        )

    all_worker_status = foreman.get_all_worker_status()
    workers_total = len(all_worker_status)
    # A worker is busy when it is not idle. A worker holding a task while
    # paused still counts as busy - it has a cache file open.
    workers_busy = len([worker for worker in all_worker_status if not worker.get('idle', True)])
    workers_paused = len([worker for worker in all_worker_status if worker.get('paused', False)])

    task_counts = count_tasks_by_status()

    # 'busy' is the conservative single-field answer an external gate reads.
    # It is False only when nothing is running AND nothing is queued to run.
    busy = bool(workers_busy) or any(count > 0 for count in task_counts.values())

    return {
        'busy':    busy,
        'workers': {
            'total':  workers_total,
            'busy':   workers_busy,
            'paused': workers_paused,
        },
        'tasks':   {
            'pending':     task_counts['pending'],
            'in_progress': task_counts['in_progress'],
            'processed':   task_counts['processed'],
        },
    }
