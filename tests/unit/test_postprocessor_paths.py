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
    triggered after a successful transcode, plus the task-owned .part
    replacement __copy_file uses to avoid leaving partial files at the
    destination.

    Invariants covered:

    1. __copy_file uses a task-owned .part suffix during the move and
       atomically replaces the final destination path.
    2. A failed final replacement retains the existing destination and
       source, and the complete part is recoverable.
    3. __copy_file refuses to copy when src and dst resolve to the
       same file (avoids data loss from same-file races).
    4. __copy_file appends to destination_files only on success.
    5. post_process_file removes the source ONLY after the cache file
       has been successfully copied to the destination.
    6. post_process_file keeps the source when the copy fails.
    7. post_process_file skips file movement entirely for failed tasks.
"""
import json
import logging
import os
import threading
from unittest import mock

import pytest
from peewee import SqliteDatabase
from playhouse.sqliteq import ResultTimeout

from unmanic.libs.postprocessor import PostProcessor
from unmanic.libs.task import Task
from unmanic.libs.unmodels import TaskLifecycle, TaskMetadata, Tasks


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
        """The move/copy lands in a task-owned part before replacement."""
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
        assert not list(tmp_path.glob("dest.mkv.unmanic.*.part"))
        # On move, source must be gone.
        assert not src.exists()
        assert destination_files == [str(dest)]

    def test_failed_final_replace_keeps_original_and_recovers_exact_part(
            self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"new")
        dest = tmp_path / "dest.mkv"
        dest.write_bytes(b"old")
        pp = _build_postprocessor(str(src), str(src), str(dest))
        destination_files = []
        real_replace = os.replace

        def fail_final_replace(source, destination):
            if destination == str(dest):
                raise OSError("final replace failed")
            return real_replace(source, destination)

        with mock.patch(
                "unmanic.libs.postprocessor.os.replace",
                side_effect=fail_final_replace):
            assert pp._PostProcessor__copy_file(
                str(src), str(dest), destination_files, "test_plugin",
                move=True) is False

        assert dest.read_bytes() == b"old"
        assert src.read_bytes() == b"new"
        parts = list(tmp_path.glob("dest.mkv.unmanic.*.part"))
        assert len(parts) == 1
        assert parts[0].read_bytes() == b"new"
        assert destination_files == []

        assert pp._PostProcessor__copy_file(
            str(src), str(dest), destination_files, "test_plugin",
            move=True) is True
        assert dest.read_bytes() == b"new"
        assert not src.exists()
        assert not parts[0].exists()
        assert destination_files == [str(dest)]

    def test_incomplete_copy_is_never_recovered_over_original(
            self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"complete-new-file")
        dest = tmp_path / "dest.mkv"
        dest.write_bytes(b"old")
        pp = _build_postprocessor(str(src), str(src), str(dest))
        part = pp._task_part_path(
            str(src), str(dest), plugin_id="test_plugin", move=False)
        copying = '{}.copying'.format(part)
        with open(copying, "wb") as incomplete:
            incomplete.write(b"partial")

        with mock.patch(
                "unmanic.libs.postprocessor.shutil.copyfile",
                side_effect=OSError("copy failed")):
            assert pp._PostProcessor__copy_file(
                str(src), str(dest), [], "test_plugin",
                move=False) is False

        assert dest.read_bytes() == b"old"
        assert src.read_bytes() == b"complete-new-file"
        assert not os.path.exists(part)

    def test_cross_filesystem_move_fallback_preserves_mode_and_source_until_replace(
            self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"new")
        src.chmod(0o640)
        dest = tmp_path / "dest.mkv"
        dest.write_bytes(b"old")
        pp = _build_postprocessor(str(src), str(src), str(dest))

        with mock.patch(
                "unmanic.libs.postprocessor.os.link",
                side_effect=OSError("cross-device link")):
            assert pp._PostProcessor__copy_file(
                str(src), str(dest), [], "test_plugin", move=True) is True

        assert dest.read_bytes() == b"new"
        assert dest.stat().st_mode & 0o777 == 0o640
        assert not src.exists()

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

        with mock.patch("unmanic.libs.postprocessor.PluginsHandler",
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

        with mock.patch("unmanic.libs.postprocessor.PluginsHandler",
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

        with mock.patch("unmanic.libs.postprocessor.PluginsHandler",
                        return_value=plugin_handler), \
                mock.patch.object(pp, "_PostProcessor__cleanup_cache_files"):
            pp.post_process_file()

        # Source untouched, destination not created.
        assert source.exists()
        assert not dest.exists()


@pytest.mark.parametrize(
    ("history_persisted", "expected_deleted"),
    [(False, False), (True, True)],
)
def test_run_deletes_local_task_only_after_history_persists(history_persisted, expected_deleted):
    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger("test.postprocessor.history")
    pp.abort_flag = threading.Event()
    pp._last_destination_files = []
    pp._last_file_move_processes_success = True

    wait_count = 0

    def stop_after_task(_timeout):
        nonlocal wait_count
        wait_count += 1
        if wait_count == 2:
            pp.abort_flag.set()

    pp.event = mock.Mock()
    pp.event.wait.side_effect = stop_after_task
    pp.system_configuration_is_valid = mock.Mock(return_value=True)
    pp.post_process_file = mock.Mock()
    pp.write_history_log = mock.Mock(return_value=history_persisted)
    pp.commit_task_metadata = mock.Mock()

    current_task = mock.Mock()
    current_task.task.success = True
    current_task.get_task_type.return_value = "local"
    current_task.get_task_success.return_value = True
    current_task.get_source_abspath.return_value = "/library/movie.mkv"
    current_task.save_completion_checkpoint.side_effect = (
        lambda *_args: setattr(current_task.task, "status", "history_pending")
    )
    pp.task_queue = mock.Mock()
    pp.task_queue.task_list_processed_is_empty.return_value = False
    pp.task_queue.get_next_processed_tasks.return_value = current_task

    with mock.patch("unmanic.libs.postprocessor.PluginsHandler"):
        pp.run()

    pp.write_history_log.assert_called_once_with()
    lifecycle_calls = [
        call[0] for call in current_task.method_calls
        if call[0] in ("save_completion_checkpoint", "set_status")
    ]
    assert lifecycle_calls[0] == "save_completion_checkpoint"
    assert current_task.set_status.call_args_list == [
        *([mock.call("completion_dispatching")] if history_persisted else []),
        *([mock.call("deletion_pending")] if history_persisted else []),
    ]
    assert current_task.delete.called is expected_deleted


def test_history_retry_does_not_repeat_file_postprocessing():
    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger("test.postprocessor.history.retry")
    pp.abort_flag = threading.Event()
    pp._last_destination_files = []
    pp._last_file_move_processes_success = True
    pp._history_retry_not_before = 0
    pp.HISTORY_RETRY_DELAY_SECONDS = 0

    wait_count = 0

    def stop_after_retry(_timeout):
        nonlocal wait_count
        wait_count += 1
        if wait_count == 5:
            pp.abort_flag.set()

    pp.event = mock.Mock()
    pp.event.wait.side_effect = stop_after_retry
    pp.system_configuration_is_valid = mock.Mock(return_value=True)
    pp.post_process_file = mock.Mock()
    pp.commit_task_metadata = mock.Mock()
    pp.write_history_log = mock.Mock(side_effect=[False, True])

    current_task = mock.Mock()
    current_task.task.status = "processed"
    current_task.task.success = True
    current_task.get_task_type.return_value = "local"
    current_task.get_task_success.return_value = True
    current_task.get_source_abspath.return_value = "/library/movie.mkv"
    current_task.set_status.side_effect = (
        lambda status: setattr(current_task.task, "status", status)
    )
    current_task.save_completion_checkpoint.side_effect = (
        lambda *_args: setattr(current_task.task, "status", "history_pending")
    )
    deleted = False

    def delete_task():
        nonlocal deleted
        deleted = True

    current_task.delete.side_effect = delete_task
    pp.task_queue = mock.Mock()
    pp.task_queue.task_list_processed_is_empty.side_effect = (
        lambda: current_task.task.status != "processed"
    )
    pp.task_queue.get_next_processed_tasks.return_value = current_task
    pp.task_queue.task_list_history_pending_is_empty.side_effect = (
        lambda: deleted or current_task.task.status != "history_pending"
    )
    pp.task_queue.get_next_history_pending_task.return_value = current_task

    with mock.patch("unmanic.libs.postprocessor.PluginsHandler"):
        pp.run()

    pp.post_process_file.assert_called_once_with()
    pp.commit_task_metadata.assert_called_once_with()
    assert pp.write_history_log.call_count == 2
    current_task.delete.assert_called_once_with()


def test_deletion_retry_does_not_repeat_history_or_completion_side_effects():
    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger("test.postprocessor.deletion.retry")
    pp.current_task = mock.Mock()
    pp.current_task.task.status = "history_pending"
    pp.current_task.set_status.side_effect = (
        lambda status: setattr(pp.current_task.task, "status", status)
    )
    pp.current_task.delete.side_effect = [RuntimeError("database busy"), None]
    pp.write_history_log = mock.Mock(return_value=True)
    pp._dispatch_completion_side_effects = mock.Mock()

    assert pp._persist_local_history_and_remove_task() is False
    assert pp.current_task.task.status == "deletion_pending"

    restarted = PostProcessor.__new__(PostProcessor)
    restarted.logger = logging.getLogger("test.postprocessor.deletion.restart")
    restarted.current_task = pp.current_task
    restarted.write_history_log = mock.Mock()

    assert restarted._remove_completed_local_task() is True
    pp.write_history_log.assert_called_once_with()
    pp._dispatch_completion_side_effects.assert_called_once_with()
    restarted.write_history_log.assert_not_called()
    assert pp.current_task.delete.call_count == 2


def test_successful_history_emits_completion_side_effects_once_before_deletion_retry():
    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger("test.postprocessor.completion.once")
    pp._last_destination_files = ["/library/movie-new.mkv"]
    pp._last_file_move_processes_success = True
    pp._log_completed_task_data = mock.Mock()
    pp.current_task = mock.Mock()
    pp.current_task.task.status = "history_pending"
    pp.current_task.task.success = False
    pp.current_task.task_dump.return_value = {
        "task_label": "movie.mkv",
        "abspath": "/library/movie.mkv",
        "task_success": False,
        "start_time": 1,
        "finish_time": 2,
        "processed_by_worker": "worker",
        "failure_category": "processing_failed",
        "failure_message": "failed",
        "failure_time": 2,
        "log": "",
    }
    pp.current_task.get_task_id.return_value = 42
    pp.current_task.get_task_library_id.return_value = 1
    pp.current_task.get_task_type.return_value = "local"
    pp.current_task.get_source_data.return_value = {
        "abspath": "/library/movie.mkv",
        "basename": "movie.mkv",
    }
    pp.current_task.get_destination_data.return_value = {
        "abspath": "/library/movie-new.mkv",
        "basename": "movie-new.mkv",
    }
    pp.current_task.set_status.side_effect = (
        lambda status: setattr(pp.current_task.task, "status", status)
    )
    pp.current_task.delete.side_effect = [RuntimeError("database busy"), None]

    history_logger = mock.Mock()
    history_logger.save_task_history.return_value = True
    notifications = mock.Mock()
    plugin_handler = mock.Mock()
    with mock.patch("unmanic.libs.postprocessor.history.History", return_value=history_logger), \
            mock.patch("unmanic.libs.postprocessor.Notifications", return_value=notifications), \
            mock.patch("unmanic.libs.postprocessor.PluginsHandler", return_value=plugin_handler):
        assert pp._persist_local_history_and_remove_task() is False
        assert pp.current_task.task.status == "deletion_pending"
        assert pp._remove_completed_local_task() is True

    history_logger.save_task_history.assert_called_once()
    notifications.add.assert_called_once()
    plugin_handler.run_event_plugins_for_plugin_type.assert_called_once()


def test_write_history_log_reports_persistence_failure():
    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger("test.postprocessor.history.write")
    pp._last_destination_files = []
    pp._last_file_move_processes_success = False
    pp._log_completed_task_data = mock.Mock()
    pp.current_task = mock.Mock()
    pp.current_task.task.success = True
    pp.current_task.task_dump.return_value = {
        "task_label": "movie.mkv",
        "abspath": "/library/movie.mkv",
        "task_success": True,
        "start_time": 1,
        "finish_time": 2,
        "processed_by_worker": "worker",
        "log": "",
    }

    history_logger = mock.Mock()
    history_logger.save_task_history.return_value = False
    plugin_handler = mock.Mock()
    with mock.patch("unmanic.libs.postprocessor.history.History", return_value=history_logger), \
            mock.patch("unmanic.libs.postprocessor.PluginsHandler", return_value=plugin_handler):
        result = pp.write_history_log()

    assert result is False
    history_logger.save_task_history.assert_called_once()
    pp._log_completed_task_data.assert_not_called()
    plugin_handler.run_event_plugins_for_plugin_type.assert_not_called()


def test_restart_after_dispatch_claim_skips_external_effects():
    database = SqliteDatabase(":memory:", pragmas={"foreign_keys": 1})
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath="/library/crash-window.mkv",
            cache_path="/cache/crash-window.mkv",
            status="history_pending",
        )
        current_task = Task.__new__(Task)
        current_task.task = record

        pp = PostProcessor.__new__(PostProcessor)
        pp.logger = logging.getLogger("test.postprocessor.completion.claim")
        pp.current_task = current_task
        pp.write_history_log = mock.Mock(return_value=True)
        pp._dispatch_completion_side_effects = mock.Mock(
            side_effect=SystemExit("crash after durable claim"))

        with pytest.raises(SystemExit):
            pp._persist_local_history_and_remove_task()

        assert Tasks.get_by_id(record.id).status == "completion_dispatching"
        pp.write_history_log.assert_called_once_with()

        recovered_task = Task.__new__(Task)
        recovered_task.task = Tasks.get_by_id(record.id)
        restarted = PostProcessor.__new__(PostProcessor)
        restarted.logger = logging.getLogger(
            "test.postprocessor.completion.claim.restart")
        restarted.current_task = recovered_task
        restarted.write_history_log = mock.Mock()
        restarted._dispatch_completion_side_effects = mock.Mock()

        assert restarted._finish_claimed_completion() is True
        restarted.write_history_log.assert_not_called()
        restarted._dispatch_completion_side_effects.assert_not_called()
        assert not Tasks.select().where(Tasks.id == record.id).exists()


def test_restart_after_effects_before_deletion_marker_does_not_redispatch():
    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger("test.postprocessor.completion.effect-window")
    pp.current_task = mock.Mock()
    pp.current_task.task.status = "history_pending"
    statuses = []

    def set_status(status):
        statuses.append(status)
        if status == "deletion_pending":
            raise RuntimeError("database unavailable")
        pp.current_task.task.status = status

    pp.current_task.set_status.side_effect = set_status
    pp.write_history_log = mock.Mock(return_value=True)
    pp._dispatch_completion_side_effects = mock.Mock()

    assert pp._persist_local_history_and_remove_task() is False
    assert statuses == ["completion_dispatching", "deletion_pending"]
    assert pp.current_task.task.status == "completion_dispatching"
    pp._dispatch_completion_side_effects.assert_called_once_with()

    restarted = PostProcessor.__new__(PostProcessor)
    restarted.logger = logging.getLogger("test.postprocessor.completion.effect-window.restart")
    restarted.current_task = pp.current_task
    restarted._dispatch_completion_side_effects = mock.Mock()
    pp.current_task.set_status.side_effect = (
        lambda status: setattr(pp.current_task.task, "status", status)
    )

    assert restarted._finish_claimed_completion() is True
    restarted._dispatch_completion_side_effects.assert_not_called()
    pp.current_task.delete.assert_called_once_with()


def test_completion_payload_is_durable_and_legacy_rows_have_safe_defaults():
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath="/library/movie.mkv",
            cache_path="/cache/movie-new.mkv",
            status="processed",
        )
        current_task = Task.__new__(Task)
        current_task.task = record

        assert current_task.get_completion_payload() == {
            "destination_files": [],
            "file_move_processes_success": False,
        }

        current_task.save_completion_payload(
            ["/library/movie-new.mkv"], True)

        recovered = Task.__new__(Task)
        recovered.task = Tasks.get_by_id(record.id)
        assert recovered.get_completion_payload() == {
            "destination_files": ["/library/movie-new.mkv"],
            "file_move_processes_success": True,
        }
        postprocessor = PostProcessor.__new__(PostProcessor)
        postprocessor.logger = logging.getLogger(
            "test.postprocessor.completion.payload-recovery")
        postprocessor.current_task = recovered
        postprocessor._last_destination_files = []
        postprocessor._last_file_move_processes_success = False
        postprocessor._load_completion_payload()
        assert postprocessor._last_destination_files == ["/library/movie-new.mkv"]
        assert postprocessor._last_file_move_processes_success is True
        persisted = json.loads(TaskLifecycle.get().json_blob)
        assert persisted["completion"] == {
            "version": 1,
            "destination_files": ["/library/movie-new.mkv"],
            "file_move_processes_success": True,
        }


def test_completion_checkpoint_is_atomic_on_transactional_database():
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath="/library/atomic.mkv",
            cache_path="/cache/atomic.mkv",
            status="checkpoint_pending",
        )
        current_task = Task.__new__(Task)
        current_task.task = record

        with mock.patch.object(
                Tasks, "update", side_effect=RuntimeError("phase write failed")):
            with pytest.raises(RuntimeError, match="phase write failed"):
                current_task.save_completion_checkpoint(
                    ["/library/atomic-new.mkv"], True)

        assert Tasks.get_by_id(record.id).status == "checkpoint_pending"
        assert TaskMetadata.select().count() == 0


def test_queued_checkpoint_writes_payload_before_phase(monkeypatch):
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath="/library/queued-window.mkv",
            cache_path="/cache/queued-window.mkv",
            status="checkpoint_pending",
        )
        current_task = Task.__new__(Task)
        current_task.task = record
        monkeypatch.setattr("unmanic.libs.task.SqliteQueueDatabase", SqliteDatabase)

        with mock.patch.object(
                current_task, "transition_status",
                side_effect=RuntimeError("phase write failed")):
            with pytest.raises(RuntimeError, match="phase write failed"):
                current_task.save_completion_checkpoint(
                    ["/library/queued-window-new.mkv"], True)

        assert Tasks.get_by_id(record.id).status == "checkpoint_pending"
        assert current_task.has_valid_completion_payload() is True


def _run_to_timed_out_checkpoint(record):
    current_task = Task.__new__(Task)
    current_task.task = record
    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger("test.postprocessor.checkpoint-timeout")
    pp.abort_flag = threading.Event()
    pp._history_retry_not_before = 0
    pp._checkpoint_quarantine = {}
    pp._last_destination_files = ["/library/checkpoint-new.mkv"]
    pp._last_file_move_processes_success = True
    pp.event = mock.Mock()
    wait_count = 0

    def stop_after_checkpoint(_timeout):
        nonlocal wait_count
        wait_count += 1
        if wait_count == 2:
            pp.abort_flag.set()

    pp.event.wait.side_effect = stop_after_checkpoint
    pp.system_configuration_is_valid = mock.Mock(return_value=True)

    def assert_durable_file_operation_claim():
        assert Tasks.get_by_id(record.id).status == "checkpoint_pending"

    pp.post_process_file = mock.Mock(side_effect=assert_durable_file_operation_claim)
    pp.commit_task_metadata = mock.Mock()
    pp.write_history_log = mock.Mock()
    pp.task_queue = mock.Mock()
    pp.task_queue.task_list_checkpoint_pending_is_empty.return_value = True
    pp.task_queue.task_list_postprocessing_is_empty.return_value = True
    pp.task_queue.task_list_processed_is_empty.return_value = False
    pp.task_queue.get_next_processed_tasks.return_value = current_task

    with mock.patch("unmanic.libs.postprocessor.PluginsHandler"), \
            mock.patch.object(
                Task,
                "save_completion_checkpoint",
                side_effect=ResultTimeout("checkpoint timed out"),
            ):
        pp.run()

    assert Tasks.get_by_id(record.id).status == "checkpoint_pending"
    assert record.id in pp._checkpoint_quarantine
    assert pp._get_next_postprocessor_task() is None
    pp.post_process_file.assert_called_once_with()
    pp.commit_task_metadata.assert_called_once_with()
    pp.write_history_log.assert_not_called()
    return pp


def test_checkpoint_timeout_then_late_commit_never_repeats_file_operations():
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath="/library/checkpoint-late.mkv",
            cache_path="/cache/checkpoint-late.mkv",
            status="processed",
            success=True,
        )

        pp = _run_to_timed_out_checkpoint(record)

        late_task = Task.__new__(Task)
        late_task.task = Tasks.get_by_id(record.id)
        late_task.save_completion_checkpoint(
            ["/library/checkpoint-new.mkv"], True)
        pp._checkpoint_quarantine[record.id]["not_before"] = 0
        pp._history_retry_not_before = 0
        pp._reconcile_checkpoint_quarantine()

        assert record.id not in pp._checkpoint_quarantine
        assert Tasks.get_by_id(record.id).status == "history_pending"
        assert late_task.has_valid_completion_payload() is True
        pp.post_process_file.assert_called_once_with()


def test_checkpoint_timeout_without_commit_retries_payload_not_file_operations():
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath="/library/checkpoint-retry.mkv",
            cache_path="/cache/checkpoint-retry.mkv",
            status="processed",
            success=True,
        )

        pp = _run_to_timed_out_checkpoint(record)
        pp._checkpoint_quarantine[record.id]["not_before"] = 0
        pp._history_retry_not_before = 0
        pp._reconcile_checkpoint_quarantine()

        recovered = Task.__new__(Task)
        recovered.task = Tasks.get_by_id(record.id)
        assert record.id not in pp._checkpoint_quarantine
        assert recovered.task.status == "history_pending"
        assert recovered.get_completion_payload() == {
            "destination_files": ["/library/checkpoint-new.mkv"],
            "file_move_processes_success": True,
        }
        pp.post_process_file.assert_called_once_with()


def test_restart_quarantines_uncheckpointed_file_operations_without_repeating_them():
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath="/library/checkpoint-restart.mkv",
            cache_path="/cache/checkpoint-restart.mkv",
            status="checkpoint_pending",
            success=True,
        )
        recovered = Task.__new__(Task)
        recovered.task = record
        pp = PostProcessor.__new__(PostProcessor)
        pp.logger = logging.getLogger("test.postprocessor.checkpoint-restart")
        pp.current_task = recovered
        pp._checkpoint_quarantine = {}
        pp.post_process_file = mock.Mock()

        assert pp._recover_interrupted_postprocessing(
            "checkpoint_pending") is True

        persisted = Tasks.get_by_id(record.id)
        recovered.task = persisted
        assert persisted.status == "history_pending"
        assert persisted.success is False
        assert persisted.failure_category == "internal_error"
        assert recovered.has_valid_completion_payload() is True
        pp.post_process_file.assert_not_called()


def test_restart_recovers_exact_task_owned_complete_part(tmp_path):
    database = SqliteDatabase(":memory:")
    cache_path = tmp_path / "cache.mkv"
    cache_path.write_bytes(b"new")
    destination = tmp_path / "library.mkv"
    destination.write_bytes(b"old")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath=str(destination),
            cache_path=str(cache_path),
            status="checkpoint_pending",
            success=True,
        )
        recovered = Task.__new__(Task)
        recovered.task = record
        pp = PostProcessor.__new__(PostProcessor)
        pp.logger = logging.getLogger("test.postprocessor.part-restart")
        pp.current_task = recovered
        pp._checkpoint_quarantine = {}
        part = pp._task_part_path(str(cache_path), str(destination))
        with open(part, "wb") as staged:
            staged.write(b"new")

        assert pp._recover_interrupted_postprocessing(
            "checkpoint_pending") is True

        recovered.task = Tasks.get_by_id(record.id)
        assert destination.read_bytes() == b"new"
        assert not cache_path.exists()
        assert not os.path.exists(part)
        assert recovered.task.status == "history_pending"
        assert recovered.get_completion_payload() == {
            "destination_files": [str(destination)],
            "file_move_processes_success": True,
        }


def test_processed_recovery_with_valid_payload_skips_file_operations():
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath="/library/crash-window.mkv",
            cache_path="/cache/crash-window.mkv",
            status="processed",
        )
        current_task = Task.__new__(Task)
        current_task.task = record
        current_task.save_completion_payload(
            ["/library/crash-window-new.mkv"], True)

        pp = PostProcessor.__new__(PostProcessor)
        pp.logger = logging.getLogger("test.postprocessor.payload-window")
        pp.abort_flag = threading.Event()
        pp._history_retry_not_before = 0
        pp._last_destination_files = []
        pp._last_file_move_processes_success = False
        pp.event = mock.Mock()
        wait_count = 0

        def stop_after_selection(_timeout):
            nonlocal wait_count
            wait_count += 1
            if wait_count == 2:
                pp.abort_flag.set()

        pp.event.wait.side_effect = stop_after_selection
        pp.system_configuration_is_valid = mock.Mock(return_value=True)
        pp.post_process_file = mock.Mock()
        pp.commit_task_metadata = mock.Mock()
        pp.write_history_log = mock.Mock(return_value=True)
        pp._dispatch_completion_side_effects = mock.Mock()
        pp.task_queue = mock.Mock()
        pp.task_queue.task_list_processed_is_empty.return_value = False
        pp.task_queue.get_next_processed_tasks.return_value = current_task

        with mock.patch("unmanic.libs.postprocessor.PluginsHandler"):
            pp.run()

        pp.post_process_file.assert_not_called()
        pp.commit_task_metadata.assert_not_called()
        pp.write_history_log.assert_called_once_with()
        assert pp._last_destination_files == ["/library/crash-window-new.mkv"]
        assert pp._last_file_move_processes_success is True
        assert not Tasks.select().where(Tasks.id == record.id).exists()


def test_malformed_completion_payload_does_not_skip_postprocessing():
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath="/library/partial-window.mkv",
            cache_path="/cache/partial-window.mkv",
            status="processed",
        )
        TaskMetadata.create(
            task=record.id,
            json_blob=json.dumps({
                "__meta__": {
                    "completion": {
                        "version": 1,
                        "destination_files": "not-a-list",
                    },
                },
            }),
        )
        current_task = Task.__new__(Task)
        current_task.task = record

        assert current_task.has_valid_completion_payload() is False

        pp = PostProcessor.__new__(PostProcessor)
        pp.logger = logging.getLogger("test.postprocessor.partial-payload")
        pp.abort_flag = threading.Event()
        pp._history_retry_not_before = 0
        pp._last_destination_files = []
        pp._last_file_move_processes_success = False
        pp.event = mock.Mock()
        wait_count = 0

        def stop_after_selection(_timeout):
            nonlocal wait_count
            wait_count += 1
            if wait_count == 2:
                pp.abort_flag.set()

        pp.event.wait.side_effect = stop_after_selection
        pp.system_configuration_is_valid = mock.Mock(return_value=True)
        pp.post_process_file = mock.Mock()
        pp.commit_task_metadata = mock.Mock()
        pp.write_history_log = mock.Mock(return_value=True)
        pp._dispatch_completion_side_effects = mock.Mock()
        pp.task_queue = mock.Mock()
        pp.task_queue.task_list_processed_is_empty.return_value = False
        pp.task_queue.get_next_processed_tasks.return_value = current_task

        with mock.patch("unmanic.libs.postprocessor.PluginsHandler"):
            pp.run()

        pp.post_process_file.assert_called_once_with()
        pp.commit_task_metadata.assert_called_once_with()
