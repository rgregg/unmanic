#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_native_done_state.py

    Issue #33: the platform's own answers to "is this file in scope for this
    library" and "is this file already done", which used to be delegated to
    two ordinary file-test plugins and were therefore outvotable.

    The tests that matter most here are the ones asserting that a native
    decision cannot be overturned by a plugin voting True, because that
    override is exactly what produced the infinite reprocess loop.
"""
import logging
import os

import pytest

from trawlarr.libs import donestate, extensions
from trawlarr.libs.filetest import FileTest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def completion_db():
    """Bind FileCompletionState to a throwaway in-memory database."""
    from peewee import SqliteDatabase
    from trawlarr.libs.unmodels import FileCompletionState

    database = SqliteDatabase(':memory:')
    with database.bind_ctx([FileCompletionState]):
        database.create_tables([FileCompletionState])
        yield database
    database.close()


def _bare_file_test(plugin_votes, allowlist=(), ignore_completed_files=False):
    """A FileTest without __init__ - that would build a real config, a real
    PluginsHandler and hit the database. Mirrors the helper in
    tests/unit/test_filetest_vote_precedence.py.
    """
    ft = FileTest.__new__(FileTest)
    ft.logger = logging.getLogger("test_native_done_state")
    ft.library_id = 1
    ft.failed_paths = ['/some/other/file/that/failed.mkv']
    ft.file_extension_allowlist = tuple(allowlist)
    ft.ignore_completed_files = ignore_completed_files
    ft.plugin_modules = [
        {'plugin_id': plugin_id, 'name': plugin_id.replace('_', ' ')}
        for plugin_id, _vote in plugin_votes
    ]

    votes = dict(plugin_votes)
    executed = []

    def exec_plugin_runner(data, plugin_id, plugin_type):
        executed.append(plugin_id)
        data['add_file_to_pending_tasks'] = votes[plugin_id]
        return True

    ft.plugin_handler = type('FakeHandler', (), {'exec_plugin_runner': staticmethod(exec_plugin_runner)})()
    ft.executed_plugins = executed
    return ft


@pytest.fixture
def video(tmp_path):
    """A real file on disk, so it can be stat'd and signed."""
    path = tmp_path / "episode.mkv"
    path.write_bytes(b"x" * 1024)
    return str(path)


def _issue_ids(issues):
    return [i.get('id') for i in issues if isinstance(i, dict)]


# ---------------------------------------------------------------------------
# Extension allow-list parsing
# ---------------------------------------------------------------------------

class TestAllowlistParsing:

    @pytest.mark.parametrize('raw,expected', [
        ('mkv,mp4', ('mkv', 'mp4')),
        ('.MKV, .Mp4', ('mkv', 'mp4')),
        ('mkv mp4\navi', ('mkv', 'mp4', 'avi')),
        (['.mkv', 'MKV', ' mp4 '], ('mkv', 'mp4')),
        ('', ()),
        (None, ()),
        (',,  ,', ()),
    ])
    def test_parse(self, raw, expected):
        assert extensions.parse_allowlist(raw) == expected

    def test_storage_form_round_trips(self):
        stored = extensions.format_allowlist(['.MKV', 'mp4', 'mkv'])
        assert stored == 'mkv,mp4'
        assert extensions.parse_allowlist(stored) == ('mkv', 'mp4')

    def test_an_empty_allowlist_allows_everything(self):
        """The upgrade default. An empty allow-list matching nothing would
        stop every existing library dead, silently."""
        for path in ['/library/ep.mkv', '/library/poster.jpg', '/library/no_extension']:
            assert extensions.extension_is_allowed(path, '') is True
            assert extensions.extension_is_allowed(path, []) is True
            assert extensions.extension_is_allowed(path, None) is True

    def test_a_populated_allowlist_restricts(self):
        assert extensions.extension_is_allowed('/library/ep.mkv', 'mkv,mp4') is True
        assert extensions.extension_is_allowed('/library/ep.MKV', 'mkv,mp4') is True
        assert extensions.extension_is_allowed('/library/poster.jpg', 'mkv,mp4') is False
        assert extensions.extension_is_allowed('/library/no_extension', 'mkv,mp4') is False


