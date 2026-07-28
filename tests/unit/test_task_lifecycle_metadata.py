import json
import logging

from peewee import SqliteDatabase

from unmanic.libs.metadata import UnmanicFileMetadata
from unmanic.libs.task import Task
from unmanic.libs.unmodels import TaskLifecycle, TaskMetadata, Tasks


def _task(record):
    current_task = Task.__new__(Task)
    current_task.task = record
    current_task.logger = logging.getLogger("test.task.lifecycle")
    current_task.errors = []
    return current_task


def _claim(tmp_path, task_id):
    source = tmp_path / "source.mkv"
    cache = tmp_path / "cache.mkv"
    destination = tmp_path / "destination.mkv"
    source.write_bytes(b"source")
    cache.write_bytes(b"processed")
    return {
        "version": 1,
        "phase": "claimed",
        "source_path": str(source),
        "cache_path": str(cache),
        "destination_path": str(destination),
        "target_paths": [str(destination)],
        "destination_in_cache": True,
        "input_fingerprint": None,
        "owner_task_id": task_id,
        "staging_token": None,
        "staging_directories": [],
    }


def test_plugin_cache_save_cannot_overwrite_lifecycle_payloads(tmp_path):
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath=str(tmp_path / "source.mkv"),
            cache_path=str(tmp_path / "cache.mkv"),
            status="processed",
            ownership_uncertain=True,
        )
        current_task = _task(record)
        UnmanicFileMetadata._task_cache.clear()
        UnmanicFileMetadata.bind_runner_context(
            "plugin.example", task_id=record.id)
        try:
            assert UnmanicFileMetadata.get() == {}
            current_task.save_remote_delivery_claim(
                _claim(tmp_path, record.id))
            current_task.save_completion_payload(
                [str(tmp_path / "destination.mkv")], True)
            UnmanicFileMetadata.set({"value": "from-stale-cache"})
        finally:
            UnmanicFileMetadata.clear_context()

        staged = json.loads(TaskMetadata.get().json_blob)
        assert staged["destination"]["plugin.example"] == {
            "value": "from-stale-cache"}
        assert "__meta__" not in staged or (
            "completion" not in staged["__meta__"]
            and "remote_delivery" not in staged["__meta__"])

        UnmanicFileMetadata._task_cache.clear()
        restarted = _task(Tasks.get_by_id(record.id))
        assert restarted.has_valid_remote_delivery_claim() is True
        assert restarted.get_completion_payload() == {
            "destination_files": [str(tmp_path / "destination.mkv")],
            "file_move_processes_success": True,
        }
        assert Tasks.get_by_id(record.id).ownership_uncertain is True


def test_legacy_internal_payloads_migrate_out_of_plugin_metadata(tmp_path):
    database = SqliteDatabase(":memory:")
    with database.bind_ctx([Tasks, TaskMetadata, TaskLifecycle]):
        database.create_tables([Tasks, TaskMetadata, TaskLifecycle])
        record = Tasks.create(
            abspath=str(tmp_path / "source.mkv"),
            cache_path=str(tmp_path / "cache.mkv"),
            status="processed",
        )
        completion = {
            "version": 1,
            "destination_files": [str(tmp_path / "destination.mkv")],
            "file_move_processes_success": True,
        }
        claim = _claim(tmp_path, record.id)
        TaskMetadata.create(
            task=record.id,
            json_blob=json.dumps({
                "destination": {"plugin.example": {"value": 1}},
                "__meta__": {
                    "completion": completion,
                    "remote_delivery": claim,
                },
            }),
        )

        restarted = _task(record)

        assert restarted.get_completion_payload() == {
            "destination_files": completion["destination_files"],
            "file_move_processes_success": True,
        }
        assert restarted.has_valid_remote_delivery_claim() is True
        lifecycle = json.loads(TaskLifecycle.get().json_blob)
        assert lifecycle["completion"] == completion
        assert lifecycle["remote_delivery"] == claim
