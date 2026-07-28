import datetime
import fcntl
import logging
import os
from unittest import mock

import pytest
from peewee import SqliteDatabase
from playhouse.sqliteq import ResultTimeout

from unmanic.libs import common
from unmanic.libs.task import Task
from unmanic.libs.taskhandler import TaskHandler
from unmanic.libs.unmodels.tasks import Tasks


def _handler(clear_pending):
    handler = TaskHandler.__new__(TaskHandler)
    handler.settings = mock.Mock()
    handler.settings.get_clear_pending_tasks_on_restart.return_value = clear_pending
    handler.settings.get_cache_path.return_value = None
    handler.logger = logging.getLogger("test.taskhandler.startup")
    return handler


@pytest.mark.parametrize("clear_pending", [False, True])
def test_startup_cleanup_preserves_local_and_remote_recovery(clear_pending):
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        local_pending = Tasks.create(abspath="/library/pending.mkv", status="pending")
        local_history = Tasks.create(abspath="/library/history.mkv", status="history_pending")
        local_dispatch = Tasks.create(
            abspath="/library/dispatch.mkv", status="completion_dispatching")
        local_deletion = Tasks.create(abspath="/library/deletion.mkv", status="deletion_pending")
        local_processed = Tasks.create(abspath="/library/processed.mkv", status="processed")
        local_postprocessing = Tasks.create(
            abspath="/library/postprocessing.mkv", status="postprocessing")
        local_checkpoint = Tasks.create(
            abspath="/library/checkpoint.mkv", status="checkpoint_pending")
        remote_rows = [
            Tasks.create(
                abspath="/cache/remote-{}.mkv".format(status),
                type="remote",
                status=status,
            )
            for status in (
                "pending",
                "processed",
                "remote_delivery",
                "remote_metadata_pending",
                "complete",
            )
        ]
        interrupted_remote = Tasks.create(
            abspath="/cache/remote-in-progress.mkv",
            type="remote",
            status="in_progress",
        )

        with mock.patch("unmanic.libs.task.TaskDataStore.clear_task"):
            _handler(clear_pending).clear_tasks_on_startup()

        remaining_ids = set(Tasks.select(Tasks.id).tuples())
        assert (local_history.id,) in remaining_ids
        assert (local_dispatch.id,) in remaining_ids
        assert (local_deletion.id,) in remaining_ids
        assert (local_processed.id,) in remaining_ids
        assert (local_postprocessing.id,) in remaining_ids
        assert (local_checkpoint.id,) in remaining_ids
        assert ((local_pending.id,) in remaining_ids) is (not clear_pending)
        remaining_ids = set(Tasks.select(Tasks.id).tuples())
        assert all((remote.id,) in remaining_ids for remote in remote_rows)
        assert Tasks.get_by_id(interrupted_remote.id).status == "pending"


def test_reconcile_stale_local_creating_rows_by_initialized_state():
    database = SqliteDatabase(":memory:")
    now = datetime.datetime.now()
    old = now - datetime.timedelta(minutes=5)
    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        incomplete = Tasks.create(
            abspath="/library/incomplete.mkv", status="creating", start_time=old,
            ownership_uncertain=True)
        complete = Tasks.create(
            abspath="/library/complete.mkv",
            cache_path="/cache/complete.mkv",
            priority=42,
            status="creating",
            start_time=old,
            ownership_uncertain=True,
        )
        recent = Tasks.create(
            abspath="/library/recent.mkv", status="creating", start_time=now,
            ownership_uncertain=True)
        remote = Tasks.create(
            abspath="/cache/upload.mkv", type="remote", status="creating", start_time=old)

        with mock.patch("unmanic.libs.task.TaskDataStore.clear_task") as clear_task:
            result = Task.reconcile_stale_creating_tasks(grace_seconds=30, now=now)

        assert result == {"pending": 1, "deleted": 1}
        assert Tasks.get_by_id(complete.id).status == "pending"
        assert Tasks.get_by_id(recent.id).status == "creating"
        assert Tasks.get_by_id(remote.id).status == "creating"
        assert not Tasks.select().where(Tasks.id == incomplete.id).exists()
        clear_task.assert_called_once_with(incomplete.id)


def test_runtime_reconciliation_catches_a_create_committed_after_initial_check():
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        assert Task.reconcile_stale_creating_tasks(grace_seconds=0) == {
            "pending": 0,
            "deleted": 0,
        }

        late_row = Tasks.create(
            abspath="/library/late.mkv",
            status="creating",
            ownership_uncertain=True,
        )

        with mock.patch("unmanic.libs.task.TaskDataStore.clear_task"):
            result = Task.reconcile_stale_creating_tasks(
                grace_seconds=0,
                now=datetime.datetime.now() + datetime.timedelta(seconds=1),
            )

        assert result == {"pending": 0, "deleted": 1}
        assert not Tasks.select().where(Tasks.id == late_row.id).exists()