# ---------------------------------------------------------------------------
# The done-state record itself
# ---------------------------------------------------------------------------

class TestCompletionRecord:

    def test_an_unknown_file_is_not_completed(self, completion_db, video):
        assert donestate.file_is_already_completed(video) == (False, '')

    def test_a_recorded_file_is_completed(self, completion_db, video):
        assert donestate.record_completion(video, library_id=1, task_id=7) is True

        completed, message = donestate.file_is_already_completed(video)

        assert completed is True
        assert 'already completed successfully' in message
        assert video in message

    def test_a_rescan_of_an_untouched_file_stays_completed(self, completion_db, video):
        donestate.record_completion(video, library_id=1, task_id=7)

        for _ in range(5):
            assert donestate.file_is_already_completed(video)[0] is True

    def test_a_re_download_to_the_same_path_is_not_completed(self, completion_db, video):
        """The distinction the issue asked for: same path, different file."""
        donestate.record_completion(video, library_id=1, task_id=7)

        # Same size, later mtime - a fresh copy of the same release.
        stat_result = os.stat(video)
        os.utime(video, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1000000000))

        assert donestate.file_is_already_completed(video)[0] is False

    def test_a_file_that_changed_size_is_not_completed(self, completion_db, video):
        donestate.record_completion(video, library_id=1, task_id=7)
        with open(video, 'ab') as f:
            f.write(b"more bytes")

        assert donestate.file_is_already_completed(video)[0] is False

    def test_a_stale_record_is_dropped_not_left_behind(self, completion_db, video):
        from trawlarr.libs.unmodels import FileCompletionState

        donestate.record_completion(video, library_id=1, task_id=7)
        with open(video, 'ab') as f:
            f.write(b"more bytes")
        donestate.file_is_already_completed(video)

        assert FileCompletionState.get_or_none(FileCompletionState.abspath == video) is None

    def test_a_missing_file_is_not_completed(self, completion_db, tmp_path):
        assert donestate.file_is_already_completed(str(tmp_path / "gone.mkv")) == (False, '')

    def test_a_completion_that_cannot_be_measured_is_not_recorded(self, completion_db, tmp_path):
        """Recording an unverifiable row would mean silently skipping whatever
        later turns up at that path."""
        assert donestate.record_completion(str(tmp_path / "gone.mkv"), library_id=1) is False

    def test_a_record_written_by_an_unknown_algorithm_is_not_trusted(self, completion_db, video):
        from trawlarr.libs.unmodels import FileCompletionState

        donestate.record_completion(video, library_id=1, task_id=7)
        row = FileCompletionState.get(FileCompletionState.abspath == video)
        row.signature_algo = 'ffprobe_fingerprint_v9'
        row.save()

        assert donestate.file_is_already_completed(video)[0] is False

    def test_a_second_completion_updates_in_place(self, completion_db, video):
        from trawlarr.libs.unmodels import FileCompletionState

        donestate.record_completion(video, library_id=1, task_id=1)
        with open(video, 'ab') as f:
            f.write(b"grown")
        donestate.record_completion(video, library_id=1, task_id=2)

        assert FileCompletionState.select().where(FileCompletionState.abspath == video).count() == 1
        assert donestate.file_is_already_completed(video)[0] is True

    def test_forget_path_makes_a_file_eligible_again(self, completion_db, video):
        """The seam for issue #41."""
        donestate.record_completion(video, library_id=1, task_id=7)
        assert donestate.file_is_already_completed(video)[0] is True

        assert donestate.forget_path(video) == 1

        assert donestate.file_is_already_completed(video)[0] is False

    def test_no_database_bound_degrades_quietly(self, video):
        """No database at all. The check is a gate on queueing work, not a
        reason to take the scanner down."""
        assert donestate.file_is_already_completed(video) == (False, '')
        assert donestate.record_completion(video, library_id=1) is False
        assert donestate.forget_path(video) == 0


# ---------------------------------------------------------------------------
# FileTest tier 0
# ---------------------------------------------------------------------------

