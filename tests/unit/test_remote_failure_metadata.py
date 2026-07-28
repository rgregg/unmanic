import logging
import queue
import threading
from unittest import mock

from playhouse.sqliteq import ResultTimeout

from unmanic.libs.installation_link import Links, RemoteTaskManager
from unmanic.libs.task import TaskFailureCategory


def _manager_with_task():
    manager = RemoteTaskManager.__new__(RemoteTaskManager)
    manager.current_task = mock.Mock()
    return manager


def test_import_remote_failure_metadata():
    manager = _manager_with_task()

    manager._import_remote_failure({
        "task_success": False,
        "failure_category": "postprocessing_failed",
        "failure_message": "The remote file could not be staged.",
        "failure_time": 123,
    })

    manager.current_task.set_failure.assert_called_once_with(
        "postprocessing_failed",
        "The remote file could not be staged.",
        failure_time=123,
    )


def test_import_old_remote_failure_payload_uses_safe_defaults():
    manager = _manager_with_task()

    manager._import_remote_failure({"task_success": False})

    manager.current_task.set_failure.assert_called_once_with(
        TaskFailureCategory.PROCESSING,
        "The remote task reported a processing failure.",
        failure_time=None,
    )


def test_import_success_payload_does_not_set_failure():
    manager = _manager_with_task()

    manager._import_remote_failure({"task_success": True})

    manager.current_task.set_failure.assert_not_called()


def _runnable_manager(current_task):
    manager = RemoteTaskManager.__new__(RemoteTaskManager)
    threading.Thread.__init__(manager, name="RemoteTaskManager-1")
    manager.thread_id = 1
    manager.installation_info = {"address": "remote"}
    manager.pending_queue = queue.Queue()
    manager.pending_queue.put(current_task)
    manager.complete_queue = queue.Queue()
    manager.event = threading.Event()
    manager.current_task = None
    manager.worker_log = []
    manager.worker_runners_info = {}
    manager._completion_enqueued = False
    manager._completion_pending = False
    manager.logger = logging.getLogger("test.remote.manager")
    return manager


def test_remote_manager_exception_is_durably_completed_once():
    current_task = mock.Mock()
    current_task.task.start_time = 1
    current_task.task.processed_by_worker = None
    manager = _runnable_manager(current_task)

    with mock.patch("unmanic.libs.installation_link.PluginsHandler"), \
            mock.patch.object(
                manager,
                "_RemoteTaskManager__process_task_queue_item",
                side_effect=RuntimeError("remote manager failed"),
            ):
        manager.run()

    assert current_task.task.success is False
    assert current_task.task.failure_category == TaskFailureCategory.INTERNAL
    assert current_task.task.failure_message == (
        "An unexpected remote processing error occurred. "
        "Check application logs for details.")
    current_task.persist_worker_completion.assert_called_once_with()
    assert manager.complete_queue.get_nowait() is current_task
    assert manager.complete_queue.empty()
    assert manager.current_task is None
    assert current_task.task.processed_by_worker == "RemoteTaskManager-1"


def test_remote_metadata_import_exception_preserves_local_source_for_postprocessor():
    current_task = mock.Mock()
    current_task.task.start_time = None
    current_task.get_source_abspath.return_value = "/library/original.mkv"
    current_task.get_task_type.return_value = "local"
    manager = _runnable_manager(current_task)

    def fail_in_metadata_path():
        current_task.set_status("in_progress")
        raise RuntimeError("task-state metadata import failed")

    with mock.patch("unmanic.libs.installation_link.PluginsHandler"), \
            mock.patch.object(
                manager,
                "_RemoteTaskManager__process_task_queue_item",
                side_effect=fail_in_metadata_path,
            ):
        manager.run()

    assert current_task.set_status.call_args_list == [mock.call("in_progress")]
    current_task.persist_worker_completion.assert_called_once_with()
    current_task.delete.assert_not_called()
    assert manager.complete_queue.get_nowait() is current_task


def test_remote_completion_timeout_retries_without_overwriting_success(
        tmp_path):
    downloaded = tmp_path / "downloaded.mkv"
    downloaded.write_bytes(b"processed")
    current_task = mock.Mock()
    current_task.task.status = "in_progress"
    current_task.task.success = True
    current_task.task.failure_category = ""
    current_task.get_cache_path.return_value = str(downloaded)
    current_task.persist_worker_completion.side_effect = [
        ResultTimeout("handoff outcome unknown"),
        True,
    ]
    manager = _runnable_manager(current_task)
    manager.current_task = current_task
    manager._completion_pending = True

    assert manager._RemoteTaskManager__persist_and_publish_completion() is True

    assert current_task.persist_worker_completion.call_count == 2
    assert current_task.task.success is True
    assert current_task.task.failure_category == ""
    assert downloaded.read_bytes() == b"processed"
    assert manager.complete_queue.get_nowait() is current_task
    assert manager.current_task is None


def test_remote_completion_timeout_reconciles_late_commit_once():
    current_task = mock.Mock()
    current_task.task.status = "in_progress"

    def late_commit():
        if current_task.persist_worker_completion.call_count == 1:
            raise ResultTimeout("handoff outcome unknown")
        current_task.task.status = "processed"
        return False

    current_task.persist_worker_completion.side_effect = late_commit
    manager = _runnable_manager(current_task)
    manager.current_task = current_task
    manager._completion_pending = True

    assert manager._RemoteTaskManager__persist_and_publish_completion() is False

    assert current_task.persist_worker_completion.call_count == 2
    assert manager.complete_queue.get_nowait() is current_task
    assert manager.complete_queue.empty()
    assert manager.current_task is None


def test_successful_remote_retrieval_delete_sends_cleanup_acknowledgement():
    links = Links.__new__(Links)
    links.remote_api_delete = mock.Mock(return_value={"success": True})

    result = links.remove_task_from_remote_installation(
        {"address": "remote"},
        42,
        retrieval_complete=True,
    )

    assert result == {"success": True}
    links.remote_api_delete.assert_called_once_with(
        {"address": "remote"},
        "/unmanic/api/v2/pending/tasks",
        {
            "id_list": [42],
            "remote_retrieval_complete": True,
        },
        timeout=15,
    )
