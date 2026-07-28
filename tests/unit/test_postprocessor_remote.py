#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    test_postprocessor_remote.py

    Tests for PostProcessor.post_process_remote_file — locks in the
    invariant that the remote-source file is never removed unless the
    processed file has been delivered to its destination.
"""
import asyncio
import json
import logging
import os
from unittest import mock

import pytest
from peewee import SqliteDatabase
from playhouse.sqliteq import ResultTimeout

from unmanic.libs.postprocessor import PostProcessor
from unmanic.libs.task import Task
from unmanic.libs.unmodels.tasklifecycle import TaskLifecycle
from unmanic.libs.unmodels.taskmetadata import TaskMetadata
from unmanic.libs.unmodels.tasks import Tasks
from unmanic.webserver.api_v2.pending_api import ApiPendingHandler


def _build_postprocessor(cache_root, dest_path, source_abspath, cache_path):
    """Build a PostProcessor with just enough state to drive the remote path."""
    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger("test.postprocessor")
    pp._last_destination_files = []
    pp._last_file_move_processes_success = False

    class _Settings:
        def get_cache_path(self):
            return str(cache_root)

    class _Task:
        def get_task_id(self_inner):
            return 1

        def get_cache_path(self_inner):
            return cache_path

        def get_source_data(self_inner):
            return {"abspath": source_abspath}

        def get_destination_data(self_inner):
            return {"abspath": dest_path}

        def modify_path(self_inner, new_path):
            self_inner.modified_to = new_path

    pp.settings = _Settings()
    pp.current_task = _Task()
    return pp


def _disable_cleanup(pp):
    """Cache cleanup unlinks tmp dirs we want to inspect — neutralise it."""
    pp._PostProcessor__cleanup_cache_files = lambda _path: None


def _durable_postprocessor(cache_root, current_task):
    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger("test.postprocessor.remote.durable")
    pp.current_task = current_task
    pp._last_destination_files = []
    pp._last_file_move_processes_success = False
    pp._remote_delivery_quarantine = {}
    pp._checkpoint_quarantine = {}
    pp._history_retry_not_before = 0

    class _Settings:
        def get_cache_path(self):
            return str(cache_root)

    pp.settings = _Settings()
    return pp


def _task_for_record(record):
    current_task = Task.__new__(Task)
    current_task.task = record
    current_task.errors = []
    return current_task


def _checkpoint_shared_staging(database, tmp_path, suffix):
    cache_root = tmp_path / "cache"
    library = tmp_path / "library"
    cache_root.mkdir()
    library.mkdir()
    source = library / "video.mkv"
    source.write_bytes(b"original")
    cache_file = cache_root / "conversion" / "video.mp4"
    cache_file.parent.mkdir()
    cache_file.write_bytes(b"processed")
    record = Tasks.create(
        abspath=str(source),
        cache_path=str(cache_file),
        type="remote",
        status="processed",
        success=True,
    )
    current_task = _task_for_record(record)
    pp = _durable_postprocessor(cache_root, current_task)
    claim = pp._build_remote_delivery_claim()
    current_task.save_remote_delivery_claim(claim)
    result = pp.post_process_remote_file(claim)
    checkpoint = pp._build_remote_delivery_checkpoint(
        claim, result["output_path"], result["delivery_success"])
    current_task.save_remote_delivery_checkpoint(checkpoint)
    current_task.set_status("complete")
    return cache_root, library, source, current_task, checkpoint


class TestPostProcessRemoteFile:

    def test_source_retained_when_copy_to_cache_destination_fails(self, tmp_path):
        """Headline case: cache-internal handoff that fails to copy must
        not delete the source. Previously the source was removed first."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()
        source = tmp_path / "source.mkv"
        source.write_bytes(b"original")
        cache_file = cache_root / "task-cache" / "processed.mkv"
        cache_file.parent.mkdir()
        cache_file.write_bytes(b"processed")
        # Destination is inside the cache root, so remove_source_file is True.
        destination = cache_root / "destination" / "out.mkv"

        pp = _build_postprocessor(cache_root, str(destination), str(source), str(cache_file))
        _disable_cleanup(pp)
        # Force the copy to fail.
        pp._PostProcessor__copy_file = lambda *args, **kwargs: False

        pp.post_process_remote_file()

        assert source.exists(), "source must be retained when delivery fails"
        assert pp._last_file_move_processes_success is False
        assert pp._last_destination_files == []

    def test_source_is_retained_after_successful_checkpointable_copy(
            self, tmp_path):
        cache_root = tmp_path / "cache"
        cache_root.mkdir()
        source = (
            cache_root / "remote_library"
            / "unmanic_remote_pending_library-request" / "source.mkv")
        source.parent.mkdir(parents=True)
        source.write_bytes(b"original")
        cache_file = cache_root / "task-cache" / "processed.mkv"
        cache_file.parent.mkdir()
        cache_file.write_bytes(b"processed")
        destination = cache_root / "destination" / "out.mkv"

        pp = _build_postprocessor(cache_root, str(destination), str(source), str(cache_file))
        _disable_cleanup(pp)
        def atomic_delivery(file_in, file_out, *_args, **_kwargs):
            os.makedirs(os.path.dirname(file_out), exist_ok=True)
            os.replace(file_in, file_out)
            return True

        pp._PostProcessor__copy_file = atomic_delivery

        pp.post_process_remote_file()

        assert source.read_bytes() == b"original"
        assert pp._last_file_move_processes_success is True
        assert pp._last_destination_files == [str(destination)]

    def test_same_extension_destination_preserves_delivered_result(self, tmp_path):
        cache_root = tmp_path / "cache"
        source = (
            cache_root / "remote_library"
            / "unmanic_remote_pending_library-request" / "video.mkv")
        source.parent.mkdir(parents=True)
        source.write_bytes(b"original")
        cache_file = cache_root / "task-cache" / "video.mkv"
        cache_file.parent.mkdir()
        cache_file.write_bytes(b"processed")

        pp = _build_postprocessor(
            cache_root, str(source), str(source), str(cache_file))
        _disable_cleanup(pp)

        def atomic_delivery(file_in, file_out, *_args, **_kwargs):
            os.replace(file_in, file_out)
            return True

        pp._PostProcessor__copy_file = atomic_delivery

        pp.post_process_remote_file()

        assert source.read_bytes() == b"processed"
        assert pp._last_file_move_processes_success is True
        assert pp._last_destination_files == [str(source)]

    def test_differing_extension_retains_source_until_retrieval_cleanup(
            self, tmp_path):
        cache_root = tmp_path / "cache"
        source = (
            cache_root / "remote_library"
            / "unmanic_remote_pending_library-request" / "video.mkv")
        source.parent.mkdir(parents=True)
        source.write_bytes(b"original")
        cache_file = cache_root / "task-cache" / "video.mp4"
        cache_file.parent.mkdir()
        cache_file.write_bytes(b"processed")
        destination = source.with_suffix(".mp4")

        pp = _build_postprocessor(
            cache_root, str(destination), str(source), str(cache_file))
        _disable_cleanup(pp)

        def atomic_delivery(file_in, file_out, *_args, **_kwargs):
            os.replace(file_in, file_out)
            return True

        pp._PostProcessor__copy_file = atomic_delivery

        pp.post_process_remote_file()

        assert source.read_bytes() == b"original"
        assert destination.read_bytes() == b"processed"
        assert pp._last_destination_files == [str(destination)]

    def test_symlink_remote_source_is_never_deleted(self, tmp_path):
        cache_root = tmp_path / "cache"
        upload_directory = (
            cache_root / "remote_library"
            / "unmanic_remote_pending_library-request")
        upload_directory.mkdir(parents=True)
        outside = tmp_path / "outside.mkv"
        outside.write_bytes(b"outside")
        source = upload_directory / "video.mkv"
        source.symlink_to(outside)
        cache_file = cache_root / "task-cache" / "video.mp4"
        cache_file.parent.mkdir()
        cache_file.write_bytes(b"processed")
        destination = upload_directory / "video.mp4"

        pp = _build_postprocessor(
            cache_root, str(destination), str(source), str(cache_file))
        _disable_cleanup(pp)
        pp._PostProcessor__copy_file = lambda *_args, **_kwargs: True

        pp.post_process_remote_file()

        assert source.is_symlink()
        assert outside.read_bytes() == b"outside"

    def test_library_destination_keeps_source_regardless(self, tmp_path):
        """When the destination is in the library (not the cache), the source
        is always kept — copy success or failure does not change that."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()
        library = tmp_path / "library"
        library.mkdir()
        source = library / "source.mkv"
        source.write_bytes(b"original")
        cache_file = cache_root / "task-cache" / "processed.mkv"
        cache_file.parent.mkdir()
        cache_file.write_bytes(b"processed")
        destination = library / "out.mkv"  # outside cache_root

        pp = _build_postprocessor(cache_root, str(destination), str(source), str(cache_file))
        _disable_cleanup(pp)
        def atomic_delivery(file_in, file_out, *_args, **_kwargs):
            os.replace(file_in, file_out)
            return True

        pp._PostProcessor__copy_file = atomic_delivery

        pp.post_process_remote_file()

        assert source.exists(), "library source must always be retained"
        assert pp._last_file_move_processes_success is True
        assert pp._last_destination_files and pp._last_destination_files[0].endswith(
            "processed.mkv")

    def test_library_path_falls_back_to_cache_on_library_failure(self, tmp_path):
        """If the library staging dir copy fails, the function falls back to
        a cache-side staging dir and reports success."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()
        library = tmp_path / "library"
        library.mkdir()
        source = library / "source.mkv"
        source.write_bytes(b"original")
        cache_file = cache_root / "task-cache" / "processed.mkv"
        cache_file.parent.mkdir()
        cache_file.write_bytes(b"processed")
        destination = library / "out.mkv"

        pp = _build_postprocessor(cache_root, str(destination), str(source), str(cache_file))
        _disable_cleanup(pp)

        # First call (library-side) fails, subsequent calls succeed.
        calls = {"n": 0}

        def fake_copy(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return False
            os.replace(args[0], args[1])
            return True

        pp._PostProcessor__copy_file = fake_copy

        pp.post_process_remote_file()

        assert source.exists()
        assert pp._last_file_move_processes_success is True
        assert pp._last_destination_files
        assert "cache" in pp._last_destination_files[0]

    def test_both_staging_dirs_failing_reports_failure(self, tmp_path):
        cache_root = tmp_path / "cache"
        cache_root.mkdir()
        library = tmp_path / "library"
        library.mkdir()
        source = library / "source.mkv"
        source.write_bytes(b"original")
        cache_file = cache_root / "task-cache" / "processed.mkv"
        cache_file.parent.mkdir()
        cache_file.write_bytes(b"processed")
        destination = library / "out.mkv"

        pp = _build_postprocessor(cache_root, str(destination), str(source), str(cache_file))
        _disable_cleanup(pp)
        pp._PostProcessor__copy_file = lambda *args, **kwargs: False

        pp.post_process_remote_file()

        assert source.exists()
        assert pp._last_file_move_processes_success is False
        assert pp._last_destination_files == []

    def test_missing_cache_file_reports_failure(self, tmp_path):
        cache_root = tmp_path / "cache"
        cache_root.mkdir()
        source = tmp_path / "source.mkv"
        source.write_bytes(b"original")
        cache_file = cache_root / "task-cache" / "processed.mkv"  # never created
        destination = cache_root / "destination" / "out.mkv"

        pp = _build_postprocessor(cache_root, str(destination), str(source), str(cache_file))
        _disable_cleanup(pp)
        # Sentinel: __copy_file should never be invoked when cache is missing.
        pp._PostProcessor__copy_file = lambda *args, **kwargs: pytest.fail("__copy_file should not be called")

        pp.post_process_remote_file()

        assert source.exists()
        assert pp._last_file_move_processes_success is False
        assert pp._last_destination_files == []


def test_dump_history_exports_only_safe_failure_summary(tmp_path):
    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger("test.postprocessor.remote.dump")
    destination = tmp_path / "result.mkv"
    current_task = mock.Mock()
    current_task.get_task_id.return_value = 42
    current_task.get_destination_data.return_value = {"abspath": str(destination)}
    current_task.task_dump.return_value = {
        "task_label": "result.mkv",
        "abspath": str(destination),
        "task_success": False,
        "start_time": 10,
        "finish_time": 20,
        "processed_by_worker": "worker",
        "failure_category": "postprocessing_failed",
        "failure_message": "Safe summary\nwithout traceback details",
        "failure_time": 19,
        "errors": ["Traceback (most recent call last): secret-token"],
        "log": "worker output",
    }
    pp.current_task = current_task
    checkpoint = {
        "version": 1,
        "phase": "checkpointed",
        "source_path": str(destination),
        "cache_path": str(destination),
        "destination_path": str(destination),
        "target_paths": [str(destination)],
        "input_fingerprint": None,
        "retrieval_path": str(destination),
        "output_path": str(destination),
        "output_fingerprint": {"size": 1, "checksum": "checksum"},
        "delivery_success": False,
        "metadata_path": Task.remote_metadata_path(str(destination)),
        "task_success": False,
        "failure_category": "postprocessing_failed",
        "failure_message": "Safe summary without traceback details",
        "failure_time": 19,
        "task_state": {"plugin": {"value": True}},
    }

    pp.dump_history_log(checkpoint)

    payload = json.loads(
        (tmp_path / Task.REMOTE_METADATA_DIRECTORY / "data.json").read_text())
    assert payload["failure_category"] == "postprocessing_failed"
    assert payload["failure_message"] == "Safe summary without traceback details"
    assert payload["failure_time"] == 19
    assert "errors" not in payload


def test_remote_delivery_claim_precedes_move_and_complete_follows_metadata(
        tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    upload = (
        cache_root / Task.REMOTE_UPLOAD_DIRECTORY
        / (Task.REMOTE_UPLOAD_PREFIX + "claim"))
    upload.mkdir(parents=True)
    source = upload / "video.mkv"
    source.write_bytes(b"original")
    cache_file = (
        cache_root / "unmanic_file_conversion-claim" / "video-new.mkv")
    cache_file.parent.mkdir()
    cache_file.write_bytes(b"processed")

    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath=str(source),
            cache_path=str(cache_file),
            type="remote",
            status="processed",
            success=True,
        )
        current_task = _task_for_record(record)
        pp = _durable_postprocessor(cache_root, current_task)
        original_copy = pp._PostProcessor__copy_file

        def assert_claimed(*args, **kwargs):
            assert Tasks.get_by_id(record.id).status == "remote_delivery"
            return original_copy(*args, **kwargs)

        pp._PostProcessor__copy_file = assert_claimed
        with mock.patch("unmanic.libs.postprocessor.PluginsHandler"):
            assert pp._process_remote_delivery("processed") is True

        persisted = Tasks.get_by_id(record.id)
        assert persisted.status == "complete"
        assert persisted.abspath == str(source)
        assert source.read_bytes() == b"processed"
        metadata = upload / Task.REMOTE_METADATA_DIRECTORY / "data.json"
        assert metadata.is_file()
        checksum = PostProcessor._remote_file_fingerprint(
            str(source))["checksum"]
        assert json.loads(metadata.read_text())["checksum"] == checksum


def test_delete_after_shared_retrieval_removes_only_owned_staging(tmp_path):
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        cache_root, library, source, current_task, checkpoint = (
            _checkpoint_shared_staging(database, tmp_path, "cleanup"))
        staging_directory = checkpoint["staging_directory"]
        assert os.path.isdir(staging_directory)
        assert source.read_bytes() == b"original"

        deleter = Task()
        deleter.settings = mock.Mock()
        deleter.settings.get_cache_path.return_value = str(cache_root)

        assert deleter.delete_tasks_recursively(
            [current_task.get_task_id()],
            cleanup_remote_staging=True,
        ) is True

        assert not os.path.exists(staging_directory)
        assert source.read_bytes() == b"original"
        assert library.exists()


def test_delete_without_retrieval_ack_keeps_owned_staging(tmp_path):
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        cache_root, _library, source, current_task, checkpoint = (
            _checkpoint_shared_staging(database, tmp_path, "unacknowledged"))
        staging_directory = checkpoint["staging_directory"]
        deleter = Task()
        deleter.settings = mock.Mock()
        deleter.settings.get_cache_path.return_value = str(cache_root)

        assert deleter.delete_tasks_recursively(
            [current_task.get_task_id()]) is True

        assert os.path.isdir(staging_directory)
        assert source.read_bytes() == b"original"


def test_tampered_staging_checkpoint_refuses_arbitrary_cleanup(tmp_path):
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        cache_root, _library, source, current_task, checkpoint = (
            _checkpoint_shared_staging(database, tmp_path, "tampered"))
        real_staging = checkpoint["staging_directory"]
        arbitrary = tmp_path / "arbitrary"
        arbitrary.mkdir()
        protected = arbitrary / "keep.txt"
        protected.write_text("keep")

        lifecycle_row = TaskLifecycle.get()
        lifecycle = json.loads(lifecycle_row.json_blob)
        lifecycle["remote_delivery"]["staging_directory"] = str(arbitrary)
        lifecycle_row.json_blob = json.dumps(lifecycle)
        lifecycle_row.save()
        current_task.settings = mock.Mock()
        current_task.settings.get_cache_path.return_value = str(cache_root)

        assert current_task.cleanup_remote_delivery_staging() is False

        assert protected.read_text() == "keep"
        assert os.path.isdir(real_staging)
        assert source.read_bytes() == b"original"


def test_traversal_spelling_of_owned_staging_is_refused(tmp_path):
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        cache_root, _library, source, current_task, checkpoint = (
            _checkpoint_shared_staging(database, tmp_path, "traversal"))
        real_staging = checkpoint["staging_directory"]
        lifecycle_row = TaskLifecycle.get()
        lifecycle = json.loads(lifecycle_row.json_blob)
        lifecycle["remote_delivery"]["staging_directory"] = os.path.join(
            os.path.dirname(real_staging),
            "unused",
            "..",
            os.path.basename(real_staging),
        )
        lifecycle_row.json_blob = json.dumps(lifecycle)
        lifecycle_row.save()
        current_task.settings = mock.Mock()
        current_task.settings.get_cache_path.return_value = str(cache_root)

        assert current_task.cleanup_remote_delivery_staging() is False

        assert os.path.isdir(real_staging)
        assert source.read_bytes() == b"original"


def test_symlink_replacement_of_owned_staging_is_refused(tmp_path):
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        cache_root, _library, source, current_task, checkpoint = (
            _checkpoint_shared_staging(database, tmp_path, "symlink"))
        real_staging = checkpoint["staging_directory"]
        moved_staging = real_staging + "-moved"
        os.rename(real_staging, moved_staging)
        os.symlink(moved_staging, real_staging)
        current_task.settings = mock.Mock()
        current_task.settings.get_cache_path.return_value = str(cache_root)

        assert current_task.cleanup_remote_delivery_staging() is False

        assert os.path.islink(real_staging)
        assert os.path.isdir(moved_staging)
        assert source.read_bytes() == b"original"


def test_restart_after_delivery_recovers_checkpoint_without_moving_again(
        tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    upload = (
        cache_root / Task.REMOTE_UPLOAD_DIRECTORY
        / (Task.REMOTE_UPLOAD_PREFIX + "restart"))
    upload.mkdir(parents=True)
    source = upload / "video.mkv"
    source.write_bytes(b"original")
    cache_file = (
        cache_root / "unmanic_file_conversion-restart" / "video-new.mkv")
    cache_file.parent.mkdir()
    cache_file.write_bytes(b"processed")

    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath=str(source),
            cache_path=str(cache_file),
            type="remote",
            status="processed",
            success=True,
        )
        current_task = _task_for_record(record)
        pp = _durable_postprocessor(cache_root, current_task)
        claim = pp._build_remote_delivery_claim()
        current_task.save_remote_delivery_claim(claim)
        os.replace(cache_file, claim["target_paths"][0])

        recovered_task = _task_for_record(Tasks.get_by_id(record.id))
        restarted = _durable_postprocessor(cache_root, recovered_task)
        restarted.post_process_remote_file = mock.Mock(
            side_effect=AssertionError("delivery must not repeat"))

        assert restarted._recover_interrupted_remote_delivery() is True
        restarted.post_process_remote_file.assert_not_called()
        assert Tasks.get_by_id(record.id).status == "complete"
        assert source.read_bytes() == b"processed"


def test_interrupted_delivery_with_invalid_checkpoint_preserves_both_paths(
        tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    upload = (
        cache_root / Task.REMOTE_UPLOAD_DIRECTORY
        / (Task.REMOTE_UPLOAD_PREFIX + "invalid"))
    upload.mkdir(parents=True)
    source = upload / "video.mkv"
    source.write_bytes(b"legitimate-existing")
    cache_file = (
        cache_root / "unmanic_file_conversion-invalid" / "video-new.mkv")
    cache_file.parent.mkdir()
    cache_file.write_bytes(b"processed")

    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath=str(source),
            cache_path=str(cache_file),
            type="remote",
            status="remote_delivery",
            success=True,
        )
        current_task = _task_for_record(record)
        pp = _durable_postprocessor(cache_root, current_task)
        pp.post_process_remote_file = mock.Mock(
            side_effect=AssertionError("uncertain delivery must not run"))

        assert pp._recover_interrupted_remote_delivery() is True

        persisted = Tasks.get_by_id(record.id)
        assert persisted.status == "complete"
        assert persisted.success is False
        assert persisted.abspath == str(cache_file)
        assert source.read_bytes() == b"legitimate-existing"
        assert cache_file.read_bytes() == b"processed"
        pp.post_process_remote_file.assert_not_called()


def test_checkpoint_timeout_late_commit_never_repeats_confirmed_delivery(
        tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    upload = (
        cache_root / Task.REMOTE_UPLOAD_DIRECTORY
        / (Task.REMOTE_UPLOAD_PREFIX + "timeout"))
    upload.mkdir(parents=True)
    source = upload / "video.mkv"
    source.write_bytes(b"original")
    cache_file = (
        cache_root / "unmanic_file_conversion-timeout" / "video-new.mkv")
    cache_file.parent.mkdir()
    cache_file.write_bytes(b"processed")

    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath=str(source),
            cache_path=str(cache_file),
            type="remote",
            status="processed",
            success=True,
        )
        current_task = _task_for_record(record)
        pp = _durable_postprocessor(cache_root, current_task)
        captured = {}

        def timeout_checkpoint(checkpoint):
            captured["checkpoint"] = checkpoint
            raise ResultTimeout("checkpoint still queued")

        current_task.save_remote_delivery_checkpoint = mock.Mock(
            side_effect=timeout_checkpoint)
        with mock.patch("unmanic.libs.postprocessor.PluginsHandler"):
            assert pp._process_remote_delivery("processed") is False

        assert source.read_bytes() == b"processed"
        assert Tasks.get_by_id(record.id).status == "remote_delivery"

        late_task = _task_for_record(Tasks.get_by_id(record.id))
        late_task.save_remote_delivery_checkpoint(captured["checkpoint"])
        recovered_task = _task_for_record(Tasks.get_by_id(record.id))
        restarted = _durable_postprocessor(cache_root, recovered_task)
        restarted.post_process_remote_file = mock.Mock(
            side_effect=AssertionError("confirmed delivery must not repeat"))

        assert restarted._persist_remote_metadata_and_complete() is True
        restarted.post_process_remote_file.assert_not_called()
        assert Tasks.get_by_id(record.id).status == "complete"
        assert source.read_bytes() == b"processed"


def test_metadata_failure_and_complete_failure_resume_without_redelivery(
        tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    upload = (
        cache_root / Task.REMOTE_UPLOAD_DIRECTORY
        / (Task.REMOTE_UPLOAD_PREFIX + "metadata"))
    upload.mkdir(parents=True)
    source = upload / "video.mkv"
    source.write_bytes(b"processed")
    cache_file = (
        cache_root / "unmanic_file_conversion-metadata" / "video-new.mkv")

    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath=str(source),
            cache_path=str(cache_file),
            type="remote",
            status="remote_delivery",
            success=True,
        )
        current_task = _task_for_record(record)
        pp = _durable_postprocessor(cache_root, current_task)
        claim = pp._build_remote_delivery_claim()
        claim["input_fingerprint"] = pp._remote_file_fingerprint(str(source))
        claim["target_paths"] = [str(source)]
        checkpoint = pp._build_remote_delivery_checkpoint(
            claim, str(source), True)
        current_task.save_remote_delivery_checkpoint(checkpoint)

        with mock.patch.object(
                pp, "_write_remote_metadata",
                side_effect=OSError("disk full")):
            assert pp._persist_remote_metadata_and_complete() is False
        assert Tasks.get_by_id(record.id).status == "remote_metadata_pending"

        current_task.set_status = mock.Mock(
            side_effect=ResultTimeout("complete still queued"))
        assert pp._persist_remote_metadata_and_complete() is False
        assert Tasks.get_by_id(record.id).status == "remote_metadata_pending"
        assert (
            upload / Task.REMOTE_METADATA_DIRECTORY / "data.json").is_file()

        recovered_task = _task_for_record(Tasks.get_by_id(record.id))
        restarted = _durable_postprocessor(cache_root, recovered_task)
        restarted.post_process_remote_file = mock.Mock(
            side_effect=AssertionError("metadata retry must not redeliver"))
        assert restarted._persist_remote_metadata_and_complete() is True
        restarted.post_process_remote_file.assert_not_called()
        assert Tasks.get_by_id(record.id).status == "complete"


def test_output_named_data_json_is_not_overwritten_by_remote_metadata(
        tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    upload = (
        cache_root / Task.REMOTE_UPLOAD_DIRECTORY
        / (Task.REMOTE_UPLOAD_PREFIX + "collision"))
    upload.mkdir(parents=True)
    source = upload / "data.json"
    source.write_bytes(b"original-json-output")
    cache_file = (
        cache_root / "unmanic_file_conversion-collision" / "data-new.json")
    cache_file.parent.mkdir()
    cache_file.write_bytes(b"processed-json-output")

    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath=str(source),
            cache_path=str(cache_file),
            type="remote",
            status="processed",
            success=True,
        )
        current_task = _task_for_record(record)
        pp = _durable_postprocessor(cache_root, current_task)

        with mock.patch("unmanic.libs.postprocessor.PluginsHandler"):
            assert pp._process_remote_delivery("processed") is True

        assert source.read_bytes() == b"processed-json-output"
        metadata = upload / Task.REMOTE_METADATA_DIRECTORY / "data.json"
        assert metadata.is_file()
        assert metadata.read_bytes() != source.read_bytes()


def _pending_handler():
    handler = ApiPendingHandler.__new__(ApiPendingHandler)
    handler.set_status = mock.Mock()
    handler.write_error = mock.Mock()
    handler.write_success = mock.Mock()
    handler.build_response = mock.Mock(side_effect=lambda _schema, data: data)
    return handler


def test_retrieval_protocol_serves_hidden_metadata_as_data_json(
        tmp_path):
    output = tmp_path / "data.json"
    output.write_bytes(b"legitimate-output")
    metadata = (
        tmp_path / Task.REMOTE_METADATA_DIRECTORY
        / Task.REMOTE_METADATA_FILENAME)
    metadata.parent.mkdir()
    metadata.write_text('{"task_success": true}')
    handler = _pending_handler()
    download_links = mock.Mock()
    download_links.generate_download_link.return_value = "link"

    with mock.patch(
            "unmanic.webserver.api_v2.pending_api.pending_tasks.fetch_tasks_status",
            return_value=[{
                "id": 7,
                "status": "complete",
                "abspath": str(output),
            }]), mock.patch(
                "unmanic.webserver.api_v2.pending_api.DownloadsLinks",
                return_value=download_links):
        asyncio.run(handler.gen_download_link_pending_task_data(task_id=7))

    download_links.generate_download_link.assert_called_once_with({
        "abspath": str(metadata),
        "basename": "data.json",
    })
    handler.write_success.assert_called_once_with({"link_id": "link"})
    assert output.read_bytes() == b"legitimate-output"


def test_output_download_is_rejected_until_remote_task_is_complete(
        tmp_path):
    output = tmp_path / "video.mkv"
    output.write_bytes(b"processed")
    handler = _pending_handler()

    with mock.patch(
            "unmanic.webserver.api_v2.pending_api.pending_tasks.fetch_tasks_status",
            return_value=[{
                "id": 7,
                "status": "remote_metadata_pending",
                "abspath": str(output),
            }]):
        asyncio.run(handler.gen_download_link_pending_task_file(task_id=7))

    handler.set_status.assert_called_once_with(
        handler.STATUS_ERROR_INTERNAL,
        reason="Pending tasks status is not 'complete'",
    )
    handler.write_error.assert_called_once_with()
    handler.write_success.assert_not_called()
