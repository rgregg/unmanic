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
"""
from peewee import fn

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
    urt = TrawlarrRunningTreads()
    foreman = urt.get_unmanic_running_thread('foreman')
    if foreman is None:
        raise ActivityStateUnavailableError(
            "The Foreman thread is not running. Trawlarr's activity state cannot be determined."
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
