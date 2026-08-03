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

        result = reprocess.apply_selection(1, library_id=1)

        assert result['applied']['completion_records_cleared'] == 1
        # The claim the whole feature rests on, asserted against the check the
        # scanner actually runs rather than against a row count.
        assert donestate.file_is_already_completed(abspath)[0] is False

    def test_nothing_is_queued(self, library):
        _completed(_file(library, 'a.mkv'))
        reprocess.apply_selection(1, library_id=1)
        # Reprocessing removes records. Queueing is the scanner's job, and if
        # this ever starts creating tasks itself it has become the silent
        # re-queue that issues #33 and #34 were written to stop.
        assert Tasks.select().count() == 0

    def test_files_outside_the_filter_keep_their_completed_state(self, library):
        keep = _completed(_file(library, 'keep.mp4'))
        _completed(_file(library, 'go.mkv'))
        reprocess.apply_selection(1, path_glob=os.path.join(library, '*.mkv'))
        assert donestate.file_is_already_completed(keep)[0] is True

    def test_an_audit_row_is_written_for_every_invalidated_file(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        reprocess.apply_selection(1, library_id=1)
        record = reprocess.history_for_path(abspath)
        assert record is not None
        assert record['occurrences'] == 1
        assert 'library_id=1' in record['requested_by']

    def test_the_audit_count_climbs_with_every_reprocess(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        reprocess.apply_selection(1, library_id=1)
        _completed(abspath)
        # Second request, well outside the cooldown window.
        later = datetime.datetime.now() + datetime.timedelta(days=30)
        reprocess.apply_selection(1, library_id=1, now=later)
        # This count is the loop signal: a script re-invalidating the same
        # library for ever leaves a number here with the file's name on it.
        assert reprocess.history_for_path(abspath)['occurrences'] == 2


# ---------------------------------------------------------------------------
# Preview before acting
# ---------------------------------------------------------------------------

class TestNothingIsInvalidatedWithoutAPreview:

    def test_a_wrong_confirm_count_refuses_and_changes_nothing(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        with pytest.raises(reprocess.ReprocessRefused) as excinfo:
            reprocess.apply_selection(5, library_id=1)
        assert 'Nothing has been changed' in str(excinfo.value)
        assert donestate.file_is_already_completed(abspath)[0] is True
        assert FileReprocessState.select().count() == 0

    def test_a_missing_confirm_count_refuses(self, library):
        _completed(_file(library, 'a.mkv'))
        with pytest.raises(reprocess.ReprocessRefused):
            reprocess.apply_selection(None, library_id=1)

    def test_a_selection_that_grew_since_the_preview_refuses(self, library):
        _completed(_file(library, 'a.mkv'))
        previewed = reprocess.build_selection(library_id=1)['counts']['selected']
        # Something else finishes a task between the preview and the apply.
        _completed(_file(library, 'b.mkv'))
        with pytest.raises(reprocess.ReprocessRefused):
            reprocess.apply_selection(previewed, library_id=1)
        assert FileCompletionState.select().count() == 2

    def test_the_preview_changes_nothing(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        reprocess.build_selection(library_id=1)
        assert donestate.file_is_already_completed(abspath)[0] is True
        assert FileReprocessState.select().count() == 0

    def test_the_count_the_preview_reports_is_the_count_apply_accepts(self, library):
        for name in ('a.mkv', 'b.mkv', 'c.mkv'):
            _completed(_file(library, name))
        selection = reprocess.build_selection(library_id=1)
        result = reprocess.apply_selection(selection['counts']['selected'], library_id=1)
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
        result = reprocess.apply_selection(5, library_id=1, limit=2)
        # Acting on only the two files the preview happened to list would do
        # part of what was asked, quietly.
        assert result['applied']['completion_records_cleared'] == 5
        assert FileCompletionState.select().count() == 0


# ---------------------------------------------------------------------------
# Files that must be left alone
# ---------------------------------------------------------------------------

class TestFilesThatAreSkipped:

    @pytest.mark.parametrize('status', ['pending', 'in_progress'])
    def test_a_file_already_in_the_queue_is_skipped(self, library, status):
        abspath = _completed(_file(library, 'a.mkv'))
        Tasks.create(abspath=abspath, library_id=1, status=status, type='local')

        selection = reprocess.build_selection(library_id=1)

        assert selection['counts']['selected'] == 0
        assert _skip_reasons(selection) == {abspath: reprocess.SKIP_QUEUED}

    def test_a_file_with_a_processed_task_is_not_skipped(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        Tasks.create(abspath=abspath, library_id=1, status='processed', type='local')
        assert reprocess.build_selection(library_id=1)['counts']['selected'] == 1

    def test_a_file_that_is_no_longer_on_disk_is_skipped(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        os.remove(abspath)
        selection = reprocess.build_selection(library_id=1)
        assert selection['counts']['selected'] == 0
        assert _skip_reasons(selection) == {abspath: reprocess.SKIP_MISSING}

    def test_a_recently_reprocessed_file_is_skipped(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        reprocess.apply_selection(1, library_id=1)
        _completed(abspath)

        selection = reprocess.build_selection(library_id=1)

        assert selection['counts']['selected'] == 0
        assert _skip_reasons(selection) == {abspath: reprocess.SKIP_COOLDOWN}

    def test_force_overrides_the_cooldown(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        reprocess.apply_selection(1, library_id=1)
        _completed(abspath)
        assert reprocess.build_selection(library_id=1, force=True)['counts']['selected'] == 1

    def test_the_cooldown_expires(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        reprocess.apply_selection(1, library_id=1)
        _completed(abspath)
        later = datetime.datetime.now() + datetime.timedelta(hours=25)
        assert reprocess.build_selection(library_id=1, now=later)['counts']['selected'] == 1


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

        reprocess.apply_selection(1, library_id=1, include_failed=True)

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
        reprocess.apply_selection(1, library_id=1, include_failed=True)
        assert CompletedTasks.select().where(CompletedTasks.id == good.id).count() == 1
        assert CompletedTasks.select().where(CompletedTasks.task_success == False).count() == 0  # noqa: E712

    def test_failed_history_is_left_alone_without_include_failed(self, library):
        abspath = _file(library, 'a.mkv')
        other = _completed(_file(library, 'b.mkv'))
        _completed(abspath)
        _failed_history(abspath)

        result = reprocess.apply_selection(1, library_id=1)

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

        result = reprocess.apply_selection(1, library_id=1)

        # Issue #34's occurrence count only advances across deliberate
        # reprocesses. Clearing it here would guarantee it never reaches its
        # repeat limit and never reports a file that can never converge.
        assert result['applied']['convergence_records_cleared'] == 0
        assert FileConvergenceState.select().where(FileConvergenceState.abspath == abspath).count() == 1

    def test_they_are_cleared_when_asked(self, library):
        abspath = _completed(_file(library, 'a.mkv'))
        self._non_converged(abspath)

        result = reprocess.apply_selection(1, library_id=1, clear_convergence=True)

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
        reprocess.apply_selection(1, library_id=1, match_file_test=True,
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

    def test_the_document_does_not_claim_a_ui(self):
        # The first cut is an API and a documented path. If a UI lands, this
        # test is the reminder to say so here.
        doc = _read_doc().lower()
        assert 'there is no user interface for this' in doc