def test_runtime_remote_timeout_does_not_stop_later_reconciliation(tmp_path):
    handler = _handler(clear_pending=False)
    handler.settings.get_cache_path.return_value = str(tmp_path)
    remote_reconcile = mock.Mock(side_effect=[
        ResultTimeout("still queued"),
        {"pending": 1, "orphaned": 0},
    ])

    with mock.patch.object(
            Task, "reconcile_stale_creating_tasks",
            return_value={"pending": 0, "deleted": 0}), \
            mock.patch.object(
                Task, "reconcile_remote_uploads", remote_reconcile):
        handler.reconcile_creating_tasks()
        handler.reconcile_creating_tasks()

    assert remote_reconcile.call_count == 2


def test_restart_cache_cleanup_preserves_recoverable_conversion_and_removes_orphan(
        tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    retained = cache_root / "unmanic_file_conversion-retained"
    checkpoint = cache_root / "unmanic_file_conversion-checkpoint"
    orphan = cache_root / "unmanic_file_conversion-orphan"
    unrelated = tmp_path / "unmanic_file_conversion-unrelated"
    for directory in (retained, checkpoint, orphan, unrelated):
        directory.mkdir(parents=True)
        (directory / "result.mkv").write_bytes(b"converted")

    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        Tasks.create(
            abspath="/library/recover.mkv",
            cache_path=str(retained / "result.mkv"),
            status="processed",
        )
        Tasks.create(
            abspath="/library/checkpoint.mkv",
            cache_path=str(checkpoint / "result.mkv"),
            status="checkpoint_pending",
        )
        Tasks.create(
            abspath="/library/traversal.mkv",
            cache_path=str(cache_root / "unmanic_file_conversion-fake"
                           / ".." / ".." / unrelated.name / "result.mkv"),
            status="history_pending",
        )

        preserved = Task.get_recoverable_cache_directories(str(cache_root))
        common.clean_files_in_cache_dir(
            str(cache_root), preserved_directories=preserved)

    assert retained.exists()
    assert checkpoint.exists()
    assert not orphan.exists()
    assert unrelated.exists()
    assert preserved == {str(retained.resolve()), str(checkpoint.resolve())}


def test_restart_cache_cleanup_preserves_remote_delivery_input(tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    conversion = cache_root / "unmanic_file_conversion-remote"
    conversion.mkdir(parents=True)
    output = conversion / "result.mkv"
    output.write_bytes(b"processed")
    upload = (
        cache_root / Task.REMOTE_UPLOAD_DIRECTORY
        / (Task.REMOTE_UPLOAD_PREFIX + "remote") / "source.mkv")
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"original")

    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        Tasks.create(
            abspath=str(upload),
            cache_path=str(output),
            type="remote",
            status="remote_delivery",
        )

        preserved = Task.get_recoverable_cache_directories(str(cache_root))
        common.clean_files_in_cache_dir(
            str(cache_root), preserved_directories=preserved)

    assert output.read_bytes() == b"processed"
    assert preserved == {str(conversion.resolve())}


def test_late_valid_remote_create_is_finalized_and_exact_upload_is_retained(
        tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    upload_directory = (
        cache_root / "remote_library" / "nested"
        / "unmanic_remote_pending_library-late")
    upload_directory.mkdir(parents=True)
    upload_path = upload_directory / "video.mkv"
    upload_path.write_bytes(b"uploaded")
    created = datetime.datetime.now()
    created_timestamp = created.timestamp()
    os.utime(upload_directory, (created_timestamp, created_timestamp))

    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        assert Task.reconcile_remote_uploads(
            str(cache_root), grace_seconds=30, now=created) == {
                "pending": 0,
                "orphaned": 0,
            }

        # Simulate the queued insert committing after the first reconciliation.
        remote = Tasks.create(
            abspath=str(upload_path),
            cache_path=str(cache_root / "unmanic_file_conversion-late" / "video.mkv"),
            priority=42,
            type="remote",
            status="creating",
            start_time=created,
            ownership_uncertain=True,
        )
        shared_path = tmp_path / "shared.mkv"
        shared_path.write_bytes(b"shared")
        shared_remote = Tasks.create(
            abspath=str(shared_path),
            cache_path=str(cache_root / "unmanic_file_conversion-shared" / "video.mkv"),
            priority=43,
            type="remote",
            status="creating",
            start_time=created,
            ownership_uncertain=True,
        )

        result = Task.reconcile_remote_uploads(
            str(cache_root), grace_seconds=30,
            now=created + datetime.timedelta(seconds=31))

        assert result == {"pending": 1, "orphaned": 0}
        assert Tasks.get_by_id(remote.id).status == "pending"
        assert Tasks.get_by_id(shared_remote.id).status == "creating"
        assert upload_path.read_bytes() == b"uploaded"


def test_normal_remote_create_waits_for_ready_after_checksum_delay(tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    upload_directory = (
        cache_root / Task.REMOTE_UPLOAD_DIRECTORY
        / (Task.REMOTE_UPLOAD_PREFIX + "checksum"))
    upload_directory.mkdir(parents=True)
    upload_path = upload_directory / "video.mkv"
    upload_path.write_bytes(b"uploaded")
    created = datetime.datetime.now() - datetime.timedelta(minutes=5)

    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        remote = Tasks.create(
            abspath=str(upload_path),
            cache_path=str(cache_root / "conversion" / "video.mkv"),
            priority=1,
            type="remote",
            status="creating",
            start_time=created,
            ownership_uncertain=False,
        )

        result = Task.reconcile_remote_uploads(
            str(cache_root),
            grace_seconds=30,
            now=datetime.datetime.now(),
        )

        assert result == {"pending": 0, "orphaned": 0}
        assert Tasks.get_by_id(remote.id).status == "creating"
        assert Task.set_tasks_status([remote.id], "pending") is True
        assert Tasks.get_by_id(remote.id).status == "pending"


@pytest.mark.parametrize("ready_first", [False, True])
def test_ready_and_uncertain_reconciliation_never_reset_active_task(
        tmp_path, ready_first):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    upload_directory = (
        cache_root / Task.REMOTE_UPLOAD_DIRECTORY
        / (Task.REMOTE_UPLOAD_PREFIX + "race"))
    upload_directory.mkdir(parents=True)
    upload_path = upload_directory / "video.mkv"
    upload_path.write_bytes(b"uploaded")
    old = datetime.datetime.now() - datetime.timedelta(minutes=5)

    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        remote = Tasks.create(
            abspath=str(upload_path),
            cache_path=str(cache_root / "conversion" / "video.mkv"),
            priority=1,
            type="remote",
            status="creating",
            start_time=old,
            ownership_uncertain=True,
        )
        if ready_first:
            assert Task.set_tasks_status([remote.id], "pending") is True
        Task.reconcile_remote_uploads(
            str(cache_root), grace_seconds=30,
            now=datetime.datetime.now())
        if not ready_first:
            assert Task.set_tasks_status([remote.id], "pending") is True

        Tasks.update(status="in_progress").where(
            Tasks.id == remote.id).execute()
        assert Task.set_tasks_status([remote.id], "pending") is True
        Task.reconcile_remote_uploads(
            str(cache_root), grace_seconds=0,
            now=datetime.datetime.now())
        assert Tasks.get_by_id(remote.id).status == "in_progress"


def test_library_update_refuses_to_mutate_running_task():
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        remote = Tasks.create(
            abspath="/cache/running.mkv",
            type="remote",
            status="in_progress",
            library_id=1,
        )

        assert Task.set_tasks_library_id([remote.id], 2) is False
        assert Tasks.get_by_id(remote.id).library_id == 1


def test_no_commit_remote_upload_is_removed_only_after_grace(tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    upload_directory = (
        cache_root / "remote_library"
        / "unmanic_remote_pending_library-orphan")
    upload_directory.mkdir(parents=True)
    upload_path = upload_directory / "video.mkv"
    upload_path.write_bytes(b"uploaded")
    created = datetime.datetime.now()
    created_timestamp = created.timestamp()
    os.utime(upload_directory, (created_timestamp, created_timestamp))

    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        assert Task.reconcile_remote_uploads(
            str(cache_root), grace_seconds=30, now=created) == {
                "pending": 0,
                "orphaned": 0,
            }
        assert upload_path.exists()

        result = Task.reconcile_remote_uploads(
            str(cache_root),
            grace_seconds=30,
            now=created + datetime.timedelta(seconds=31),
        )

        assert result == {"pending": 0, "orphaned": 1}
        assert not upload_directory.exists()


def test_reconciliation_keeps_locked_active_marker_and_removes_crashed_marker(
        tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    active_directory = (
        cache_root / "remote_library"
        / "unmanic_remote_pending_library-active")
    crashed_directory = (
        cache_root / "remote_library"
        / "unmanic_remote_pending_library-crashed")
    for directory in (active_directory, crashed_directory):
        directory.mkdir(parents=True)
        (directory / "video.mkv").write_bytes(b"uploading")
        marker = directory / Task.REMOTE_UPLOAD_ACTIVE_MARKER
        marker.write_text("owner")
        os.utime(marker, (1, 1))
        os.utime(directory, (1, 1))

    active_marker = os.open(
        active_directory / Task.REMOTE_UPLOAD_ACTIVE_MARKER, os.O_RDWR)
    fcntl.flock(active_marker, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with database.bind_ctx([Tasks]):
            database.create_tables([Tasks])
            result = Task.reconcile_remote_uploads(
                str(cache_root),
                grace_seconds=30,
                now=datetime.datetime.now(),
            )
    finally:
        os.close(active_marker)

    assert result == {"pending": 0, "orphaned": 1}
    assert active_directory.exists()
    assert not crashed_directory.exists()


def test_crashed_marker_uses_conservative_grace(tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    upload_directory = (
        cache_root / "remote_library"
        / "unmanic_remote_pending_library-crashed")
    upload_directory.mkdir(parents=True)
    (upload_directory / "video.mkv").write_bytes(b"uploaded")
    marker = upload_directory / Task.REMOTE_UPLOAD_ACTIVE_MARKER
    marker.write_text("owner")
    created = datetime.datetime.now()
    timestamp = created.timestamp()
    os.utime(upload_directory, (1, 1))
    os.utime(marker, (timestamp, timestamp))

    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        assert Task.reconcile_remote_uploads(
            str(cache_root),
            grace_seconds=30,
            now=created + datetime.timedelta(seconds=31),
        ) == {"pending": 0, "orphaned": 0}
        assert upload_directory.exists()

        result = Task.reconcile_remote_uploads(
            str(cache_root),
            grace_seconds=30,
            now=created + datetime.timedelta(
                seconds=Task.REMOTE_UPLOAD_MARKER_GRACE_SECONDS + 1),
        )

    assert result == {"pending": 0, "orphaned": 1}
    assert not upload_directory.exists()


def test_no_commit_cleanup_waits_for_inflight_queue_barrier(
        monkeypatch, tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    upload_directory = (
        cache_root / "remote_library"
        / "unmanic_remote_pending_library-inflight")
    upload_directory.mkdir(parents=True)
    (upload_directory / "video.mkv").write_bytes(b"uploaded")
    os.utime(upload_directory, (1, 1))

    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        barrier = mock.Mock()
        barrier.where.return_value = barrier
        barrier.execute.side_effect = ResultTimeout("insert still in flight")
        monkeypatch.setattr(
            "unmanic.libs.task.SqliteQueueDatabase", SqliteDatabase)
        monkeypatch.setattr(Tasks, "update", mock.Mock(return_value=barrier))

        result = Task.reconcile_remote_uploads(
            str(cache_root), grace_seconds=30,
            now=datetime.datetime.now())

    assert result == {"pending": 0, "orphaned": 0}
    assert upload_directory.exists()
    barrier.execute.assert_called_once_with()


def test_remote_cleanup_requires_exact_task_path_ownership(tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    owned_directory = (
        cache_root / "remote_library" / "a"
        / "unmanic_remote_pending_library-owned")
    orphan_directory = (
        cache_root / "remote_library" / "b"
        / "unmanic_remote_pending_library-orphan")
    outside_directory = (
        tmp_path / "outside" / "unmanic_remote_pending_library-outside")
    for directory in (owned_directory, orphan_directory, outside_directory):
        directory.mkdir(parents=True)
        (directory / "video.mkv").write_bytes(b"uploaded")
        os.utime(directory, (1, 1))

    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        Tasks.create(
            abspath=str(owned_directory / "video.mkv"),
            type="remote",
            status="creating",
        )
        # A merely similar path must not claim the orphan.
        Tasks.create(
            abspath=str(orphan_directory) + "-other/video.mkv",
            type="remote",
            status="creating",
        )

        result = Task.reconcile_remote_uploads(
            str(cache_root), grace_seconds=30,
            now=datetime.datetime.now())

    assert result == {"pending": 0, "orphaned": 1}
    assert owned_directory.exists()
    assert not orphan_directory.exists()
    assert outside_directory.exists()


def test_startup_reconciles_stale_nested_remote_upload_without_deleting_it(
        tmp_path):
    database = SqliteDatabase(":memory:")
    cache_root = tmp_path / "cache"
    upload_directory = (
        cache_root / "remote_library" / "nested"
        / "unmanic_remote_pending_library-stale")
    upload_directory.mkdir(parents=True)
    upload_path = upload_directory / "video.mkv"
    upload_path.write_bytes(b"uploaded")
    os.utime(upload_directory, (1, 1))

    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        remote = Tasks.create(
            abspath=str(upload_path),
            cache_path=str(cache_root / "conversion" / "video.mkv"),
            priority=1,
            type="remote",
            status="creating",
            start_time=datetime.datetime.fromtimestamp(1),
            ownership_uncertain=True,
        )
        handler = _handler(clear_pending=False)
        handler.settings.get_cache_path.return_value = str(cache_root)

        with mock.patch("unmanic.libs.task.TaskDataStore.clear_task"):
            handler.clear_tasks_on_startup()

        assert Tasks.select().count() == 1
        assert Tasks.get_by_id(remote.id).status == "pending"
        assert upload_directory.exists()
