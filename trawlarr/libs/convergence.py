#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    trawlarr.libs.convergence.py

    Did the task actually achieve anything? (issue #34)

    THE INCIDENT
    ------------
    From docs/FIELD-ISSUES.md 3.1: files "landed in completed-task history with
    no AAC track and no signal from the queue that anything was wrong". The
    task ran, the plugin quietly did nothing useful, the task reported success,
    and the file was filed away as processed. Nothing in the pipeline ever
    asked the obvious question afterwards: *does this file still match the
    criteria we queued it for?* Two of the worst incidents in the field log
    were invisible for weeks purely because nobody asked.

    WHAT THIS MODULE ASKS, AND OF WHOM
    ----------------------------------
    After a task completes and its output has been delivered, the library's
    file-test PLUGINS are re-run against the delivered file - tiers 1-3 of the
    precedence model, via FileTest.run_file_test_plugins(). If they still vote
    "queue this file", the file did not converge.

    Deliberately NOT the full FileTest. Tier 0 (issue #33) is a native gate
    that answers "is this file in scope, and has it already been completed?".
    The post-processor has just recorded this exact file as completed, so the
    full test would answer "do not queue" for every file that has ever
    finished a task and would report perfect convergence forever - a check
    that is always green, which is worse than no check at all because it also
    carries the appearance of one. The question convergence asks lives
    entirely in the plugin layer, so it is asked of the plugin layer.

    WHAT HAPPENS ON A NON-CONVERGED FILE
    ------------------------------------
    Recorded, counted and surfaced. Explicitly NOT:

      * re-queued. Silent re-queue is the reprocess loop (1.1, 3.4) and it is
        the thing that made these incidents expensive: the loop consumes GPU
        time indefinitely while looking, from the dashboard, like activity.
        The file stays recorded as done by issue #33; a human decides.
      * recorded as a task failure. The task did not fail. It ran, it
        succeeded, it delivered a file. Filing this under the #25 failure
        categories would mean the retry guard refuses re-queues of a file that
        never failed, the health view's failure count stops meaning "something
        broke", and FileTest.file_failed_in_history() blacklists a perfectly
        good file. Those are three different wrong answers, so this is its own
        state with its own table and its own endpoint.

    THE OCCURRENCE COUNT
    --------------------
    A single non-converged completion can be legitimate: a plugin flow that
    genuinely needs two passes, a file the operator re-queued by hand while
    mid-way through changing the library's target format. What is not
    legitimate is the same file completing again and again and never
    converging - that is a configuration or plugin bug, and the report says so
    once `occurrences` reaches REPEAT_LIMIT.

    Note that with issue #33 in place the count can only advance when somebody
    (or issue #41's reprocess action) deliberately runs the file again. That is
    the correct shape: the count measures "we tried again and it still did not
    work", which is exactly when the confident wording is earned.

    HONESTY ABOUT WHAT THIS DOES NOT CATCH
    --------------------------------------
      * A library whose file-test plugins are all guards and filters - nothing
        that ever votes True - has no criteria to converge against. Every file
        is reported as not evaluated, and nothing is recorded. That is not a
        clean bill of health and this module does not pretend otherwise.
      * A plugin that votes True unconditionally will mark every file
        non-converged. That IS a reprocess loop by any other name, and naming
        the plugin in the report is the useful answer; but it will be loud on
        such a library, and it is worth saying plainly that the loudness is
        proportional to how badly the library is configured, not to how much
        damage is being done.
      * Convergence is judged by the same criteria that queued the file. It
        cannot notice a plugin that damaged the file in a way the criteria do
        not describe. That is the output sanity checks' job (issue #35), and
        the two are complementary, not redundant.
"""

import datetime

from trawlarr.libs.logs import TrawlarrLogging
from trawlarr.libs.unmodels import FileConvergenceState

#: The file no longer matches the library's plugin criteria. Normal, quiet.
STATE_CONVERGED = 'converged'
#: The file still matches them. Something is wrong.
STATE_NOT_CONVERGED = 'not_converged'
#: The question could not be asked - no file, no criteria, or the file test
#: raised. Never reported as either of the above; see the module docstring.
STATE_NOT_EVALUATED = 'not_evaluated'

#: Consecutive non-converged completions of one file after which the report
#: stops calling it bad luck and starts calling it a bug.
REPEAT_LIMIT = 2

#: Cap on the size of the health view's file list, so one badly configured
#: library cannot turn a status request into a table scan dump.
DEFAULT_LIST_LIMIT = 200

MAX_MESSAGE_LENGTH = 2000

logger = TrawlarrLogging.get_logger(name='Convergence')


class ConvergenceResult(object):
    """
    The outcome of asking the library's plugins about a delivered file.

    :ivar state:        one of the STATE_* constants
    :ivar plugin_id:    the plugin that still wants the file (when known)
    :ivar plugin_name:  its display name
    :ivar message:      human-readable explanation, for the log and the UI
    :ivar issues:       the file-test issue list, verbatim
    """

    def __init__(self, state, plugin_id=None, plugin_name=None, message='', issues=None):
        self.state = state
        self.plugin_id = plugin_id
        self.plugin_name = plugin_name
        self.message = message
        self.issues = issues or []

    @property
    def converged(self):
        return self.state == STATE_CONVERGED

    @property
    def not_converged(self):
        return self.state == STATE_NOT_CONVERGED

    @property
    def evaluated(self):
        return self.state != STATE_NOT_EVALUATED


def _truncate(message):
    if message is None:
        return ''
    text = str(message).strip()
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    return text[:MAX_MESSAGE_LENGTH - 3] + '...'


def evaluate_path(abspath, library_id, shared_info=None, file_test=None):
    """
    Ask the library's file-test plugins whether `abspath` still qualifies.

    Never raises. A broken file test must not be able to take down
    post-processing of a task that has already succeeded - but it must not be
    able to report convergence it did not establish either, so any problem
    yields STATE_NOT_EVALUATED rather than STATE_CONVERGED.

    :param abspath:
    :param library_id:
    :param shared_info: seed for the plugins' `shared_info` dict; see
                        FileTest.run_file_test_plugins()
    :param file_test:   an existing FileTest, for testing/injection
    :return: ConvergenceResult
    """
    if not abspath:
        return ConvergenceResult(STATE_NOT_EVALUATED, message='No output path to evaluate')

    try:
        if file_test is None:
            from trawlarr.libs.filetest import FileTest
            file_test = FileTest(library_id)
    except Exception:
        logger.exception("Unable to build a file test for library %s", library_id)
        return ConvergenceResult(STATE_NOT_EVALUATED, message='The library file test could not be constructed')

    if not getattr(file_test, 'plugin_modules', None):
        # No file-test plugins means the library has expressed no criteria.
        # There is nothing to converge against, and claiming convergence here
        # would be inventing a result.
        return ConvergenceResult(
            STATE_NOT_EVALUATED,
            message='Library {} has no file test plugins enabled, so it has no criteria to converge against'.format(
                library_id))

    try:
        should_queue, issues, _priority, decision_plugin = file_test.run_file_test_plugins(
            abspath, shared_info=shared_info)
    except Exception:
        logger.exception("Unable to re-run the library file test against '%s'", abspath)
        return ConvergenceResult(STATE_NOT_EVALUATED, message='The library file test raised while re-checking the output')

    if should_queue is not True:
        return ConvergenceResult(STATE_CONVERGED, issues=issues)

    decision_plugin = decision_plugin or {}
    plugin_id = decision_plugin.get('plugin_id')
    plugin_name = decision_plugin.get('plugin_name')
    message = (
        "The task completed, but '{}' still matches this library's criteria for processing. "
        "Plugin '{}' would queue it again.").format(abspath, plugin_name or plugin_id or 'unknown')
    return ConvergenceResult(STATE_NOT_CONVERGED, plugin_id=plugin_id, plugin_name=plugin_name,
                             message=message, issues=issues)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def record(abspath, library_id=0, task_id=None, result=None):
    """
    Persist (or advance) the non-converged state for a path.

    An existing row for the same path has its `occurrences` incremented rather
    than being replaced, so the "this has now happened N times" signal survives
    restarts and history pruning. Re-recording also clears a previous
    dismissal: the operator acknowledged the last occurrence, not this one.

    Never raises; a database problem here must not affect the delivered file.

    :param abspath:
    :param library_id:
    :param task_id:
    :param result:  ConvergenceResult
    :return: the resulting occurrence count, or 0 when nothing was stored
    """
    if not abspath or result is None:
        return 0
    now = datetime.datetime.now()
    try:
        row = FileConvergenceState.get_or_none(FileConvergenceState.abspath == abspath)
        if row is None:
            FileConvergenceState.create(
                abspath=abspath,
                library_id=library_id or 0,
                occurrences=1,
                plugin_id=result.plugin_id,
                plugin_name=result.plugin_name,
                message=_truncate(result.message),
                task_id=task_id,
                first_seen=now,
                last_seen=now,
                dismissed=False,
            )
            return 1
        row.library_id = library_id or 0
        row.occurrences = int(row.occurrences or 0) + 1
        row.plugin_id = result.plugin_id
        row.plugin_name = result.plugin_name
        row.message = _truncate(result.message)
        row.task_id = task_id
        row.last_seen = now
        row.dismissed = False
        row.save()
        return row.occurrences
    except Exception:
        logger.exception("Unable to record the non-converged state for '%s'", abspath)
        return 0


def clear(abspath):
    """
    Drop the non-converged record for a path.

    Called when a task leaves the file converged. The problem is over; keeping
    the row would mean the health view only ever grows, which is the same
    mistake outstanding_failure_summary() exists to avoid.

    :param abspath:
    :return: number of rows removed
    """
    return clear_paths([abspath])


def clear_paths(abspaths):
    """
    clear() for several paths at once.

    :param abspaths:
    :return: number of rows removed
    """
    paths = [p for p in (abspaths or []) if p]
    if not paths:
        return 0
    try:
        return FileConvergenceState.delete().where(FileConvergenceState.abspath.in_(paths)).execute()
    except Exception:
        logger.exception("Unable to clear the non-converged state for %s path(s)", len(paths))
        return 0


def _row_to_dict(row):
    def _stamp(value):
        if value is None:
            return None
        if hasattr(value, 'timestamp'):
            try:
                return value.timestamp()
            except (ValueError, OSError, OverflowError):
                return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    occurrences = int(row.occurrences or 0)
    return {
        'id':          row.id,
        'abspath':     row.abspath,
        'library_id':  row.library_id,
        'occurrences': occurrences,
        'plugin_id':   row.plugin_id,
        'plugin_name': row.plugin_name,
        'message':     row.message,
        'task_id':     row.task_id,
        'first_seen':  _stamp(row.first_seen),
        'last_seen':   _stamp(row.last_seen),
        'dismissed':   bool(row.dismissed),
        'repeated':    occurrences >= REPEAT_LIMIT,
    }


def list_outstanding(limit=DEFAULT_LIST_LIMIT, include_dismissed=False):
    """
    The files currently reported as non-converged, worst first.

    "Worst" is the occurrence count: a file that has now completed three times
    without converging is a more useful thing to look at than one that has
    done it once.

    :param limit:
    :param include_dismissed:
    :return: list of dicts
    """
    try:
        query = FileConvergenceState.select()
        if not include_dismissed:
            query = query.where((FileConvergenceState.dismissed == False) |  # noqa: E712
                                FileConvergenceState.dismissed.is_null(True))
        query = query.order_by(FileConvergenceState.occurrences.desc(),
                               FileConvergenceState.last_seen.desc())
        if limit:
            query = query.limit(int(limit))
        return [_row_to_dict(row) for row in query]
    except Exception:
        logger.exception("Unable to list non-converged files")
        return []


def outstanding_summary():
    """
    The health view for non-convergence.

    Mirrors taskfailure.outstanding_failure_summary() in shape and in
    philosophy: it answers a question about NOW. A file that converged on a
    later task has had its row removed and is not counted, and a dismissed
    file is acknowledged rather than deleted.

    :return: dict
    """
    summary = {
        'total':        0,
        'repeated':     0,
        'plugins':      {},
        'oldest':       None,
        'newest':       None,
        'repeat_limit': REPEAT_LIMIT,
    }
    for item in list_outstanding(limit=None):
        summary['total'] += 1
        if item['repeated']:
            summary['repeated'] += 1
        plugin_id = item.get('plugin_id') or 'unknown'
        summary['plugins'][plugin_id] = summary['plugins'].get(plugin_id, 0) + 1
        stamp = item.get('last_seen')
        if stamp is None:
            continue
        if summary['newest'] is None or stamp > summary['newest']:
            summary['newest'] = stamp
        if summary['oldest'] is None or stamp < summary['oldest']:
            summary['oldest'] = stamp
    return summary


def set_dismissed(id_list, dismissed=True):
    """
    Acknowledge non-converged files without deleting the record.

    As with #25's dismissal, this means "I have seen this", not "this did not
    happen": the occurrence count keeps advancing underneath, so a file that
    is dismissed and then completes again without converging comes back with a
    higher count and an un-dismissed row.

    :param id_list:
    :param dismissed:
    :return: number of rows updated
    """
    if not id_list:
        return 0
    try:
        return (FileConvergenceState
                .update({FileConvergenceState.dismissed: bool(dismissed)})
                .where(FileConvergenceState.id.in_(list(id_list)))
                .execute())
    except Exception:
        logger.exception("Unable to update the dismissed state for convergence records %s", id_list)
        return 0
