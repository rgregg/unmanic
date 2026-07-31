#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_postprocessor_failed_move.py

    A file that was never replaced must never be recorded as done.

    `record_completed_file()` writes the state that stops a file being offered
    to the plugins again (#33). It used to gate on task success alone. But
    "the task succeeded" only means the WORKER produced an output in the cache
    directory - the post-processor still has to get that output into the
    library, and that step has its own verdict, `file_move_processes_success`.

    When the move fails (disk full, read-only mount, a `postprocessor.file_move`
    plugin raising) the post-processor deliberately leaves the original file
    alone: see the "Keeping source file" branch in post_process_file(). Nothing
    was delivered, so `_last_destination_files` is empty, and
    record_completed_file() fell through to `destination_data['abspath']` -
    which on the common in-place path IS the source path. It signed off the
    untouched original.

    The consequence is silent and permanent: the file is on disk unprocessed,
    the done-state row says otherwise, and no future library scan will ever
    queue it again.
"""
import logging

import pytest

from trawlarr.libs import donestate
from trawlarr.libs.postprocessor import PostProcessor


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


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "episode.mkv"
    path.write_bytes(b"the original, unprocessed file" * 8)
    return str(path)


def _postprocessor(source_abspath, destination_abspath, destination_files,
                   task_success=True, file_move_success=True):
    """PostProcessor without its thread/queue machinery, mirroring the helper
    the other post-processor tests use."""
    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger("test.postprocessor.failed_move")
    pp._last_destination_files = list(destination_files)
    pp._last_file_move_processes_success = file_move_success

    class _CurrentTask:
        def get_task_id(self_inner):
            return 42

        def get_task_library_id(self_inner):
            return 3

        def get_task_success(self_inner):
            return task_success

        def get_source_data(self_inner):
            return {"abspath": source_abspath}

        def get_destination_data(self_inner):
            return {"abspath": destination_abspath}

    pp.current_task = _CurrentTask()
    return pp


@pytest.mark.unittest
class TestAFailedFileMoveRecordsNothing:

    def test_an_in_place_task_whose_move_failed_does_not_sign_off_the_source(self, completion_db, video):
        """The reported bug, in its most damaging shape.

        Destination path == source path (the usual in-place transcode), the
        worker succeeded, the move failed, nothing was delivered. The file at
        that path is the ORIGINAL. Recording it as done retires a file that
        was never processed.
        """
        pp = _postprocessor(video, video, [], file_move_success=False)

        recorded = pp.record_completed_file()

        assert recorded == [], (
            "A task whose post-processor file movement failed recorded {}. "
            "That path holds the untouched source file.".format(recorded))
        assert donestate.file_is_already_completed(video)[0] is False, (
            "The unprocessed source is now marked done and will never be "
            "queued again.")

    def test_a_renamed_task_whose_move_failed_leaves_the_source_record_alone(self, completion_db, tmp_path):
        """The other half of the damage.

        On a rename the method also calls forget_path() on the source. If the
        move failed, the source is still exactly where it was and still the
        real file - dropping its done-state row would hand it straight back to
        the scanner for a reprocess that already failed once.
        """
        source = tmp_path / "ep.avi"
        source.write_bytes(b"a" * 64)
        destination = tmp_path / "ep.mkv"
        donestate.record_completion(str(source), library_id=3, task_id=1)

        pp = _postprocessor(str(source), str(destination), [], file_move_success=False)

        assert pp.record_completed_file() == []
        assert donestate.file_is_already_completed(str(source))[0] is True, (
            "A failed move discarded the source file's existing done-state.")
        assert donestate.file_is_already_completed(str(destination))[0] is False

    def test_a_partial_delivery_from_a_failed_move_is_not_recorded(self, completion_db, tmp_path):
        """A plugin copy can land one file before a later copy fails.

        `destination_files` is then non-empty, so the empty-list fallback is
        not what saves us here - the verdict has to be checked in its own
        right. A half-finished delivery is not a completed file.
        """
        delivered = tmp_path / "archive" / "ep.mkv"
        delivered.parent.mkdir()
        delivered.write_bytes(b"b" * 32)
        source = tmp_path / "ep.mkv"
        source.write_bytes(b"a" * 64)

        pp = _postprocessor(str(source), str(source), [str(delivered)], file_move_success=False)

        assert pp.record_completed_file() == []
        assert donestate.file_is_already_completed(str(delivered))[0] is False

    def test_a_successful_move_still_records_normally(self, completion_db, video):
        """The gate must not swallow the happy path it sits in front of."""
        pp = _postprocessor(video, video, [video], file_move_success=True)

        assert pp.record_completed_file() == [video]
        assert donestate.file_is_already_completed(video)[0] is True


@pytest.mark.unittest
class TestTheMoveVerdictIsPerTask:

    def test_the_run_loop_clears_the_move_verdict_before_using_it(self):
        """The verdict is an attribute on a long-lived thread.

        post_process_file() is the only writer and run() calls it inside a
        try/except that logs and carries on. If it raises before assigning,
        a True left over from the previous task would defeat the gate above
        for a task that moved nothing at all.
        """
        import inspect
        source = inspect.getsource(PostProcessor.run)

        assert 'self._last_file_move_processes_success = False' in source, (
            "run() does not clear the file-move verdict per task; a task that "
            "failed before post-processing inherits the previous task's "
            "success and records its own untouched source as done.")

        reset_at = source.index('self._last_file_move_processes_success = False')
        post_process_at = source.index('self.post_process_file()')
        assert reset_at < post_process_at, (
            "the verdict is cleared after post-processing, which would erase "
            "the result the task just produced")
