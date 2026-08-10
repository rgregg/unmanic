#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    trawlarr.libs.reprocess.py

    Deliberately re-running an existing library under changed rules (issue #41).

    THE PROBLEM
    -----------
    Everything the pipeline does is one-shot by design. Issue #33 made
    "already completed successfully" a native, non-overridable gate, precisely
    so that a file cannot be processed a second time by accident. That is the
    right default, and it is what makes the reprocess loop impossible.

    It also means that when the *rules change* - a plugin is reconfigured, a
    new one is added, a bug in the old flow is fixed - there is no supported
    way to re-run the files that were processed under the old rules. The field
    log records what that cost after the loudness fix: a read-only classifier
    and a separate repair script living entirely outside the application, with
    their own bucketing, atomic replace, idempotency markers and verification,
    none of which the application knew about.

    So this module is the sanctioned exception to issue #33. It does not add a
    way to process a file twice - queueing a file by hand already does that. It
    adds a way to say "these files should be looked at again", scoped, and have
    the ordinary pipeline pick them up.

    WHAT IT ACTUALLY DOES
    ---------------------
    It removes recorded state. That is all.

      * `donestate.forget_paths()` drops the completion record, which is the
        seam issue #33 left for exactly this.
      * with `include_failed`, the failed `completed_tasks` rows for the path
        are deleted, because those blacklist the file by a completely
        different route (see below).

    It does NOT queue anything. Nothing here creates a pending task, and
    nothing here calls the scanner. The next library scan - or an inotify
    event, or a manual rescan - finds a file it has no record of and puts it
    through the same file test as any other file. That matters: a reprocessed
    file is subject to every guard, filter and plugin vote that a newly
    discovered file is subject to, including the ones the operator just
    changed. If the new rules do not want the file, nothing happens to it.

    THE TWO ROUTES A FILE CAN BE BLOCKED BY
    ---------------------------------------
    `FileTest.should_file_be_added_to_task_list()` has two separate terminal
    states and they are stored in different places:

      1. completed successfully  -> a `file_completion_state` row (issue #33)
      2. failed in history       -> any `completed_tasks` row with
                                    `task_success = False`

    The second one is upstream behaviour and it is permanent and unconditional:
    `FileTest.file_failed_in_history()` reads every failed history row and
    blacklists those paths, and a *later success does not clear it*. A file
    that failed once in 2024 and has processed cleanly a dozen times since is
    still on that list.

    That is why clearing the done-state alone is not enough for such a file,
    and why this module refuses to pretend otherwise. A path with failed
    history is reported in the preview as blocked, with the reason, and is
    only acted on when the caller explicitly passes `include_failed`. Acting
    on it means deleting those history rows, which:

      * removes the diagnostics for those failures (category, message, logs),
      * and resets the consecutive-failure count that issue #25's retry guard
        uses, because that count is computed from the same rows.

    Both consequences are real and neither is reversible, so they are opt-in
    and they are stated in the preview rather than in a release note.

    WHAT STOPS THIS BEING A LOOP
    ----------------------------
    Silent re-queue is the pathology that made the incidents behind issues #33
    and #34 expensive. Adding an "invalidate the done-state" operation is
    adding, on purpose, the primitive those issues removed - so the shape of
    the operation matters more than the operation:

      * It is never automatic. Nothing in this process calls it: no scheduler
        entry, no plugin hook, no post-processor path. The only caller is the
        API handler, which means a human or something a human wrote and
        pointed at this installation.
      * It must be scoped, and scoped *to something*. `library_id` or
        `path_glob` is required, and a `path_glob` made only of wildcards is
        not a scope: `path_glob='*'` matches every absolute path there is, so
        on its own it is the unscoped request wearing a hat, and it is refused
        by the same check for the same reason. A glob with any literal
        character in it ('*.mkv', '/library/*') selects a real subset and is
        allowed however wide, because "wide" is a legitimate thing to ask for
        and the preview is what makes it legible. A selection larger than
        MAX_SELECTION is refused outright with a message asking for a
        narrower filter, rather than truncated - truncating would do most of
        an unintended thing quietly.
      * It must be previewed. `apply_selection()` requires `confirm_count`
        *and* `confirm_digest`, and refuses unless both match what the same
        filter selects right now. The count alone is not enough: two files
        completing and one file vanishing between the preview and the apply
        leaves the count identical and the set different, and both events are
        ordinary in a running pipeline. The digest is over the selected paths,
        so a caller whose selection has changed at all fails closed instead of
        acting on files it never saw.
      * It is remembered. Every invalidated path gets a `file_reprocess_state`
        row carrying a count and a timestamp, and a path invalidated within
        `cooldown_hours` is skipped unless `force` is passed. A script that
        re-invalidates the same library every night does nothing at all on
        nights 2..n, and the count on the row says how many times it tried.
      * It is loud. Every applied request logs a warning naming the filter and
        the number of files, whether or not anything was found.

    None of that makes deliberate misuse impossible. `force` exists, and a
    determined caller can pass it every time. The goal is that a loop cannot
    be created by accident, that it cannot be created by *inaction* (the
    default settings do nothing), and that when one is created there is a
    counter with the file's name on it.

    WHY CONVERGENCE RECORDS ARE NOT CLEARED BY DEFAULT
    --------------------------------------------------
    `convergence.clear_paths()` exists and this module can call it, but
    `clear_convergence` defaults to False, which is the opposite of the
    obvious choice. The reason is in issue #34's own design notes: the
    occurrence count on a convergence record measures "we ran this file again
    and it *still* did not end up matching the library's criteria", and with
    issue #33 in place the only thing that can advance that count is a
    deliberate reprocess. In other words the count is only meaningful across
    reprocess requests, and clearing it on every request would guarantee it
    never reaches REPEAT_LIMIT and never says the one thing it was built to
    say.

    Clearing is still offered, because after a genuine rule change the old
    non-convergence records describe criteria that no longer exist. That is a
    judgement only the operator can make, so it is a flag they set and not a
    default they discover.

    WHAT IS DELIBERATELY SKIPPED
    ----------------------------
      * A file with a task anywhere in the pipeline - 'creating', 'pending',
        'in_progress' or 'processed'. It is already going to be processed,
        under the current rules, and invalidating its done-state would achieve
        nothing except to confuse the record it writes when it finishes.

        'processed' matters as much as 'pending' and is the less obvious of
        the two: a task in that state has finished encoding and is waiting for
        the post-processor, which calls `donestate.record_completion()`. Treat
        such a file as reprocessable and the sequence is - delete the record,
        report the file as reprocessed, watch the post-processor write the
        record straight back, and then have the 24h cooldown refuse the
        correct retry. 'creating' matters for the mirror-image reason: a
        remote task sits in it until a remote trigger moves it on, which can
        be a long time, and it is a real queued task throughout.

        'complete' is NOT in that set. A remote task ends there and its row
        stays, so counting it as active would make every remotely-processed
        file permanently un-reprocessable.
      * A file that is not on disk. Whatever the record says, there is
        nothing to reprocess.
      * A file invalidated within the cooldown window (see above).
      * With `match_file_test`, a file the library's file-test plugins do not
        currently want. This is the "file-test predicate" filter, and it is
        the one that answers the question the field log actually asked -
        *which of these already-transcoded files would the new rules still
        pick up?* It is also the expensive one: it runs the library's plugins,
        which probe, against every candidate, on preview and again on apply.
"""

import datetime
import fnmatch
import hashlib
import os

from trawlarr.libs import convergence, donestate
from trawlarr.libs.logs import TrawlarrLogging
from trawlarr.libs.unmodels import CompletedTasks, FileCompletionState, FileReprocessState, Libraries, Tasks

#: How many entries the preview will describe individually. The counts are
#: always complete; only the list is capped, so one request cannot turn into a
#: dump of every file in a library.
DEFAULT_LIST_LIMIT = 200

#: Refuse a selection bigger than this. Not a truncation - a refusal, with a
#: message asking for a narrower filter. See the module docstring.
MAX_SELECTION = 5000

#: A path invalidated more recently than this is skipped unless forced.
DEFAULT_COOLDOWN_HOURS = 24

#: SQLite's default variable limit is 999; stay well under it.
_QUERY_BATCH = 400

#: Task statuses that mean "this file is already on its way through". Every
#: status a task row can hold except 'complete', which is where a finished
#: remote task's row is left for good. See the module docstring.
_ACTIVE_TASK_STATUSES = ('creating', 'pending', 'in_progress', 'processed')

#: Characters that restrict nothing when they are all a glob is made of.
_GLOB_WILDCARDS = '*?/' + os.sep

#: Synthetic absolute paths sharing no component, extension or letter pattern.
#: A glob matching ALL of them restricts nothing, whatever characters it is
#: spelled with. Used by _glob_restricts_nothing(); see its docstring for why
#: the question is asked this way rather than by inspecting the glob text.
_UNRELATED_PROBE_PATHS = (
    '/zzq/unlikely-dir/episode.mkv',
    '/other-root/9/thing.bin',
    '/m/n/o/p/q/r/deep.avi',
)


# Why a candidate was not selected. Reported per file by the preview.
SKIP_MISSING = 'file_missing'
SKIP_QUEUED = 'already_queued'
SKIP_FAILED_HISTORY = 'blocked_by_failed_history'
SKIP_COOLDOWN = 'recently_reprocessed'
SKIP_NO_MATCH = 'file_test_no_match'
SKIP_FILE_TEST_ERROR = 'file_test_error'

logger = TrawlarrLogging.get_logger(name='Reprocess')


class ReprocessRefused(Exception):
    """
    The request was understood and deliberately not carried out.

    Distinct from an error: nothing went wrong, and nothing was changed. The
    message is written to be shown to whoever asked.
    """
    pass


def _now(now=None):
    return now if now is not None else datetime.datetime.now()


def _as_datetime(value):
    """
    Peewee hands back a datetime for a DateTimeField, but a database written
    by an older build - or a test using a raw string - can yield a string.
    """
    if isinstance(value, datetime.datetime):
        return value
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _stamp(value):
    """
    Render a timestamp the way the rest of the v2 API renders them: an epoch
    float, or None. Matches convergence._row_to_dict().
    """
    value = _as_datetime(value)
    if value is None:
        return None
    try:
        return value.timestamp()
    except (ValueError, OSError, OverflowError):
        return None


def describe_filter(library_id=None, path_glob=None, match_file_test=False, include_failed=False,
                    clear_convergence=False, force=False, cooldown_hours=DEFAULT_COOLDOWN_HOURS):
    """
    Render a filter as one line, for the log and for the audit row.

    Anything that changes what the request does has to appear here, because
    this string is the whole of what the warning log and the audit row say
    about why a file was invalidated. `cooldown_hours=0` is the case that
    makes the point: it switches the cooldown off exactly as `force` does, so
    leaving it out would let the guard be bypassed without either the log or
    the audit trail recording that it had been.

    :return: str
    """
    parts = []
    if library_id:
        parts.append('library_id={}'.format(library_id))
    if path_glob:
        parts.append("path_glob='{}'".format(path_glob))
    if match_file_test:
        parts.append('match_file_test=True')
    if include_failed:
        parts.append('include_failed=True')
    if clear_convergence:
        parts.append('clear_convergence=True')
    if force:
        parts.append('force=True')
    if not cooldown_hours:
        parts.append('cooldown_hours=0 (no cooldown)')
    elif cooldown_hours != DEFAULT_COOLDOWN_HOURS:
        parts.append('cooldown_hours={}'.format(cooldown_hours))
    return ', '.join(parts) if parts else '<no filter>'


def _glob_restricts_nothing(path_glob):
    """
    Does this glob match everything, whatever it is made of?

    `'*'`, `'/*'`, `'*/*'` all match every absolute path on the machine, so a
    request scoped only by one of them is the "everything this installation
    has ever processed" request under another name - which is the one request
    this module will not carry out in a single call.

    Asked SEMANTICALLY, by matching the glob against synthetic paths that share
    nothing with each other, rather than by inspecting its characters. An
    earlier version tested that every character was a wildcard or a separator,
    and `'[/]*'` walked straight through it: three literal characters, and it
    still matches every absolute path there is. A character class, a `?`, a
    `[!x]` negation and anything else fnmatch grows later are all covered by
    asking the question the check actually cares about.

    A glob that names a real subset - `'*.mkv'`, `'/library/*'`, `'[ab]*'` -
    fails to match at least one of the probes and is allowed, however wide it
    is. The bound on wide-but-real selections is MAX_SELECTION and the
    preview, not this check.
    """
    if not path_glob:
        return True
    return all(fnmatch.fnmatch(probe, path_glob) for probe in _UNRELATED_PROBE_PATHS)


def selection_digest(paths):
    """
    A short, stable fingerprint of an exact set of selected paths.

    This is what `apply_selection()` confirms, and the reason it confirms it
    rather than a count is in the module docstring: a count cannot tell "the
    12 files you previewed" apart from "12 files, one of which you have never
    seen". Not a security boundary - a caller who wants to act blind can
    preview and immediately apply - so a truncated digest is plenty.

    :return: str
    """
    joined = '\0'.join(paths)
    return hashlib.sha256(joined.encode('utf-8', 'surrogatepass')).hexdigest()[:16]


def _library_path(library_id):
    """
    The configured path of a library, or None.

    Used only to scope failed-history rows, which - unlike completion records
    - carry no library id of their own.
    """
    if not library_id:
        return None
    try:
        row = Libraries.get_or_none(Libraries.id == library_id)
    except Exception:
        logger.exception("Unable to read library %s", library_id)
        return None
    if row is None:
        return None
    return row.path


def _matches(abspath, path_glob, library_path):
    """
    Does this path pass the non-database parts of the filter?

    `fnmatch` semantics: `*` crosses directory separators, so
    '/library/*.mkv' matches nested files too. That is the behaviour people
    expect of a "path glob" in a filter box and it is documented as such.
    """
    if library_path and not (abspath == library_path or abspath.startswith(os.path.join(library_path, ''))):
        return False
    if path_glob and not fnmatch.fnmatchcase(abspath, path_glob):
        return False
    return True


def _completed_records(library_id, path_glob):
    """
    {abspath: {...}} for every recorded successful completion in scope.
    """
    records = {}
    try:
        query = FileCompletionState.select()
        if library_id:
            query = query.where(FileCompletionState.library_id == library_id)
        for row in query:
            if not _matches(row.abspath, path_glob, None):
                continue
            records[row.abspath] = {
                'library_id':   row.library_id,
                'task_id':      row.task_id,
                'completed_at': row.completed_at,
            }
    except Exception:
        logger.exception("Unable to read completion records for reprocessing")
    return records


def _failed_records(library_id, path_glob):
    """
    {abspath: [completed_tasks row ids]} for every failed history entry in
    scope.

    Scoped by the library's *path* rather than by a library id, because the
    history table has never carried one. A file that has since moved out of
    the library is therefore not reachable by a library-scoped request; it is
    reachable by a path glob.
    """
    library_path = _library_path(library_id) if library_id else None
    if library_id and not library_path:
        return {}
    records = {}
    try:
        query = (CompletedTasks
                 .select(CompletedTasks.id, CompletedTasks.abspath)
                 .where(CompletedTasks.task_success == False))  # noqa: E712
        for row in query:
            if not row.abspath or not _matches(row.abspath, path_glob, library_path):
                continue
            records.setdefault(row.abspath, []).append(row.id)
    except Exception:
        logger.exception("Unable to read failed history for reprocessing")
    return records


def _active_task_paths(paths):
    """
    Which of these paths already have a pending or in-progress task?
    """
    active = {}
    paths = list(paths)
    for index in range(0, len(paths), _QUERY_BATCH):
        batch = paths[index:index + _QUERY_BATCH]
        try:
            query = (Tasks
                     .select(Tasks.abspath, Tasks.status)
                     .where((Tasks.abspath.in_(batch)) & (Tasks.status.in_(list(_ACTIVE_TASK_STATUSES)))))
            for row in query:
                active[row.abspath] = row.status
        except Exception:
            logger.exception("Unable to read the task queue while building a reprocess selection")
    return active


def _reprocess_rows(paths):
    """
    {abspath: FileReprocessState row} for the paths that have one.
    """
    rows = {}
    paths = list(paths)
    for index in range(0, len(paths), _QUERY_BATCH):
        batch = paths[index:index + _QUERY_BATCH]
        try:
            for row in FileReprocessState.select().where(FileReprocessState.abspath.in_(batch)):
                rows[row.abspath] = row
        except Exception:
            logger.exception("Unable to read the reprocess audit trail")
    return rows


class _FileTestPredicate(object):
    """
    "Would the library's file-test plugins want this file today?"

    Wraps one FileTest per library, built with `ignore_completed_files=True`
    and asked only tiers 1-3 (`run_file_test_plugins`) - the same narrowing
    issue #34's convergence check makes, and for the same reason: tier 0 would
    answer "already completed" for every candidate here by construction, so
    asking it would make the predicate always False.
    """

    def __init__(self, factory=None):
        self._factory = factory or self._default_factory
        self._cache = {}

    @staticmethod
    def _default_factory(library_id):
        from trawlarr.libs.filetest import FileTest
        return FileTest(library_id, ignore_completed_files=True)

    def _file_test(self, library_id):
        if library_id not in self._cache:
            self._cache[library_id] = self._factory(library_id)
        return self._cache[library_id]

    def evaluate(self, abspath, library_id):
        """
        :return: (matches: bool, error: str or None)
        """
        if not library_id:
            return False, ("no library is recorded against this file, so the library's file-test plugins "
                           "cannot be run against it")
        try:
            file_test = self._file_test(library_id)
        except Exception as e:
            return False, "the file test for library {} could not be built: {}".format(library_id, e)
        try:
            result, _issues, _priority, _plugin = file_test.run_file_test_plugins(abspath)
        except Exception as e:
            # Includes FileTestPluginError. A plugin that did not vote leaves
            # the question unanswered, and "unanswered" must not mean "yes":
            # saying yes here is what re-encodes a file nobody asked for.
            return False, str(e)
        return bool(result), None


def build_selection(library_id=None, path_glob=None, match_file_test=False, include_failed=False,
                    clear_convergence=False, force=False, limit=DEFAULT_LIST_LIMIT, now=None,
                    cooldown_hours=DEFAULT_COOLDOWN_HOURS, file_test_factory=None):
    """
    Work out what a reprocess request would act on, without acting on it.

    This is the preview, and it is also exactly what `apply_selection()` acts
    on - there is one selection function, so the preview cannot drift from the
    action it describes.

    :param library_id:       restrict to one library. Required unless path_glob is given.
    :param path_glob:        fnmatch pattern over the absolute path. `*` crosses '/'.
    :param match_file_test:  additionally require that the library's file-test
                             plugins currently want the file. Expensive: it
                             runs them, and they probe.
    :param include_failed:   also act on paths blacklisted by a failed history
                             entry, deleting those history rows.
    :param clear_convergence: also clear non-convergence records. Off by
                             default; see the module docstring.
    :param force:            ignore the cooldown.
    :param limit:            cap on how many entries are listed individually.
    :param now:              injection point for the cooldown clock.
    :param cooldown_hours:   how recently invalidated is "too recently".
    :param file_test_factory: injection point for the predicate's FileTest.
    :raises ReprocessRefused: no filter was given, the only filter given
            restricts nothing, or the selection is too big.
    :return: dict
    """
    if not library_id and _glob_restricts_nothing(path_glob):
        raise ReprocessRefused(
            "A reprocess request must be scoped: give a library_id, a path_glob that names something, or "
            "both. {} Invalidating the completed state of every file this installation has ever processed "
            "is not something this API will do in one call.".format(
                "A path_glob of '{}' matches every path there is, which is the same request.".format(path_glob)
                if path_glob else "No filter was given."))

    filter_description = describe_filter(library_id=library_id, path_glob=path_glob,
                                         match_file_test=match_file_test, include_failed=include_failed,
                                         clear_convergence=clear_convergence, force=force,
                                         cooldown_hours=cooldown_hours)

    completed = _completed_records(library_id, path_glob)
    failed = _failed_records(library_id, path_glob)

    # A path known only from failed history has no completion record to
    # invalidate, but it IS blocked from reprocessing, so it belongs in the
    # selection when include_failed says so and in the skip list otherwise.
    candidate_paths = sorted(set(completed) | set(failed))

    if len(candidate_paths) > MAX_SELECTION:
        raise ReprocessRefused(
            "This filter matches {} files, which is more than the {} this operation will consider at once "
            "({}). Narrow the filter and run it again.".format(len(candidate_paths), MAX_SELECTION,
                                                               filter_description))

    active = _active_task_paths(candidate_paths)
    audit = _reprocess_rows(candidate_paths)
    cooldown = datetime.timedelta(hours=cooldown_hours) if cooldown_hours else None
    clock = _now(now)
    predicate = _FileTestPredicate(factory=file_test_factory) if match_file_test else None

    selected = []
    skipped = []
    skipped_counts = {}

    def _skip(entry, reason, detail):
        entry['skip_reason'] = reason
        entry['skip_detail'] = detail
        skipped_counts[reason] = skipped_counts.get(reason, 0) + 1
        skipped.append(entry)

    for abspath in candidate_paths:
        completion = completed.get(abspath)
        failed_ids = failed.get(abspath, [])
        audit_row = audit.get(abspath)
        last_reprocessed = _as_datetime(getattr(audit_row, 'requested_at', None))
        entry = {
            'abspath':              abspath,
            'library_id':           (completion or {}).get('library_id') or (library_id or 0),
            'completed_at':         _stamp((completion or {}).get('completed_at')),
            'task_id':              (completion or {}).get('task_id'),
            'has_completion_state': completion is not None,
            'failed_history_rows':  len(failed_ids),
            'previous_reprocesses': getattr(audit_row, 'occurrences', 0) or 0,
            'last_reprocessed_at':  _stamp(last_reprocessed),
            'skip_reason':          None,
            'skip_detail':          '',
        }

        if not os.path.exists(abspath):
            _skip(entry, SKIP_MISSING,
                  "There is no file at this path, so there is nothing to reprocess. The stale record is "
                  "left alone.")
            continue

        if abspath in active:
            _skip(entry, SKIP_QUEUED,
                  "This file already has a {} task and will be processed under the current rules without "
                  "any need to invalidate its history.".format(active[abspath]))
            continue

        if failed_ids and not include_failed:
            _skip(entry, SKIP_FAILED_HISTORY,
                  "This file is blacklisted by {} failed history entr{}, which the file test checks "
                  "independently of the completed state - clearing the completed state alone would not make "
                  "it eligible. Set include_failed to delete those history rows, which also deletes their "
                  "diagnostics and resets the retry guard's consecutive-failure count for this "
                  "file.".format(len(failed_ids), 'y' if len(failed_ids) == 1 else 'ies'))
            continue

        if cooldown and not force:
            last = last_reprocessed
            if last is not None and (clock - last) < cooldown:
                _skip(entry, SKIP_COOLDOWN,
                      "This file was already reprocessed at {} ({} time(s) in total). Reprocessing it again "
                      "within {} hours is refused unless force is set - repeatedly invalidating the same "
                      "file is what a reprocess loop looks like.".format(last, entry['previous_reprocesses'],
                                                                         cooldown_hours))
                continue

        if predicate is not None:
            matches, error = predicate.evaluate(abspath, entry['library_id'])
            if error:
                _skip(entry, SKIP_FILE_TEST_ERROR,
                      "The library's file-test plugins gave no answer for this file ({}). It is not selected: "
                      "an unanswered question must not be read as a request to re-encode.".format(error))
                continue
            if not matches:
                _skip(entry, SKIP_NO_MATCH,
                      "The library's file-test plugins do not currently want this file, so reprocessing it "
                      "would queue nothing.")
                continue

        selected.append(entry)

    return {
        'success':         True,
        'filter':          {
            'library_id':        library_id,
            'path_glob':         path_glob,
            'match_file_test':   bool(match_file_test),
            'include_failed':    bool(include_failed),
            'clear_convergence': bool(clear_convergence),
            'force':             bool(force),
            'cooldown_hours':    cooldown_hours,
            'description':       filter_description,
        },
        'counts':          {
            'candidates':          len(candidate_paths),
            'selected':            len(selected),
            'skipped':             len(skipped),
            'completion_records':  sum(1 for e in selected if e['has_completion_state']),
            'failed_history_rows': sum(e['failed_history_rows'] for e in selected),
        },
        'skipped_reasons': skipped_counts,
        # Over the whole selection, never the truncated listing: a preview
        # asked for with limit=2 must still confirm all 137 files.
        'digest':          selection_digest([e['abspath'] for e in selected]),
        'selected':        selected[:limit],
        'skipped':         skipped[:limit],
        'listing_limited': len(selected) > limit or len(skipped) > limit,
        'listing_limit':   limit,
    }


def apply_selection(confirm_count, library_id=None, path_glob=None, match_file_test=False, include_failed=False,
                    clear_convergence=False, force=False, limit=DEFAULT_LIST_LIMIT, now=None,
                    cooldown_hours=DEFAULT_COOLDOWN_HOURS, file_test_factory=None, confirm_digest=None):
    """
    Invalidate the recorded state for everything the filter selects.

    `confirm_count` must equal the number of files this filter selects at the
    moment of the call, and `confirm_digest` must equal the digest the same
    preview reported. That is the whole preview requirement: a caller that has
    not looked can supply neither, and a caller whose selection has changed
    since it looked is refused rather than acting on a set it has not seen.

    The digest is what makes that second half true. Confirming the count alone
    would accept any set of the same size, and "the same size, different
    files" is not a corner case here: one file completing and one file being
    deleted between the preview and the apply is a Tuesday in a running
    pipeline, and the operator would then invalidate a file they never saw
    listed. `confirm_digest` is therefore required; None is a mismatch, not a
    waiver.

    Never queues anything. See the module docstring.

    :raises ReprocessRefused: the request was scoped wrongly, is too large, or
            the confirmation does not match the selection.
    :return: the selection dict, with an 'applied' section added
    """
    # Selected once, with the listing cap wound right off, so that the set
    # confirm_count is checked against is the same object that is acted on.
    # Selecting twice would mean checking one set and invalidating another,
    # and with match_file_test it would also probe every candidate twice.
    selection = build_selection(library_id=library_id, path_glob=path_glob, match_file_test=match_file_test,
                                include_failed=include_failed, clear_convergence=clear_convergence, force=force,
                                limit=MAX_SELECTION, now=now, cooldown_hours=cooldown_hours,
                                file_test_factory=file_test_factory)
    entries = selection['selected']
    actual = selection['counts']['selected']
    actual_digest = selection['digest']
    try:
        confirmed = int(confirm_count)
    except (TypeError, ValueError):
        confirmed = None
    if confirmed is None or confirmed != actual:
        raise ReprocessRefused(
            "This filter now selects {} file(s), but the request confirmed {}. Nothing has been changed. "
            "Preview the selection again and re-send the request with the count and digest it "
            "reports.".format(actual, confirm_count))
    if not confirm_digest or str(confirm_digest) != actual_digest:
        # Same size, different files - or a caller that never previewed at
        # all. Both act on a set nobody has read, which is the one thing the
        # preview exists to prevent.
        raise ReprocessRefused(
            "This filter selects {} file(s) as confirmed, but they are not the same {} files: the selection "
            "digest is now '{}' and the request confirmed '{}'. Something completed, failed, was queued or "
            "was deleted since the preview. Nothing has been changed. Preview the selection again and "
            "re-send the request with the digest it reports.".format(
                actual, actual, actual_digest, confirm_digest or '<none>'))

    filter_description = selection['filter']['description']
    if actual == 0:
        logger.warning("Reprocess request selected no files (%s). Nothing was invalidated.", filter_description)
        selection['applied'] = {
            'completion_records_cleared':  0,
            'failed_history_rows_deleted': 0,
            'convergence_records_cleared': 0,
            'files':                       [],
        }
        selection['listing_limit'] = limit
        return selection

    paths = [e['abspath'] for e in entries]

    logger.warning("Reprocessing %s file(s): the completed state recorded for them is being deleted so that "
                   "the next library scan queues them again (%s)", len(paths), filter_description)

    cleared = 0
    for index in range(0, len(paths), _QUERY_BATCH):
        cleared += donestate.forget_paths(paths[index:index + _QUERY_BATCH])

    convergence_cleared = 0
    if clear_convergence:
        for index in range(0, len(paths), _QUERY_BATCH):
            convergence_cleared += convergence.clear_paths(paths[index:index + _QUERY_BATCH])

    failed_deleted = 0
    if include_failed:
        failed_map = _failed_records(library_id, path_glob)
        failed_ids = []
        for abspath in paths:
            failed_ids.extend(failed_map.get(abspath, []))
        failed_deleted = _delete_failed_history(failed_ids)

    recorded = record_reprocessed(entries, filter_description, now=now)

    logger.warning("Reprocess complete: %s completion record(s) cleared, %s failed history row(s) deleted, "
                   "%s convergence record(s) cleared, %s audit row(s) written (%s)",
                   cleared, failed_deleted, convergence_cleared, recorded, filter_description)

    selection['applied'] = {
        'completion_records_cleared':  cleared,
        'failed_history_rows_deleted': failed_deleted,
        'convergence_records_cleared': convergence_cleared,
        'files':                       paths,
    }
    selection['selected'] = entries[:limit]
    selection['listing_limited'] = len(entries) > limit or len(selection['skipped']) > limit
    selection['skipped'] = selection['skipped'][:limit]
    selection['listing_limit'] = limit
    return selection


def _delete_failed_history(row_ids):
    """
    Delete the named failed `completed_tasks` rows and their command logs.

    :return: number of rows deleted
    """
    if not row_ids:
        return 0
    from trawlarr.libs.history import History
    history = History()
    deleted = 0
    row_ids = list(dict.fromkeys(row_ids))
    for index in range(0, len(row_ids), _QUERY_BATCH):
        batch = row_ids[index:index + _QUERY_BATCH]
        try:
            history.delete_historic_task_command_logs(batch)
            if history.delete_historic_tasks_recursively(batch):
                deleted += len(batch)
        except Exception:
            logger.exception("Unable to delete %s failed history row(s) during a reprocess request", len(batch))
    return deleted


def record_reprocessed(entries, filter_description, now=None):
    """
    Write (or bump) the audit row for every path that was invalidated.

    Never raises: a failure to write the audit trail must not undo an
    invalidation that has already happened. It does mean the cooldown will not
    see this request, which is logged.

    :return: number of rows written
    """
    clock = _now(now)
    written = 0
    for entry in entries or []:
        abspath = entry.get('abspath')
        if not abspath:
            continue
        try:
            row = FileReprocessState.get_or_none(FileReprocessState.abspath == abspath)
            if row is None:
                FileReprocessState.create(
                    abspath=abspath,
                    library_id=entry.get('library_id') or 0,
                    requested_at=clock,
                    occurrences=1,
                    requested_by=filter_description,
                )
            else:
                row.library_id = entry.get('library_id') or 0
                row.requested_at = clock
                row.occurrences = (row.occurrences or 0) + 1
                row.requested_by = filter_description
                row.save()
            written += 1
        except Exception:
            logger.exception("Unable to record the reprocess audit row for '%s'. The file has still been "
                             "invalidated; the cooldown will not know about this request.", abspath)
    return written


def history_for_path(abspath):
    """
    What does the audit trail know about this path?

    :return: dict or None
    """
    if not abspath:
        return None
    try:
        row = FileReprocessState.get_or_none(FileReprocessState.abspath == abspath)
    except Exception:
        logger.exception("Unable to read the reprocess audit trail for '%s'", abspath)
        return None
    if row is None:
        return None
    return {
        'abspath':      row.abspath,
        'library_id':   row.library_id,
        'requested_at': _stamp(row.requested_at),
        'occurrences':  row.occurrences,
        'requested_by': row.requested_by,
    }
