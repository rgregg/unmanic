#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    taskfailure.py

    One vocabulary for "this task failed, and here is why" (issue #25).

    Before this module a failed task was a boolean and a wall of log text.
    The daemon knew perfectly well *why* it had failed - a plugin raised, a
    command exited non-zero, the stall detector killed the subprocess, the
    output failed its sanity check - and then threw that knowledge away.

    This module owns the whole lifecycle of that knowledge:

      record()  - while the task is running, into the in-memory task data
                  store (and the worker log, which is durable)
      read()    - at post-processing time, so it can be written to the
                  completed_tasks row
      the query helpers below - afterwards, for the UI and the retry guard

    ## Coordination with #15 (worker stall detector)

    #15 landed `Worker.record_task_failure()` explicitly as the seam for this
    issue, producing a `{category, message, timestamp}` record under
    `TASK_FAILURE_STATE_KEY`. That record shape and that key are kept exactly
    as they were; `Worker.record_task_failure()` now delegates here. There is
    deliberately no second notion of a failed task.

    ## First writer wins

    `record()` will not overwrite an existing record unless asked to. This
    matters for the stall case specifically: the stall detector records
    `stalled` from the monitor thread, and *then* the killed command exits
    non-zero and the ordinary path would record `command_failed`. The root
    cause is the stall; the non-zero exit is its symptom. Reporting the
    symptom would be a quieter and less useful answer, so the first record -
    the one closest to the cause - is the one that is kept.
"""

import datetime
import time

from trawlarr.libs.logs import TrawlarrLogging
from trawlarr.libs.task import TaskDataStore
from trawlarr.libs.unmodels import CompletedTasks

# The key #15 chose in the task data store. Unchanged on purpose.
TASK_FAILURE_STATE_KEY = 'task_failure'

# Failure categories. Deliberately a small closed set: a category is only
# useful if it groups things, and an open-ended string does not.
CATEGORY_STALLED = 'stalled'  # #15: no sign of life, subprocess killed
CATEGORY_PLUGIN_ERROR = 'plugin_error'  # a worker.process runner returned failure/raised
CATEGORY_COMMAND_FAILED = 'command_failed'  # the requested subprocess exited non-zero
CATEGORY_SANITY_CHECK = 'sanity_check'  # #35: output refused before delivery
CATEGORY_WORKER_TERMINATED = 'worker_terminated'  # worker was shut down mid-task
CATEGORY_OUTPUT_MISSING = 'output_missing'  # the final cache file could not be produced
CATEGORY_POSTPROCESSOR_ERROR = 'postprocessor_error'  # failed after the worker, during delivery
CATEGORY_CONFIGURATION = 'configuration'  # #40: refused before processing, a required plugin setting is unset
CATEGORY_UNKNOWN = 'unknown'  # failed, and nothing on the way down said why

KNOWN_CATEGORIES = (
    CATEGORY_STALLED,
    CATEGORY_PLUGIN_ERROR,
    CATEGORY_COMMAND_FAILED,
    CATEGORY_SANITY_CHECK,
    CATEGORY_WORKER_TERMINATED,
    CATEGORY_OUTPUT_MISSING,
    CATEGORY_POSTPROCESSOR_ERROR,
    CATEGORY_CONFIGURATION,
    CATEGORY_UNKNOWN,
)

# The message is shown in a table cell and stored in the DB. Long messages are
# truncated rather than dropped - a truncated reason still beats no reason.
MAX_MESSAGE_LENGTH = 2000

# How many consecutive failures of the same file before a retry is refused
# unless it is explicitly forced. See `evaluate_retry()`.
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

# Upper bound on how far back the consecutive-failure scan will walk. A file
# with more history than this is already well past any threshold.
_HISTORY_SCAN_LIMIT = 100

# How many paths to put in a single `IN (...)` clause when the health view
# asks which of them have processed successfully since. SQLite's default
# variable limit is 999.
_PATH_QUERY_BATCH = 400

logger = TrawlarrLogging.get_logger(name='TaskFailure')


def normalise_category(category):
    """
    Map an arbitrary category string onto the known set.

    An unrecognised category is reported as 'unknown' rather than stored
    verbatim, so that the UI and the summary counts never have to cope with a
    category nobody defined. The original is kept in the message by the
    caller if it matters.

    :param category:
    :return:
    """
    if not category:
        return CATEGORY_UNKNOWN
    value = str(category).strip().lower()
    if value in KNOWN_CATEGORIES:
        return value
    return CATEGORY_UNKNOWN


def truncate_message(message):
    """
    :param message:
    :return:
    """
    if message is None:
        return ''
    text = str(message).strip()
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    return text[:MAX_MESSAGE_LENGTH - 3] + '...'


def record(task_id, category, message, worker_log=None, overwrite=False):
    """
    Attach a failure category and message to a running task.

    Called from worker threads, the subprocess monitor thread and the
    postprocessor. It must never raise: a failure while recording a failure
    must not become a second, larger failure.

    :param task_id:   ID of the task being processed (None is tolerated)
    :param category:  one of KNOWN_CATEGORIES
    :param message:   human-readable explanation
    :param worker_log: optional list to append a visible banner to
    :param overwrite: replace an existing record. Default False; see the
                      module docstring on why the first record wins.
    :return: the record that is now in effect, or the new record if it could
             not be stored
    """
    new_record = {
        'category':  normalise_category(category),
        'message':   truncate_message(message),
        'timestamp': time.time(),
    }

    existing = read(task_id)
    if existing is not None and not overwrite:
        # Keep the earlier, more specific reason. Still note the later one in
        # the log so the sequence of events is not lost.
        _append_banner(worker_log,
                       "\n\nTASK FAILURE (already recorded as {}): {}\n".format(
                           existing.get('category'), new_record['message']))
        return existing

    # Loud in the log the user actually reads. A task that failed for a reason
    # nobody can see is the failure mode this whole issue is about.
    _append_banner(worker_log,
                   "\n\nTASK FAILED [{}]\n{}\n".format(new_record['category'].upper(), new_record['message']))

    if task_id is None:
        return new_record

    try:
        TaskDataStore.set_task_state(TASK_FAILURE_STATE_KEY, new_record, task_id=task_id)
    except Exception:
        logger.exception("Failed to record the task failure state for task %s", task_id)
    return new_record


def read(task_id):
    """
    Read back the failure record for a running task, if any.

    :param task_id:
    :return: dict or None
    """
    if task_id is None:
        return None
    try:
        value = TaskDataStore.get_task_state(TASK_FAILURE_STATE_KEY, task_id=task_id)
    except Exception:
        logger.exception("Failed to read the task failure state for task %s", task_id)
        return None
    if not isinstance(value, dict):
        return None
    return value


def _append_banner(worker_log, text):
    if worker_log is None:
        return
    try:
        worker_log.append(text)
    except Exception:
        logger.exception("Failed to append the task failure banner to the worker log")


def timestamp_to_datetime(timestamp):
    """
    Convert the epoch timestamp in a failure record to a datetime for storage.

    :param timestamp:
    :return: datetime or None
    """
    if timestamp is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(float(timestamp))
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def build_history_fields(task_id, task_success, abspath):
    """
    Produce the failure columns for a completed_tasks row.

    A successful task carries no failure fields. A failed task always carries
    them - if nothing on the way down recorded a reason we say so explicitly
    with the 'unknown' category, because "failed, reason not recorded" is
    honest and "failed" alone is what this issue exists to stop.

    :param task_id:
    :param task_success:
    :param abspath:
    :return: dict of column values
    """
    if task_success:
        return {
            'failure_category':  None,
            'failure_message':   None,
            'failure_time':      None,
            'failure_attempt':   None,
            'failure_dismissed': False,
        }

    failure = read(task_id) or {}
    category = normalise_category(failure.get('category'))
    message = truncate_message(failure.get('message'))
    if not message:
        message = ("No component reported a reason for this failure. "
                   "The task log is the only remaining diagnostic.")
    failure_time = timestamp_to_datetime(failure.get('timestamp')) or datetime.datetime.now()

    # This failure is the (N+1)th in a row for this file.
    attempt = consecutive_failure_count(abspath) + 1

    return {
        'failure_category':  category,
        'failure_message':   message,
        'failure_time':      failure_time,
        'failure_attempt':   attempt,
        'failure_dismissed': False,
    }


def consecutive_failure_count(abspath):
    """
    How many times in a row this exact path has failed, most recent first.

    Counted from history rather than carried on the pending task, which is
    what makes it survive a restart: there is no in-memory counter to lose.
    A success resets it to zero - a file that failed twice last month and has
    processed cleanly since is not a problem file.

    Deleting the history rows also resets it. That is intentional: deleting a
    completed task is the user saying the record no longer applies.

    :param abspath:
    :return: int
    """
    if not abspath:
        return 0
    try:
        query = (CompletedTasks
                 .select(CompletedTasks.task_success)
                 .where(CompletedTasks.abspath == abspath)
                 .order_by(CompletedTasks.finish_time.desc(), CompletedTasks.id.desc())
                 .limit(_HISTORY_SCAN_LIMIT))
        count = 0
        for row in query:
            if row.task_success:
                break
            count += 1
        return count
    except Exception:
        logger.exception("Failed to count consecutive failures for '%s'", abspath)
        return 0


def max_consecutive_failures(settings=None):
    """
    Read the retry threshold off the settings, defensively.

    :param settings:
    :return: int >= 1
    """
    value = DEFAULT_MAX_CONSECUTIVE_FAILURES
    getter = getattr(settings, 'get_max_consecutive_task_failures', None)
    if callable(getter):
        try:
            read_value = getter()
        except Exception:
            read_value = None
        if read_value is not None:
            value = read_value
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_CONSECUTIVE_FAILURES
    # A threshold of zero would refuse every retry, including the first one
    # after a one-off failure. Clamp rather than honour it.
    return max(1, value)


def evaluate_retry(abspath, settings=None, force=False):
    """
    Decide whether re-queueing this path is a reasonable thing to do.

    The point of the guard is the milestone's whole theme: a file that fails
    identically every time must not be quietly re-run forever. So retries are
    always user-initiated (nothing here re-queues anything automatically) and
    a file that has failed `max_consecutive_failures` times in a row is
    refused with the count and the last reason in the refusal, unless the
    caller explicitly forces it.

    Refusing loudly and being overridable is the deliberate choice over
    silently retrying, and also over refusing permanently: the user may well
    have just fixed the plugin.

    :param abspath:
    :param settings:
    :param force:
    :return: (allowed: bool, reason: str or None)
    """
    limit = max_consecutive_failures(settings)
    count = consecutive_failure_count(abspath)
    if count < limit:
        return True, None
    last = last_failure_for_path(abspath)
    detail = ''
    if last:
        detail = " Last failure ({}): {}".format(last.get('failure_category'), last.get('failure_message'))
    reason = ("'{}' has failed {} times in a row (limit {}). Retrying is unlikely to produce a different "
              "result until something changes.{} Retry again with force enabled to override.").format(
        abspath, count, limit, detail)
    if force:
        return True, reason
    return False, reason


def last_failure_for_path(abspath):
    """
    The most recent failed completed_tasks row for a path, as a dict.

    :param abspath:
    :return: dict or None
    """
    if not abspath:
        return None
    try:
        query = (CompletedTasks
                 .select()
                 .where((CompletedTasks.abspath == abspath) & (CompletedTasks.task_success == False))  # noqa: E712
                 .order_by(CompletedTasks.finish_time.desc(), CompletedTasks.id.desc())
                 .limit(1))
        for row in query:
            return {
                'id':               row.id,
                'failure_category': row.failure_category,
                'failure_message':  row.failure_message,
                'failure_time':     row.failure_time,
                'failure_attempt':  row.failure_attempt,
            }
    except Exception:
        logger.exception("Failed to read the last failure for '%s'", abspath)
    return None


def _row_order_key(finish_time, row_id):
    """
    The ordering used everywhere in this module: newest finish_time first,
    with the row id as the tie-break for rows written in the same second.

    :param finish_time:
    :param row_id:
    :return: (float, int)
    """
    stamp = 0.0
    if finish_time is not None:
        if hasattr(finish_time, 'timestamp'):
            try:
                stamp = finish_time.timestamp()
            except (ValueError, OSError, OverflowError):
                stamp = 0.0
        else:
            try:
                stamp = float(finish_time)
            except (TypeError, ValueError):
                stamp = 0.0
    try:
        row_id = int(row_id)
    except (TypeError, ValueError):
        row_id = 0
    return stamp, row_id


def _latest_success_keys(abspaths):
    """
    For each of the given paths, the order key of its most recent success.

    Paths that have never processed successfully are absent from the result.

    :param abspaths: iterable of paths
    :return: dict of path -> order key
    """
    latest = {}
    paths = sorted({p for p in abspaths if p})
    for offset in range(0, len(paths), _PATH_QUERY_BATCH):
        batch = paths[offset:offset + _PATH_QUERY_BATCH]
        query = (CompletedTasks
                 .select(CompletedTasks.id, CompletedTasks.abspath, CompletedTasks.finish_time)
                 .where((CompletedTasks.task_success == True) &  # noqa: E712
                        (CompletedTasks.abspath.in_(batch))))
        for row in query:
            key = _row_order_key(row.finish_time, row.id)
            if latest.get(row.abspath) is None or key > latest[row.abspath]:
                latest[row.abspath] = key
    return latest


def outstanding_failure_summary():
    """
    The health view: what is *currently* wrong and has not been acknowledged.

    "Outstanding" means failed, not dismissed, and not since superseded by a
    successful run of the same file. That last clause is the whole point of
    the view. Counting every failed row ever recorded would mean the number
    only ever goes up, that the first launch after an upgrade reports every
    historic failure as a live problem, and - directly against the principle
    `consecutive_failure_count()` already implements - that "a file that
    failed twice last month and has processed cleanly since" would still be
    reported as a problem file. It is not one; it is fixed.

    So a later success clears every earlier failure of that path from the
    health view, exactly as it resets the consecutive-failure count, and the
    two answers cannot disagree. The rows themselves are untouched: the
    history table still shows what happened, and the retry guard still reads
    it. This is a question about now, not about the past.

    Dismissal remains the way to silence a failure that has *not* been fixed.

    :return: dict
    """
    summary = {
        'total':      0,
        'categories': {},
        'oldest':     None,
        'newest':     None,
    }
    try:
        query = (CompletedTasks
                 .select()
                 .where((CompletedTasks.task_success == False) &  # noqa: E712
                        ((CompletedTasks.failure_dismissed == False) |  # noqa: E712
                         CompletedTasks.failure_dismissed.is_null(True)))
                 .order_by(CompletedTasks.finish_time.desc()))
        rows = list(query)
        # Only failures whose path has no more recent success survive. A row
        # with no path at all cannot be correlated with anything, so it is
        # kept - a failure we cannot prove is fixed is still a failure.
        latest_success = _latest_success_keys(row.abspath for row in rows)
        for row in rows:
            success_key = latest_success.get(row.abspath) if row.abspath else None
            failure_key = _row_order_key(row.finish_time, row.id)
            if success_key is not None and success_key > failure_key:
                continue
            summary['total'] += 1
            category = normalise_category(row.failure_category)
            summary['categories'][category] = summary['categories'].get(category, 0) + 1
            stamp = failure_key[0] if row.finish_time is not None else None
            if stamp is not None:
                if summary['newest'] is None or stamp > summary['newest']:
                    summary['newest'] = stamp
                if summary['oldest'] is None or stamp < summary['oldest']:
                    summary['oldest'] = stamp
    except Exception:
        logger.exception("Failed to build the outstanding task failure summary")
    return summary


def set_dismissed(id_list, dismissed=True):
    """
    Acknowledge (or un-acknowledge) failures without deleting them.

    Dismissal is deliberately not deletion. The diagnostics - category,
    message, timestamp, attempt count and the command log - all stay exactly
    where they were, and the retry guard still counts a dismissed failure.
    Dismissing means "I have seen this", not "this did not happen".

    :param id_list:
    :param dismissed:
    :return: number of rows updated
    """
    if not id_list:
        return 0
    try:
        return (CompletedTasks
                .update({CompletedTasks.failure_dismissed: bool(dismissed)})
                .where((CompletedTasks.id.in_(list(id_list))) &
                       (CompletedTasks.task_success == False))  # noqa: E712
                .execute())
    except Exception:
        logger.exception("Failed to update the dismissed state for completed tasks %s", id_list)
        return 0
