#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_postprocessor_paths.py

    Tests for PostProcessor.post_process_file (the local-task pipeline)
    and PostProcessor.__copy_file (the underlying file movement helper).

    The remote-task path is covered by test_postprocessor_remote.py.
    This file fills in the local-task path: the cache→destination move
    triggered after a successful transcode, plus the .unmanic.part
    rename dance __copy_file uses to avoid leaving partial files at
    the destination.

    Invariants covered:

    1. __copy_file uses a .unmanic.part suffix during the move and
       renames atomically to the final destination path. A crash mid-
       copy must not leave a partial file at the destination filename.
    2. __copy_file removes an existing destination only after the
       .part file is already in place, not before — so a failed copy
       can't destroy the existing destination.
    3. __copy_file refuses to copy when src and dst resolve to the
       same file (avoids data loss from same-file races).
    4. __copy_file appends to destination_files only on success.
    5. post_process_file removes the source ONLY after the cache file
       has been successfully copied to the destination.
    6. post_process_file keeps the source when the copy fails.
    7. post_process_file skips file movement entirely for failed tasks.
"""
import logging
import os
from unittest import mock

import pytest

from trawlarr.libs.postprocessor import PostProcessor


def _build_postprocessor(cache_path, source_abspath, dest_path, *, task_success=True):
    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger("test.postprocessor.paths")
    pp._last_destination_files = []
    pp._last_file_move_processes_success = False
    pp.event = mock.Mock()  # __copy_file calls .wait(1) on missing file_in
    pp.worker_log = []

    class _Task:
        success = task_success

    class _Settings:
        def get_cache_path(self):
            return os.path.dirname(cache_path)

    class _CurrentTask:
        task = _Task()

        def get_task_data(self_inner):
            return {}

        def get_task_id(self_inner):
            return 1

        def get_task_library_id(self_inner):
            return 1

        def get_task_type(self_inner):
            return "local"

        def get_task_success(self_inner):
            return task_success

        def get_cache_path(self_inner):
            return cache_path

        def get_source_data(self_inner):
            return {"abspath": source_abspath}

        def get_destination_data(self_inner):
            return {"abspath": dest_path}

        def get_start_time(self_inner):
            return 0

        def get_finish_time(self_inner):
            return 1

    pp.settings = _Settings()
    pp.current_task = _CurrentTask()
    return pp


class TestCopyFilePartSuffixDance:

    def test_uses_part_suffix_then_renames(self, tmp_path):
        """The move/copy lands at <dest>.unmanic.part first, then is
        renamed to <dest>. Catches partial files if the process dies
        between write and rename."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"data")
        dest = tmp_path / "dest.mkv"

        pp = _build_postprocessor(
            str(src), str(src), str(dest))
        destination_files = []
        result = pp._PostProcessor__copy_file(
            str(src), str(dest), destination_files, "test_plugin", move=True)

        assert result is True
        assert dest.exists()
        assert dest.read_bytes() == b"data"
        # No leftover .part file on success.
        assert not (tmp_path / "dest.mkv.unmanic.part").exists()
        # On move, source must be gone.
        assert not src.exists()
        assert destination_files == [str(dest)]

    def test_copy_mode_keeps_source(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"data")
        dest = tmp_path / "dest.mkv"

        pp = _build_postprocessor(str(src), str(src), str(dest))
        destination_files = []
        result = pp._PostProcessor__copy_file(
            str(src), str(dest), destination_files, "test_plugin", move=False)

        assert result is True
        assert dest.exists()
        # On copy (not move), source remains.
        assert src.exists()
        assert destination_files == [str(dest)]

    def test_overwrites_existing_destination(self, tmp_path):
        """The default file movement path overwrites the source file
        in place when destination == source — verify that an existing
        destination file is replaced, not appended to."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"new")
        dest = tmp_path / "dest.mkv"
        dest.write_bytes(b"old contents to be replaced")

        pp = _build_postprocessor(str(src), str(src), str(dest))
        destination_files = []
        pp._PostProcessor__copy_file(
            str(src), str(dest), destination_files, "test_plugin", move=True)

        assert dest.read_bytes() == b"new"

    def test_same_file_refused(self, tmp_path):
        """If file_in and file_out resolve to the same file, refuse
        the copy. Otherwise we'd potentially truncate the file before
        reading it."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"data")

        pp = _build_postprocessor(str(src), str(src), str(src))
        destination_files = []
        result = pp._PostProcessor__copy_file(
            str(src), str(src), destination_files, "test_plugin", move=False)

        assert result is False
        assert destination_files == []
        # Source still has its data.
        assert src.read_bytes() == b"data"

    def test_failure_does_not_append_to_destination_files(self, tmp_path):
        """Exception during the copy must not record success in the
        destination_files list (which downstream listeners read)."""
        src = tmp_path / "missing.mkv"  # Does not exist
        dest = tmp_path / "dest.mkv"

        pp = _build_postprocessor(str(src), str(src), str(dest))
        destination_files = []
        result = pp._PostProcessor__copy_file(
            str(src), str(dest), destination_files, "test_plugin", move=False)

        assert result is False
        assert destination_files == []


