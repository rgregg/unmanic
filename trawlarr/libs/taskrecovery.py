#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    taskrecovery.py

    Recovery for tasks stranded in 'in_progress' by a process that stopped
    while it was holding them (issue #83).

    ## The defect

    Claiming a task is a one-way door. `taskqueue.get_next_pending_tasks()`
    moves a row from 'pending' to 'in_progress' and only the worker that
    claimed it ever moves it on. Kill the process in between - a container
    restart, an OOM kill, `docker compose down` mid-transcode - and the row
    stays claimed forever. It is never retried, never surfaced, never cleaned
    up. Worse, it is invisible in both directions: #33's done-state says the
    file was never completed, and the task row says it is already being
    handled, so no library scan will offer it again. And #42's busy/idle gate
    counts 'in_progress', so one stranded row means the installation never
    reports idle again and anything gated on that waits forever.

    ## Telling a stranded task from a live one

    This is the whole problem, and the answer here is timing, not inspection.

    Workers are threads of the daemon process. The Foreman is the only thing
    that ever claims a task, and `RootService.start_threads()` is the only
    thing that starts a Foreman. So reconciliation runs exactly once, from
    inside `start_threads()`, after the database is open and *before* the
    Foreman thread exists. At that instant this process has zero workers, so
    an 'in_progress' row cannot belong to a live worker of this process. No
    heartbeat, no PID column, no "claimed more than an hour ago" timeout - and
    therefore no way to be wrong about a task that is legitimately running,
    which would be a far worse bug than the stranded row.

    The one case that timing does not cover is a *second* Trawlarr process
    sharing the same config directory. That is not a supported deployment and
    was already fatal before this module: `start_threads()` deletes everything
    in the shared cache directory on startup, which destroys the in-flight
    output of any other instance a few lines above where this now runs. This
    check inherits exactly that assumption and adds no new one.

    What this deliberately does NOT cover, so nobody mistakes it for a
    watchdog: a worker thread that dies while the daemon keeps running. That
    is #15's territory (the stall detector, which watches the subprocess) and
    is not detected here until the next restart.

    ## Where a stranded task goes

    Not silently back to 'pending'. An interrupted task is a failure like any
    other and is written to the completed-tasks history with #25's failure
    vocabulary, under the 'interrupted' category, so it shows up in the health
    view and counts toward the consecutive-failure guard.

    Whether the file then goes back on the queue is decided by #25's
    consecutive-failure threshold - the same budget that governs every other
    kind of failure of the same file, and which any success in between clears.
    That matters for the case this feature could otherwise make worse: a file
    whose transcode reliably OOM-kills the container would be re-queued on
    every boot, killing the container again, forever. Counting this
    interruption against that budget means the third one is left failed
    instead, for a human to look at.

    A file whose source has since disappeared is never re-queued either -
    there is nothing left to process.

    Either way the 'in_progress' row is gone by the time the Foreman starts,
    which is what unblocks #42's idle gate.
"""

import datetime
import os

from trawlarr.libs import history, taskfailure
from trawlarr.libs.logs import TrawlarrLogging
from trawlarr.libs.notifications import Notifications
from trawlarr.libs.unmodels.tasks import Tasks

logger = TrawlarrLogging.get_logger(name='TaskRecovery')

# The status a claim moves a task into, and the status it is returned to.
CLAIMED_STATUS = 'in_progress'
QUEUED_STATUS = 'pending'


def _failure_message(abspath, reason):
    return ("Trawlarr stopped while this task was being processed, so it was never completed. "
            "{} (file: {})".format(reason, abspath))


def _record_interruption(row, reason, attempt):
    """
    Write the interrupted task into the completed-tasks history as a failure.

    :param row: the Tasks row
    :param reason: what is being done about it, for the stored message
    :param attempt: which consecutive failure of this file this is
    :return: True when the history row was written
    """
    abspath = row.abspath or ''
    message = _failure_message(abspath, reason)
    task_history = {
        'task_label':          os.path.basename(abspath),
        'abspath':             abspath,
        'task_success':        False,
        'start_time':          row.start_time,
        'finish_time':         datetime.datetime.now(),
        'processed_by_worker': row.processed_by_worker or '',
        'log':                 message,
        'failure_category':    taskfailure.CATEGORY_INTERRUPTED,
        'failure_message':     taskfailure.truncate_message(message),
        'failure_time':        datetime.datetime.now(),
        'failure_attempt':     attempt,
        'failure_dismissed':   False,
    }
    return history.History().save_task_history(task_history)


def _requeue(row):
    """
    Put a stranded task back in the pending queue.

    A conditional update for the same reason the claim is one: only a row that
    is still in the state we read is safe to move.

    :param row:
    :return: True when the row was re-queued
    """
    updated = (Tasks
               .update({Tasks.status: QUEUED_STATUS, Tasks.processed_by_worker: None})
               .where((Tasks.id == row.id) & (Tasks.status == CLAIMED_STATUS))
               .execute())
    return bool(updated)


def _abandon(row):
    """
    Drop a stranded task that is not going to be re-queued.

    The task row has to go: leaving it 'in_progress' is the bug, and there is
    no 'failed' status in the task table. The failure is not lost - it was
    written to the completed-tasks history first, which is also what
    `FileTest.file_failed_in_history()` reads, so the next library scan will
    not silently offer the file back.

    :param row:
    :return: True when the row was removed
    """
    deleted = (Tasks
               .delete()
               .where((Tasks.id == row.id) & (Tasks.status == CLAIMED_STATUS))
               .execute())
    return bool(deleted)


def _decide(row, settings, attempt):
    """
    Re-queue this interrupted task, or leave it failed?

    The threshold is #25's, so an interruption counts toward the same
    consecutive-failure budget as every other kind of failure of the same
    file, and a success in between clears it.

    :param row:
    :param settings:
    :param attempt: which consecutive failure of this file this interruption
                    is - counted with this one included, since its history row
                    has not been written yet
    :return: (requeue: bool, reason: str)
    """
    abspath = row.abspath or ''
    if not abspath or not os.path.exists(abspath):
        return False, "Its source file is no longer at that path, so it has not been queued again."
    limit = taskfailure.max_consecutive_failures(settings)
    if attempt >= limit:
        return False, ("It has not been queued again: this file has now failed {} times in a row (limit {}), "
                       "so re-queueing it on every restart would only repeat whatever is interrupting it. "
                       "Retry it from the completed tasks list once the cause is understood.".format(attempt, limit))
    return True, "It has been queued again and will be processed from the start."


def reconcile_interrupted_tasks(settings=None):
    """
    Reconcile every claimed task against the workers that exist right now.

    MUST be called before any Foreman is started; see the module docstring for
    why that ordering is the entire correctness argument.

    Never raises. A failure to recover tasks must not stop the daemon from
    starting - the operator can still see the log, and a daemon that refuses
    to boot helps nobody.

    :param settings: Config, for the consecutive-failure threshold
    :return: dict summary of what was done
    """
    summary = {'found': 0, 'requeued': [], 'failed': [], 'errors': 0}
    try:
        rows = list(Tasks.select().where(Tasks.status == CLAIMED_STATUS))
    except Exception:
        logger.exception("Unable to read claimed tasks. Interrupted tasks have NOT been recovered.")
        summary['errors'] += 1
        return summary

    summary['found'] = len(rows)
    if not rows:
        logger.debug("No tasks were left claimed by a previous run.")
        return summary

    logger.warning("Found %s task(s) left claimed by a previous run of Trawlarr. "
                   "These were interrupted before they completed and are being recovered.", len(rows))

    for row in rows:
        abspath = row.abspath or ''
        try:
            # This interruption's ordinal in the file's run of consecutive
            # failures. Its own history row does not exist yet, hence the +1;
            # both the decision and the recorded attempt number use it, so
            # they cannot disagree.
            attempt = taskfailure.consecutive_failure_count(abspath) + 1
            requeue, reason = _decide(row, settings, attempt)
            # Recorded whatever the decision is: an interruption that put the
            # file straight back on the queue still has to be visible, or the
            # thing interrupting every restart is never noticed.
            if not _record_interruption(row, reason, attempt):
                logger.error("Failed to record the interruption of '%s' in the task history.", abspath)
                summary['errors'] += 1
            if requeue and _requeue(row):
                logger.warning("Interrupted task for '%s' has been queued again.", abspath)
                summary['requeued'].append(abspath)
            else:
                if requeue:
                    reason = "It could not be queued again and has been removed from the task queue."
                _abandon(row)
                logger.error("Interrupted task for '%s' was not queued again. %s", abspath, reason)
                summary['failed'].append(abspath)
        except Exception:
            logger.exception("Failed to recover the interrupted task for '%s'", abspath)
            summary['errors'] += 1

    _notify(summary)
    return summary


def _notify(summary):
    """
    Raise the same UI notification a failed task raises.

    An interruption the operator never hears about is the failure mode this
    issue is about, and the completed-tasks list is where the record now is.

    :param summary:
    :return:
    """
    if not summary['requeued'] and not summary['failed']:
        return
    try:
        Notifications().add({
            'uuid':       'interruptedTasks',
            'type':       'warning',
            'icon':       'restore',
            'label':      'failedTaskLabel',
            'message':    ('{} task(s) were interrupted by a previous shutdown. {} queued again, '
                           '{} left failed. See your completed tasks list.').format(
                summary['found'], len(summary['requeued']), len(summary['failed'])),
            'navigation': {
                'push':   '/ui/dashboard',
                'events': [
                    'completedTasksShowFailed',
                ],
            },
        })
    except Exception:
        logger.exception("Failed to raise the interrupted task notification")
