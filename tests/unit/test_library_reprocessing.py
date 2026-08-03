#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_library_reprocessing.py

    Issue #41: the sanctioned way to re-run an already-processed library under
    changed pipeline rules.

    This feature deliberately switches off the guard that issues #33 and #34
    exist to enforce, so most of what is pinned here is the *restraint* rather
    than the capability: an unscoped request is refused, an unconfirmed request
    is refused, an oversized selection is refused, a file that is already
    queued is left alone, a file blocked by failed history is reported instead
    of quietly missed, and nothing anywhere in this path queues a task.

    The two behavioural pins that matter most:

      * after apply(), donestate.file_is_already_completed() answers False for
        the selected file - that is the whole point of the feature, and it is
        asserted against the real check the scanner uses rather than against
        a row count.
      * after apply(include_failed=True), FileTest.file_failed_in_history()
        answers False - the second, independent blacklist that clearing the
        done-state alone does not touch.
"""
import datetime
import os

import pytest
from peewee import SqliteDatabase

from trawlarr.libs import convergence, donestate, reprocess
from trawlarr.libs.unmodels import CompletedTasks, CompletedTasksCommandLogs, FileCompletionState, \
    FileConvergenceState, FileReprocessState, Libraries, Tasks

MODELS = [
    CompletedTasks,
    CompletedTasksCommandLogs,
    FileCompletionState,
    FileConvergenceState,
    FileReprocessState,
    Libraries,
    Tasks,
]


@pytest.fixture
def db():
    """Bind every table this feature touches to a throwaway database."""
    database = SqliteDatabase(':memory:')
    with database.bind_ctx(MODELS):
        database.create_tables(MODELS)
        yield database
    database.close()


@pytest.fixture
def library(tmp_path, db):
    """A library row whose path is a real directory."""
    path = tmp_path / "library"
    path.mkdir()
    Libraries.create(id=1, name='Test', path=str(path))
    return str(path)


def _file(library_path, name, contents=b'x' * 32):
    path = os.path.join(library_path, name)
    with open(path, 'wb') as f:
        f.write(contents)
    return path


def _completed(abspath, library_id=1, task_id=7):
    """Record the file as done, exactly as the post-processor does."""
    assert donestate.record_completion(abspath, library_id=library_id, task_id=task_id)
    return abspath


def _failed_history(abspath, task_success=False):
    return CompletedTasks.create(
        task_label=os.path.basename(abspath),
        abspath=abspath,
        task_success=task_success,
        processed_by_worker='worker-1',
    )


def _paths(entries):
    return [e['abspath'] for e in entries]


def _apply(**kwargs):
    """
    Preview, then apply what the preview reported.

    This is the sanctioned two-step, and it is what every caller has to do:
    apply() takes both the count and a digest of the selected paths, neither
    of which can be guessed. Tests about the confirmation itself pass the two
    values by hand instead.
    """
    preview = reprocess.build_selection(**kwargs)
    return reprocess.apply_selection(preview['counts']['selected'],
                                     confirm_digest=preview['digest'], **kwargs)


def _skip_reasons(selection):
    return {e['abspath']: e['skip_reason'] for e in selection['skipped']}


# ---------------------------------------------------------------------------
# Scoping: the request has to say what it means
# ---------------------------------------------------------------------------

class TestARequestMustBeScoped:

    def test_a_request_with_no_filter_is_refused(self, db):
        with pytest.raises(reprocess.ReprocessRefused) as excinfo:
            reprocess.build_selection()
        assert 'scoped' in str(excinfo.value)

    def test_a_library_id_alone_is_enough(self, library):
        _completed(_file(library, 'a.mkv'))
        selection = reprocess.build_selection(library_id=1)
        assert selection['counts']['selected'] == 1

    def test_a_path_glob_alone_is_enough(self, library):
        _completed(_file(library, 'a.mkv'))
        selection = reprocess.build_selection(path_glob=os.path.join(library, '*.mkv'))
        assert selection['counts']['selected'] == 1

    def test_the_glob_excludes_files_it_does_not_match(self, library):
        _completed(_file(library, 'keep.mkv'))
        _completed(_file(library, 'other.mp4'))
        selection = reprocess.build_selection(path_glob=os.path.join(library, '*.mkv'))
        assert _paths(selection['selected']) == [os.path.join(library, 'keep.mkv')]

    def test_another_librarys_files_are_not_selected(self, library, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        Libraries.create(id=2, name='Other', path=str(other))
        _completed(_file(library, 'mine.mkv'), library_id=1)
        _completed(_file(str(other), 'theirs.mkv'), library_id=2)
        selection = reprocess.build_selection(library_id=1)
        assert _paths(selection['selected']) == [os.path.join(library, 'mine.mkv')]

    @pytest.mark.parametrize('path_glob', ['*', '/*', '*/*', '**', '*?*', '/*/*/*'])
    def test_a_glob_that_restricts_nothing_is_not_a_scope(self, library, path_glob):
        """
        `path_glob='*'` matches every absolute path there is, so on its own it
        IS "everything this installation has ever processed" - the one request
        the module says it will not carry out in a single call. Before this
        check it was expressible, and one call cleared the completed state of
        two separate libraries.
        """
        _completed(_file(library, 'a.mkv'))

        with pytest.raises(reprocess.ReprocessRefused) as excinfo:
            reprocess.build_selection(path_glob=path_glob)

        assert 'scoped' in str(excinfo.value)
        assert donestate.file_is_already_completed(os.path.join(library, 'a.mkv'))[0] is True

    def test_a_wildcard_glob_cannot_cross_libraries(self, library, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        Libraries.create(id=2, name='Other', path=str(other))
        mine = _completed(_file(library, 'mine.mkv'), library_id=1)
        theirs = _completed(_file(str(other), 'theirs.mkv'), library_id=2)

        with pytest.raises(reprocess.ReprocessRefused):
            reprocess.apply_selection(2, path_glob='*', confirm_digest='whatever')

        assert donestate.file_is_already_completed(mine)[0] is True
        assert donestate.file_is_already_completed(theirs)[0] is True

    @pytest.mark.parametrize('suffix', ['*.mkv', '*/season*', '[ab]*'])
    def test_a_wide_glob_with_something_literal_in_it_is_still_allowed(self, library, suffix):
        # The bound on a wide-but-real selection is MAX_SELECTION and the
        # preview, not this check. Refusing every wide glob would make the
        # backfill this feature exists for impossible to express.
        _completed(_file(library, 'a.mkv'))
        reprocess.build_selection(path_glob=suffix)

    def test_a_bare_wildcard_is_fine_once_a_library_scopes_it(self, library):
        _completed(_file(library, 'a.mkv'))
        assert reprocess.build_selection(library_id=1, path_glob='*')['counts']['selected'] == 1

    def test_an_oversized_selection_is_refused_and_not_truncated(self, library, monkeypatch):
        monkeypatch.setattr(reprocess, 'MAX_SELECTION', 2)
        for name in ('a.mkv', 'b.mkv', 'c.mkv'):
            _completed(_file(library, name))
        with pytest.raises(reprocess.ReprocessRefused) as excinfo:
            reprocess.build_selection(library_id=1)
        assert 'Narrow the filter' in str(excinfo.value)
        # Refused means refused: the records are all still there.
        assert FileCompletionState.select().count() == 3


# ---------------------------------------------------------------------------
# What the operation actually does
# ---------------------------------------------------------------------------

class TestApplyingASelection:

    def test_the_file_stops_being_reported_as_already_completed(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        # Precondition: the scanner's own check says "done".
        assert donestate.file_is_already_completed(abspath)[0] is True

        result = _apply(library_id=1)

        assert result['applied']['completion_records_cleared'] == 1
        # The claim the whole feature rests on, asserted against the check the
        # scanner actually runs rather than against a row count.
        assert donestate.file_is_already_completed(abspath)[0] is False

    def test_nothing_is_queued(self, library):
        _completed(_file(library, 'a.mkv'))
        _apply(library_id=1)
        # Reprocessing removes records. Queueing is the scanner's job, and if
        # this ever starts creating tasks itself it has become the silent
        # re-queue that issues #33 and #34 were written to stop.
        assert Tasks.select().count() == 0

    def test_files_outside_the_filter_keep_their_completed_state(self, library):
        keep = _completed(_file(library, 'keep.mp4'))
        _completed(_file(library, 'go.mkv'))
        _apply(path_glob=os.path.join(library, '*.mkv'))
        assert donestate.file_is_already_completed(keep)[0] is True

    def test_an_audit_row_is_written_for_every_invalidated_file(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        _apply(library_id=1)
        record = reprocess.history_for_path(abspath)
        assert record is not None
        assert record['occurrences'] == 1
        assert 'library_id=1' in record['requested_by']

    def test_the_audit_count_climbs_with_every_reprocess(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        _apply(library_id=1)
        _completed(abspath)
        # Second request, well outside the cooldown window.
        later = datetime.datetime.now() + datetime.timedelta(days=30)
        _apply(library_id=1, now=later)
        # This count is the loop signal: a script re-invalidating the same
        # library for ever leaves a number here with the file's name on it.
        assert reprocess.history_for_path(abspath)['occurrences'] == 2


# ---------------------------------------------------------------------------
# Preview before acting
# ---------------------------------------------------------------------------

class TestNothingIsInvalidatedWithoutAPreview:

    def test_a_wrong_confirm_count_refuses_and_changes_nothing(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        digest = reprocess.build_selection(library_id=1)['digest']
        with pytest.raises(reprocess.ReprocessRefused) as excinfo:
            reprocess.apply_selection(5, confirm_digest=digest, library_id=1)
        assert 'Nothing has been changed' in str(excinfo.value)
        assert donestate.file_is_already_completed(abspath)[0] is True
        assert FileReprocessState.select().count() == 0

    def test_a_missing_confirm_count_refuses(self, library):
        _completed(_file(library, 'a.mkv'))
        digest = reprocess.build_selection(library_id=1)['digest']
        with pytest.raises(reprocess.ReprocessRefused):
            reprocess.apply_selection(None, confirm_digest=digest, library_id=1)

    def test_a_missing_confirm_digest_refuses(self, library):
        # The count is guessable; the digest is not. Accepting a request that
        # omits it would leave "act without previewing" one field away.
        abspath = _completed(_file(library, 'a.mkv'))
        with pytest.raises(reprocess.ReprocessRefused) as excinfo:
            reprocess.apply_selection(1, library_id=1)
        assert 'digest' in str(excinfo.value)
        assert donestate.file_is_already_completed(abspath)[0] is True

    def test_a_selection_that_grew_since_the_preview_refuses(self, library):
        _completed(_file(library, 'a.mkv'))
        preview = reprocess.build_selection(library_id=1)
        # Something else finishes a task between the preview and the apply.
        _completed(_file(library, 'b.mkv'))
        with pytest.raises(reprocess.ReprocessRefused):
            reprocess.apply_selection(preview['counts']['selected'],
                                      confirm_digest=preview['digest'], library_id=1)
        assert FileCompletionState.select().count() == 2

    def test_a_same_size_but_different_selection_refuses(self, library):
        """
        The case a count cannot see, and the one that actually happens.

        Preview {a, b}. Before the apply lands, `a` is deleted from disk and
        `c` finishes a task - two entirely routine events in a running
        pipeline. The selection is now {b, c}: same size, and one file the
        operator has never laid eyes on. Confirming the count alone accepted
        this and invalidated `c`.
        """
        first = _completed(_file(library, 'a.mkv'))
        _completed(_file(library, 'b.mkv'))
        preview = reprocess.build_selection(library_id=1)
        assert preview['counts']['selected'] == 2

        os.remove(first)
        never_previewed = _completed(_file(library, 'c.mkv'))

        with pytest.raises(reprocess.ReprocessRefused) as excinfo:
            reprocess.apply_selection(2, confirm_digest=preview['digest'], library_id=1)

        assert 'not the same' in str(excinfo.value)
        assert donestate.file_is_already_completed(never_previewed)[0] is True
        assert FileReprocessState.select().count() == 0

    def test_the_digest_covers_the_whole_selection_not_the_listed_page(self, library):
        # A preview taken with a small `limit` must still confirm every file
        # it counted, or a capped preview could not be applied at all.
        for index in range(5):
            _completed(_file(library, 'file{}.mkv'.format(index)))
        capped = reprocess.build_selection(library_id=1, limit=2)
        full = reprocess.build_selection(library_id=1)
        assert capped['digest'] == full['digest']

    def test_the_digest_is_a_function_of_the_paths_alone(self, library):
        one = _completed(_file(library, 'a.mkv'))
        two = _completed(_file(library, 'b.mkv'))
        assert reprocess.selection_digest([one, two]) == reprocess.build_selection(library_id=1)['digest']
        assert reprocess.selection_digest([one]) != reprocess.selection_digest([two])

    def test_the_preview_changes_nothing(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        reprocess.build_selection(library_id=1)
        assert donestate.file_is_already_completed(abspath)[0] is True
        assert FileReprocessState.select().count() == 0

    def test_the_count_the_preview_reports_is_the_count_apply_accepts(self, library):
        for name in ('a.mkv', 'b.mkv', 'c.mkv'):
            _completed(_file(library, name))
        selection = reprocess.build_selection(library_id=1)
        result = reprocess.apply_selection(selection['counts']['selected'],
                                           confirm_digest=selection['digest'], library_id=1)
        assert result['applied']['completion_records_cleared'] == 3

    def test_the_listing_is_capped_but_the_counts_are_not(self, library):
        for index in range(5):
            _completed(_file(library, 'file{}.mkv'.format(index)))
        selection = reprocess.build_selection(library_id=1, limit=2)
        assert selection['counts']['selected'] == 5
        assert len(selection['selected']) == 2
        assert selection['listing_limited'] is True

    def test_a_capped_listing_does_not_cap_what_is_invalidated(self, library):
        for index in range(5):
            _completed(_file(library, 'file{}.mkv'.format(index)))
        result = _apply(library_id=1, limit=2)
        # Acting on only the two files the preview happened to list would do
        # part of what was asked, quietly.
        assert result['applied']['completion_records_cleared'] == 5
        assert FileCompletionState.select().count() == 0


# ---------------------------------------------------------------------------
# Files that must be left alone
# ---------------------------------------------------------------------------

class TestFilesThatAreSkipped:

    @pytest.mark.parametrize('status', ['creating', 'pending', 'in_progress', 'processed'])
    def test_a_file_already_in_the_queue_is_skipped(self, library, status):
        abspath = _completed(_file(library, 'a.mkv'))
        Tasks.create(abspath=abspath, library_id=1, status=status, type='local')

        selection = reprocess.build_selection(library_id=1)

        assert selection['counts']['selected'] == 0
        assert _skip_reasons(selection) == {abspath: reprocess.SKIP_QUEUED}

    def test_a_task_waiting_for_the_post_processor_is_skipped(self, library):
        """
        'processed' means the encode is done and the post-processor has not
        run yet - and the post-processor is what calls record_completion().

        Selecting such a file deletes a record that is written straight back:
        the operator is told the file was reprocessed, nothing changes, and the
        cooldown then refuses the retry that would have worked.
        """
        abspath = _completed(_file(library, 'a.mkv'))
        Tasks.create(abspath=abspath, library_id=1, status='processed', type='local')

        selection = reprocess.build_selection(library_id=1)

        assert selection['counts']['selected'] == 0
        assert _skip_reasons(selection) == {abspath: reprocess.SKIP_QUEUED}
        assert 'processed' in selection['skipped'][0]['skip_detail']

    def test_a_remote_task_still_being_created_is_skipped(self, library):
        # A remote task sits in 'creating' until a remote trigger moves it on,
        # which can be a long time. It is a queued task throughout.
        abspath = _completed(_file(library, 'a.mkv'))
        Tasks.create(abspath=abspath, library_id=1, status='creating', type='remote')

        assert reprocess.build_selection(library_id=1)['counts']['selected'] == 0

    def test_a_finished_remote_tasks_row_does_not_block_reprocessing(self, library):
        # A remote task's row is left at 'complete' for good. Counting that as
        # live would make every remotely-processed file un-reprocessable.
        abspath = _completed(_file(library, 'a.mkv'))
        Tasks.create(abspath=abspath, library_id=1, status='complete', type='remote')

        assert reprocess.build_selection(library_id=1)['counts']['selected'] == 1

    def test_every_status_a_live_task_row_can_hold_is_covered(self):
        """
        The skip list is derived from the pipeline's own statuses, not from a
        guess. Read them out of Task.set_status()'s allow-list, add the
        'creating' state a task is opened in, and the only one this operation
        may ignore is 'complete'. A new status added to the pipeline fails
        here rather than quietly becoming a file this operation is willing to
        invalidate mid-flight.
        """
        import ast
        import inspect
        import textwrap

        from trawlarr.libs.task import Task

        tree = ast.parse(textwrap.dedent(inspect.getsource(Task.set_status)))
        assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
        allowed = next(node for node in assignments
                       if getattr(node.targets[0], 'id', None) == 'allowed')
        live_statuses = {element.value for element in allowed.value.elts} | {'creating'}

        assert set(reprocess._ACTIVE_TASK_STATUSES) == live_statuses - {'complete'}

    def test_a_file_that_is_no_longer_on_disk_is_skipped(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        os.remove(abspath)
        selection = reprocess.build_selection(library_id=1)
        assert selection['counts']['selected'] == 0
        assert _skip_reasons(selection) == {abspath: reprocess.SKIP_MISSING}

    def test_a_recently_reprocessed_file_is_skipped(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        _apply(library_id=1)
        _completed(abspath)

        selection = reprocess.build_selection(library_id=1)

        assert selection['counts']['selected'] == 0
        assert _skip_reasons(selection) == {abspath: reprocess.SKIP_COOLDOWN}

    def test_force_overrides_the_cooldown(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        _apply(library_id=1)
        _completed(abspath)
        assert reprocess.build_selection(library_id=1, force=True)['counts']['selected'] == 1

    def test_the_cooldown_expires(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        _apply(library_id=1)
        _completed(abspath)
        later = datetime.datetime.now() + datetime.timedelta(hours=25)
        assert reprocess.build_selection(library_id=1, now=later)['counts']['selected'] == 1


# ---------------------------------------------------------------------------
# What the record says was done
# ---------------------------------------------------------------------------

class TestTheAuditTrailNamesWhatWasBypassed:
    """
    The filter description is the whole of what the warning log and the audit
    row say about why a file was invalidated. Anything that changes what the
    request does has to appear in it, or the record is not a record.
    """

    def test_the_flags_that_widen_the_request_are_named(self):
        described = reprocess.describe_filter(library_id=1, path_glob='/tv/*.mkv', match_file_test=True,
                                              include_failed=True, clear_convergence=True, force=True)
        for expected in ("library_id=1", "path_glob='/tv/*.mkv'", 'match_file_test=True',
                         'include_failed=True', 'clear_convergence=True', 'force=True'):
            assert expected in described

    def test_the_default_cooldown_is_not_noise_in_the_line(self):
        assert reprocess.describe_filter(library_id=1) == 'library_id=1'

    def test_switching_the_cooldown_off_is_named(self):
        # cooldown_hours=0 disables the cooldown exactly as force does. Left
        # unrendered, the guard could be bypassed without the log or the audit
        # row recording that it had been.
        assert 'cooldown_hours=0 (no cooldown)' in reprocess.describe_filter(library_id=1, cooldown_hours=0)

    def test_a_shortened_cooldown_is_named(self):
        assert 'cooldown_hours=1' in reprocess.describe_filter(library_id=1, cooldown_hours=1)

    def test_a_zero_cooldown_reaches_the_audit_row(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        _apply(library_id=1)
        _completed(abspath)

        # Without the cooldown this file is not eligible at all; with it off
        # it is - so the row it writes has to say the guard was switched off.
        _apply(library_id=1, cooldown_hours=0)

        record = reprocess.history_for_path(abspath)
        assert record['occurrences'] == 2
        assert 'cooldown_hours=0 (no cooldown)' in record['requested_by']


# ---------------------------------------------------------------------------
# The other blacklist
# ---------------------------------------------------------------------------

class TestFilesBlockedByFailedHistory:
    """
    FileTest blacklists a path that appears in failed history, permanently and
    independently of the done-state. Clearing the completed record alone would
    leave such a file exactly as un-processable as before, and reporting it as
    reprocessed would be a lie.
    """

    def test_a_failed_file_is_reported_as_blocked_not_selected(self, library):
        abspath = _file(library, 'a.mkv')
        _completed(abspath)
        _failed_history(abspath)

        selection = reprocess.build_selection(library_id=1)

        assert selection['counts']['selected'] == 0
        assert _skip_reasons(selection) == {abspath: reprocess.SKIP_FAILED_HISTORY}
        detail = selection['skipped'][0]['skip_detail']
        assert 'include_failed' in detail

    def test_include_failed_selects_it(self, library):
        abspath = _file(library, 'a.mkv')
        _completed(abspath)
        _failed_history(abspath)
        selection = reprocess.build_selection(library_id=1, include_failed=True)
        assert _paths(selection['selected']) == [abspath]
        assert selection['counts']['failed_history_rows'] == 1

    def test_include_failed_lifts_the_file_test_blacklist(self, library, monkeypatch):
        from trawlarr.libs.filetest import FileTest

        abspath = _file(library, 'a.mkv')
        _completed(abspath)
        _failed_history(abspath)

        def _blacklisted():
            file_test = FileTest.__new__(FileTest)
            file_test.failed_paths = []
            return file_test.file_failed_in_history(abspath)

        assert _blacklisted() is True

        _apply(library_id=1, include_failed=True)

        # The second blacklist is what actually kept this file out of the
        # queue. If only the done-state had been cleared, the operator would
        # have been told the file was reprocessed and nothing would happen.
        assert _blacklisted() is False

    def test_a_file_known_only_from_failed_history_can_be_selected(self, library):
        abspath = _file(library, 'never-succeeded.mkv')
        _failed_history(abspath)
        selection = reprocess.build_selection(library_id=1, include_failed=True)
        assert _paths(selection['selected']) == [abspath]
        assert selection['selected'][0]['has_completion_state'] is False

    def test_successful_history_rows_are_left_alone(self, library):
        abspath = _file(library, 'a.mkv')
        _completed(abspath)
        good = _failed_history(abspath, task_success=True)
        _failed_history(abspath)
        _apply(library_id=1, include_failed=True)
        assert CompletedTasks.select().where(CompletedTasks.id == good.id).count() == 1
        assert CompletedTasks.select().where(CompletedTasks.task_success == False).count() == 0  # noqa: E712

    def test_failed_history_is_left_alone_without_include_failed(self, library):
        abspath = _file(library, 'a.mkv')
        other = _completed(_file(library, 'b.mkv'))
        _completed(abspath)
        _failed_history(abspath)

        result = _apply(library_id=1)

        assert result['applied']['files'] == [other]
        assert result['applied']['failed_history_rows_deleted'] == 0
        assert CompletedTasks.select().count() == 1


# ---------------------------------------------------------------------------
# Convergence records
# ---------------------------------------------------------------------------

class TestConvergenceRecords:

    @staticmethod
    def _non_converged(abspath):
        convergence.record(abspath, library_id=1, task_id=1,
                           result=convergence.ConvergenceResult(convergence.STATE_NOT_CONVERGED,
                                                                plugin_id='video_transcoder',
                                                                message='still matches'))

    def test_they_are_kept_by_default(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        self._non_converged(abspath)

        result = _apply(library_id=1)

        # Issue #34's occurrence count only advances across deliberate
        # reprocesses. Clearing it here would guarantee it never reaches its
        # repeat limit and never reports a file that can never converge.
        assert result['applied']['convergence_records_cleared'] == 0
        assert FileConvergenceState.select().where(FileConvergenceState.abspath == abspath).count() == 1

    def test_they_are_cleared_when_asked(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        self._non_converged(abspath)

        result = _apply(library_id=1, clear_convergence=True)

        assert result['applied']['convergence_records_cleared'] == 1
        assert FileConvergenceState.select().where(FileConvergenceState.abspath == abspath).count() == 0


# ---------------------------------------------------------------------------
# The file-test predicate
# ---------------------------------------------------------------------------

class _FakeFileTest(object):
    def __init__(self, verdicts, raises=None):
        self.verdicts = verdicts
        self.raises = raises or {}
        self.seen = []

    def run_file_test_plugins(self, path, file_issues=None, shared_info=None):
        self.seen.append(path)
        if path in self.raises:
            raise self.raises[path]
        return self.verdicts.get(path), [], 0, None


class TestTheFileTestPredicate:

    def test_only_files_the_plugins_still_want_are_selected(self, library):
        wanted = _completed(_file(library, 'wanted.mkv'))
        unwanted = _completed(_file(library, 'unwanted.mkv'))
        fake = _FakeFileTest({wanted: True, unwanted: False})

        selection = reprocess.build_selection(library_id=1, match_file_test=True,
                                              file_test_factory=lambda library_id: fake)

        assert _paths(selection['selected']) == [wanted]
        assert _skip_reasons(selection) == {unwanted: reprocess.SKIP_NO_MATCH}

    def test_a_plugin_that_raises_does_not_select_the_file(self, library):
        from trawlarr.libs.filetest import FileTestPluginError

        abspath = _completed(_file(library, 'a.mkv'))
        fake = _FakeFileTest({}, raises={abspath: FileTestPluginError('guard_plugin', detail='boom')})

        selection = reprocess.build_selection(library_id=1, match_file_test=True,
                                              file_test_factory=lambda library_id: fake)

        # An unanswered question must never be read as "yes, re-encode it".
        assert selection['counts']['selected'] == 0
        assert _skip_reasons(selection) == {abspath: reprocess.SKIP_FILE_TEST_ERROR}

    def test_the_predicate_is_not_run_unless_asked_for(self, library):
        _completed(_file(library, 'a.mkv'))
        fake = _FakeFileTest({})
        reprocess.build_selection(library_id=1, file_test_factory=lambda library_id: fake)
        # It probes every candidate. Running it by default would make an
        # ordinary preview of a large library an hours-long operation.
        assert fake.seen == []

    def test_one_file_test_is_built_per_library(self, library):
        for name in ('a.mkv', 'b.mkv', 'c.mkv'):
            _completed(_file(library, name))
        built = []

        def factory(library_id):
            built.append(library_id)
            return _FakeFileTest({})

        reprocess.build_selection(library_id=1, match_file_test=True, file_test_factory=factory)
        assert built == [1]

    def test_apply_does_not_probe_the_library_twice(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        fake = _FakeFileTest({abspath: True})
        preview = reprocess.build_selection(library_id=1, match_file_test=True,
                                            file_test_factory=lambda library_id: fake)
        fake.seen = []
        reprocess.apply_selection(preview['counts']['selected'], confirm_digest=preview['digest'],
                                  library_id=1, match_file_test=True,
                                  file_test_factory=lambda library_id: fake)
        # Selecting once and acting on that same selection is also what makes
        # confirm_count meaningful: the set that was counted is the set that
        # was invalidated.
        assert fake.seen == [abspath]


# ---------------------------------------------------------------------------
# Call sites
# ---------------------------------------------------------------------------

class TestTheApiIsWiredUp:
    """
    The library above is useless if nothing exposes it. The request router
    resolves `/trawlarr/api/v2/reprocess/...` to `ApiReprocessHandler` by name
    from the api_v2 package, so both the export and the routes are load-bearing.
    """

    def test_the_handler_is_exported_from_the_api_package(self):
        from trawlarr.webserver import api_v2

        assert 'ApiReprocessHandler' in api_v2.list_all_handlers(), (
            "The reprocess handler is not exported from trawlarr.webserver.api_v2, so the request "
            "router cannot find it and there is no way to reprocess a library short of editing the "
            "database by hand - which is the situation issue #41 exists to end."
        )

    def test_both_routes_are_declared(self):
        from trawlarr.webserver.api_v2 import ApiReprocessHandler

        routes = {route['path_pattern']: route for route in ApiReprocessHandler.routes}
        assert set(routes) == {r'/reprocess/preview', r'/reprocess/apply'}
        assert routes[r'/reprocess/preview']['supported_methods'] == ['POST']
        assert routes[r'/reprocess/apply']['supported_methods'] == ['POST']

    def test_apply_calls_the_library_and_not_a_second_implementation(self):
        import ast
        import inspect
        import textwrap

        from trawlarr.webserver.api_v2 import reprocess_api

        source = textwrap.dedent(inspect.getsource(reprocess_api.ApiReprocessHandler.apply_reprocess_selection))
        called = [node.func.attr for node in ast.walk(ast.parse(source))
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        assert 'apply_selection' in called, (
            "The apply endpoint no longer calls reprocess.apply_selection(). Every guard in that "
            "function - the scope check, the confirm_count check, the cooldown and the audit trail - "
            "is bypassed, and the endpoint is a way to delete completed-task history in bulk with no "
            "preview and no record."
        )


# ---------------------------------------------------------------------------
# Documented behaviour
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _read_doc():
    with open(os.path.join(PROJECT_ROOT, 'docs', 'REPROCESSING.md'), encoding='utf-8') as f:
        return f.read()


class TestTheDocumentedClaims:
    """
    docs/REPROCESSING.md states these as facts. Each one is pinned to the code
    it describes so the document cannot quietly become wrong.
    """

    def test_the_documented_defaults_are_the_real_defaults(self):
        doc = _read_doc()
        assert '`{}` hours'.format(reprocess.DEFAULT_COOLDOWN_HOURS) in doc
        assert str(reprocess.MAX_SELECTION) in doc

    def test_the_documented_skip_reasons_are_the_real_ones(self):
        doc = _read_doc()
        for reason in (reprocess.SKIP_MISSING, reprocess.SKIP_QUEUED, reprocess.SKIP_FAILED_HISTORY,
                       reprocess.SKIP_COOLDOWN, reprocess.SKIP_NO_MATCH, reprocess.SKIP_FILE_TEST_ERROR):
            assert '`{}`'.format(reason) in doc, \
                "docs/REPROCESSING.md does not document the '{}' skip reason".format(reason)

    def test_the_document_tells_the_caller_to_send_the_digest(self):
        # The worked example is what people copy. If apply() needs a value the
        # example does not send, the documented path does not work.
        doc = _read_doc()
        assert 'confirm_digest' in doc
        assert '"confirm_digest"' in doc

    def test_the_documented_active_statuses_are_the_real_ones(self):
        doc = _read_doc()
        for status in reprocess._ACTIVE_TASK_STATUSES:
            assert '`{}`'.format(status) in doc, \
                "docs/REPROCESSING.md does not mention the '{}' task status that is skipped".format(status)

    def test_the_document_does_not_claim_a_ui(self):
        # The first cut is an API and a documented path. If a UI lands, this
        # test is the reminder to say so here.
        doc = _read_doc().lower()
        assert 'there is no user interface for this' in doc


@pytest.mark.unittest
class TestTheScopeCheckCannotBeSpelledAround:
    """The scope check asks whether a glob matches everything, not what it is
    made of.

    The first version tested that every character was a wildcard or a
    separator. `'[/]*'` walked straight through it -- three literal
    characters, and it still fnmatch-matches every absolute path there is. A
    review probe confirmed it: preview reported 9 files across 3 libraries
    from a single "scoped" request.

    Asking the question semantically closes the whole class rather than that
    one spelling, which matters because fnmatch's grammar is not ours to
    freeze: `?`, `[!x]` negation and anything it grows later are covered
    without being enumerated.
    """

    @pytest.mark.parametrize('path_glob', [
        '*',            # the obvious one
        '/*',
        '*/*',
        '[/]*',         # the review's bypass: literal characters, matches everything
        '?*',           # one-or-more, still everything
        '*[!zzzz]*',    # negated class, still everything
        '**',
        '/**/*',
        '*/*/*',
    ])
    def test_a_glob_that_matches_everything_is_not_a_scope(self, path_glob):
        assert reprocess._glob_restricts_nothing(path_glob) is True, (
            "{!r} matches every path, so a request scoped only by it is the "
            "'everything ever processed' request under another name".format(path_glob)
        )

    @pytest.mark.parametrize('path_glob', [
        '*.mkv',                  # wide, but names an extension
        '/library/*',             # names a directory
        '[ab]*',                  # a real character class
        '/mnt/tv/**',
        'S01*',
        '/library/Show/*.mkv',
    ])
    def test_a_glob_that_names_a_real_subset_is_allowed_however_wide(self, path_glob):
        assert reprocess._glob_restricts_nothing(path_glob) is False, (
            "{!r} excludes something, so it is a scope. The bound on wide-but-real "
            "selections is MAX_SELECTION and the preview, not this check.".format(path_glob)
        )