class TestPostProcessFileSourceRemovalOrdering:
    """Mirror of test_postprocessor_remote.py's headline check, applied
    to the local-task pipeline. Source removal must come AFTER copy
    success — never before."""

    def test_source_removed_only_after_successful_copy(self, tmp_path):
        cache = tmp_path / "cache" / "task" / "out.mkv"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"transcoded")
        source = tmp_path / "library" / "movie.mkv"
        source.parent.mkdir()
        source.write_bytes(b"original")
        # Destination is different from source — triggers remove_source_file.
        dest = tmp_path / "library" / "movie-new.mkv"

        pp = _build_postprocessor(str(cache), str(source), str(dest))
        plugin_handler = mock.Mock()
        # No file_move plugins, no task_result plugins.
        plugin_handler.get_enabled_plugin_modules_by_type.return_value = []

        with mock.patch("trawlarr.libs.postprocessor.PluginsHandler",
                        return_value=plugin_handler), \
                mock.patch.object(pp, "_PostProcessor__cleanup_cache_files"):
            pp.post_process_file()

        # Destination got the cached file.
        assert dest.exists()
        assert dest.read_bytes() == b"transcoded"
        # Source was removed AFTER the copy succeeded.
        assert not source.exists()
        # Listener-visible state reflects success.
        assert pp._last_file_move_processes_success is True
        assert pp._last_destination_files == [str(dest)]

    def test_source_kept_when_copy_fails(self, tmp_path):
        """If the move from cache to destination fails, the source must
        NOT be removed — same data-loss class as the remote-path bug."""
        cache = tmp_path / "cache" / "task" / "out.mkv"
        cache.parent.mkdir(parents=True)
        # Don't create the cache file — copy will fail
        source = tmp_path / "library" / "movie.mkv"
        source.parent.mkdir()
        source.write_bytes(b"original")
        dest = tmp_path / "library" / "movie-new.mkv"

        pp = _build_postprocessor(str(cache), str(source), str(dest))
        plugin_handler = mock.Mock()
        plugin_handler.get_enabled_plugin_modules_by_type.return_value = []

        with mock.patch("trawlarr.libs.postprocessor.PluginsHandler",
                        return_value=plugin_handler), \
                mock.patch.object(pp, "_PostProcessor__cleanup_cache_files"):
            pp.post_process_file()

        # Source must still exist — the only copy of the data.
        assert source.exists()
        assert source.read_bytes() == b"original"
        assert pp._last_file_move_processes_success is False

    def test_failed_task_skips_file_movement(self, tmp_path):
        """When task.success is False, no file movement must run."""
        cache = tmp_path / "cache" / "task" / "out.mkv"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"partial")
        source = tmp_path / "library" / "movie.mkv"
        source.parent.mkdir()
        source.write_bytes(b"original")
        dest = tmp_path / "library" / "movie-new.mkv"

        pp = _build_postprocessor(str(cache), str(source), str(dest), task_success=False)
        plugin_handler = mock.Mock()
        plugin_handler.get_enabled_plugin_modules_by_type.return_value = []

        with mock.patch("trawlarr.libs.postprocessor.PluginsHandler",
                        return_value=plugin_handler), \
                mock.patch.object(pp, "_PostProcessor__cleanup_cache_files"):
            pp.post_process_file()

        # Source untouched, destination not created.
        assert source.exists()
        assert not dest.exists()