class TestExtensionAllowlistGate:

    def test_an_empty_allowlist_lets_everything_through(self, tmp_path):
        """What every library gets on upgrade."""
        poster = tmp_path / "poster.jpg"
        poster.write_bytes(b"x")
        ft = _bare_file_test([('ensure_2ch_aac_audio', True)], allowlist=())

        result, issues, _score, _plugin = ft.should_file_be_added_to_task_list(str(poster))

        assert result is True
        assert 'extensionnotallowed' not in _issue_ids(issues)

    def test_a_file_outside_the_allowlist_is_rejected(self, tmp_path):
        poster = tmp_path / "poster.jpg"
        poster.write_bytes(b"x")
        ft = _bare_file_test([('ensure_2ch_aac_audio', True)], allowlist=('mkv', 'mp4'))

        result, issues, _score, _plugin = ft.should_file_be_added_to_task_list(str(poster))

        assert result is False
        assert 'extensionnotallowed' in _issue_ids(issues)

    def test_a_plugin_voting_true_cannot_override_the_allowlist(self, tmp_path):
        """The native check is above the plugin vote, not part of it. This is
        the property that `limit_library_search_by_file_extension` could not
        provide, because its vote was outvotable."""
        poster = tmp_path / "poster.jpg"
        poster.write_bytes(b"x")
        ft = _bare_file_test([
            ('requester_one', True),
            ('requester_two', True),
        ], allowlist=('mkv',))

        result, _issues, _score, _plugin = ft.should_file_be_added_to_task_list(str(poster))

        assert result is False

    def test_a_rejected_file_never_reaches_the_plugins(self, tmp_path):
        poster = tmp_path / "poster.jpg"
        poster.write_bytes(b"x")
        ft = _bare_file_test([('ensure_2ch_aac_audio', True)], allowlist=('mkv',))

        ft.should_file_be_added_to_task_list(str(poster))

        assert ft.executed_plugins == []

    def test_a_file_inside_the_allowlist_is_still_offered_to_plugins(self, video):
        ft = _bare_file_test([('ensure_2ch_aac_audio', True)], allowlist=('mkv',))

        result, _issues, _score, _plugin = ft.should_file_be_added_to_task_list(video)

        assert result is True
        assert ft.executed_plugins == ['ensure_2ch_aac_audio']


class TestCompletedFileGate:

    def test_a_completed_file_is_not_queued_again(self, completion_db, video):
        donestate.record_completion(video, library_id=1, task_id=7)
        ft = _bare_file_test([('ensure_2ch_aac_audio', True)])

        result, issues, _score, _plugin = ft.should_file_be_added_to_task_list(video)

        assert result is False
        assert 'alreadycompleted' in _issue_ids(issues)

    def test_a_plugin_voting_true_cannot_re_queue_a_completed_file(self, completion_db, video):
        """The reprocess loop, pinned. Before this, `ignore_completed_tasks`
        voted False and any requester plugin voting True beat it."""
        donestate.record_completion(video, library_id=1, task_id=7)
        ft = _bare_file_test([
            ('requester_one', True),
            ('requester_two', True),
        ])

        result, _issues, _score, _plugin = ft.should_file_be_added_to_task_list(video)

        assert result is False
        assert ft.executed_plugins == []

    def test_a_file_that_was_never_completed_is_offered_to_plugins(self, completion_db, video):
        ft = _bare_file_test([('ensure_2ch_aac_audio', True)])

        result, _issues, _score, _plugin = ft.should_file_be_added_to_task_list(video)

        assert result is True

    def test_a_replaced_file_at_a_completed_path_is_queued_again(self, completion_db, video):
        donestate.record_completion(video, library_id=1, task_id=7)
        with open(video, 'ab') as f:
            f.write(b"a whole new download")
        ft = _bare_file_test([('ensure_2ch_aac_audio', True)])

        result, _issues, _score, _plugin = ft.should_file_be_added_to_task_list(video)

        assert result is True

    def test_ignore_completed_files_bypasses_the_gate(self, completion_db, video):
        """The per-caller seam for issue #41: reprocess without discarding the
        stored state."""
        donestate.record_completion(video, library_id=1, task_id=7)
        ft = _bare_file_test([('ensure_2ch_aac_audio', True)], ignore_completed_files=True)

        result, _issues, _score, _plugin = ft.should_file_be_added_to_task_list(video)

        assert result is True
        assert donestate.file_is_already_completed(video)[0] is True


# ---------------------------------------------------------------------------
# The write side: the post-processor
# ---------------------------------------------------------------------------

def _build_postprocessor(source_abspath, destination_abspath, destination_files, success=True):
    from trawlarr.libs.postprocessor import PostProcessor

    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger("test.postprocessor.donestate")
    pp._last_destination_files = list(destination_files)
    pp._last_file_move_processes_success = True

    class _CurrentTask:
        def get_task_id(self_inner):
            return 42

        def get_task_library_id(self_inner):
            return 3

        def get_task_success(self_inner):
            return success

        def get_source_data(self_inner):
            return {"abspath": source_abspath}

        def get_destination_data(self_inner):
            return {"abspath": destination_abspath}

    pp.current_task = _CurrentTask()
    return pp


class TestPostProcessorRecordsCompletion:

    def test_a_successful_task_records_its_output(self, completion_db, video):
        pp = _build_postprocessor(video, video, [video])

        assert pp.record_completed_file() == [video]
        assert donestate.file_is_already_completed(video)[0] is True

    def test_a_failed_task_records_nothing(self, completion_db, video):
        """A failed task is already terminal via file_failed_in_history(), and
        the file on disk is the untouched source."""
        pp = _build_postprocessor(video, video, [video], success=False)

        assert pp.record_completed_file() == []
        assert donestate.file_is_already_completed(video)[0] is False

    def test_a_renamed_output_moves_the_record_off_the_source_path(self, completion_db, tmp_path):
        source = tmp_path / "ep.avi"
        source.write_bytes(b"a" * 64)
        destination = tmp_path / "ep.mkv"
        destination.write_bytes(b"b" * 64)
        donestate.record_completion(str(source), library_id=3, task_id=1)

        pp = _build_postprocessor(str(source), str(destination), [str(destination)])
        pp.record_completed_file()

        from trawlarr.libs.unmodels import FileCompletionState
        assert FileCompletionState.get_or_none(FileCompletionState.abspath == str(source)) is None
        assert donestate.file_is_already_completed(str(destination))[0] is True

    def test_every_delivered_copy_is_recorded(self, completion_db, tmp_path):
        first = tmp_path / "ep.mkv"
        first.write_bytes(b"a" * 16)
        second = tmp_path / "archive" / "ep.mkv"
        second.parent.mkdir()
        second.write_bytes(b"a" * 16)

        pp = _build_postprocessor(str(first), str(first), [str(first), str(second)])
        pp.record_completed_file()

        assert donestate.file_is_already_completed(str(first))[0] is True
        assert donestate.file_is_already_completed(str(second))[0] is True

    def test_a_task_that_delivered_nothing_records_nothing(self, completion_db, video):
        pp = _build_postprocessor(video, None, [])

        assert pp.record_completed_file() == []
        assert donestate.file_is_already_completed(video)[0] is False


class TestPluginVotePrecedenceIsUnchanged:
    """Issue #33 adds a tier above the plugin vote. It deliberately does not
    alter the tiers below it - see the note at the top of filetest.py."""

    def test_the_guard_veto_from_issue_32_still_stands(self, completion_db, video):
        ft = _bare_file_test([
            ('ignore_completed_tasks', False),
            ('ensure_2ch_aac_audio', True),
        ])

        result, _issues, _score, decision_plugin = ft.should_file_be_added_to_task_list(video)

        assert result is False
        assert decision_plugin.get('plugin_id') == 'ignore_completed_tasks'

    def test_the_advisory_skip_list_still_stands(self, completion_db, video):
        from trawlarr.libs.filetest import ADVISORY_SKIP_PLUGIN_IDS

        assert 'skip_files_matching_ffprobe_data' in ADVISORY_SKIP_PLUGIN_IDS

        ft = _bare_file_test([
            ('skip_files_matching_ffprobe_data', False),
            ('ensure_2ch_aac_audio', True),
        ])

        result, _issues, _score, decision_plugin = ft.should_file_be_added_to_task_list(video)

        assert result is True
        assert decision_plugin.get('plugin_id') == 'ensure_2ch_aac_audio'
