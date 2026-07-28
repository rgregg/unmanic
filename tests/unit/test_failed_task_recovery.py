import logging
import queue
import threading
from unittest import mock

import pytest
from peewee import SqliteDatabase
from playhouse.sqliteq import ResultTimeout

from unmanic.libs.history import History
from unmanic.libs.task import Task, TaskCreationOwnershipUncertain, TaskFailureCategory
from unmanic.libs.taskhandler import TaskHandler
from unmanic.libs.unmodels import CompletedTasks, CompletedTasksCommandLogs, FileMetadataPaths, Tasks
from unmanic.libs.workers import Worker
from unmanic.webserver.api_v2.schema.schemas import CompletedTasksSchema
from unmanic.webserver.helpers import completed_tasks


@pytest.fixture
def history_database():
    database = SqliteDatabase(':memory:')
    models = [CompletedTasks, CompletedTasksCommandLogs, FileMetadataPaths, Tasks]
    with database.bind_ctx(models):
        database.create_tables(models)
        yield database


def _failure(**overrides):
    data = {
        'task_label': 'broken.mkv',
        'abspath': __file__,
        'task_success': False,
        'start_time': 100,
        'finish_time': 200,
        'processed_by_worker': 'Worker-1',
        'failure_category': TaskFailureCategory.PROCESSING,
        'failure_message': 'The processor reported a failure.',
        'failure_time': 190,
        'log': 'private diagnostic details',
    }
    data.update(overrides)
    return data


def test_failure_diagnostics_persist_and_are_returned_without_command_log(history_database):
    assert History().save_task_history(_failure())

    response = completed_tasks.prepare_filtered_completed_tasks({
        'status': 'failed',
        'start': 0,
        'length': 10,
    })

    assert response['failedCount'] == 1
    assert response['results'][0]['failure_category'] == 'processing_failed'
    assert response['results'][0]['failure_message'] == 'The processor reported a failure.'
    assert response['results'][0]['failure_time'] == 190
    assert 'private diagnostic details' not in response['results'][0]
    assert History().get_historic_task_data_dictionary(1)['failure_message'] == (
        'The processor reported a failure.'
    )


def test_command_log_failure_rolls_back_history_row(history_database, monkeypatch):
    monkeypatch.setattr(
        History,
        'create_historic_task_ffmpeg_log_entry',
        mock.Mock(side_effect=RuntimeError('log insert failed')),
    )

    assert History().save_task_history(_failure(source_task_id=42)) is False

    assert CompletedTasks.select().count() == 0
    assert CompletedTasksCommandLogs.select().count() == 0


def test_queued_log_timeout_keeps_owned_row_and_retry_is_idempotent(
        history_database, monkeypatch):
    monkeypatch.setattr('unmanic.libs.history.SqliteQueueDatabase', SqliteDatabase)
    history_logger = History()
    original_log_insert = history_logger.create_historic_task_ffmpeg_log_entry
    monkeypatch.setattr(
        history_logger,
        'create_historic_task_ffmpeg_log_entry',
        mock.Mock(side_effect=ResultTimeout('log insert timed out')),
    )

    assert history_logger.save_task_history(_failure(source_task_id=42)) is False
    assert CompletedTasks.select().count() == 1
    assert CompletedTasksCommandLogs.select().count() == 0

    monkeypatch.setattr(
        history_logger,
        'create_historic_task_ffmpeg_log_entry',
        original_log_insert,
    )
    assert history_logger.save_task_history(_failure(source_task_id=42)) is True
    assert CompletedTasks.select().count() == 1
    assert CompletedTasksCommandLogs.select().count() == 1


def test_queued_definitive_log_failure_keeps_history_row_without_delete(
        history_database, monkeypatch):
    monkeypatch.setattr('unmanic.libs.history.SqliteQueueDatabase', SqliteDatabase)
    delete_instance = mock.Mock()
    monkeypatch.setattr(CompletedTasks, 'delete_instance', delete_instance)
    monkeypatch.setattr(
        History,
        'create_historic_task_ffmpeg_log_entry',
        mock.Mock(side_effect=RuntimeError('log insert failed')),
    )

    assert History().save_task_history(_failure(source_task_id=42)) is False
    assert CompletedTasks.select().count() == 1
    assert CompletedTasksCommandLogs.select().count() == 0
    delete_instance.assert_not_called()


def test_queued_definitive_log_failure_retry_fills_log_without_late_delete(
        history_database, monkeypatch):
    monkeypatch.setattr('unmanic.libs.history.SqliteQueueDatabase', SqliteDatabase)
    history_logger = History()
    original_log_insert = history_logger.create_historic_task_ffmpeg_log_entry
    failed_insert = mock.Mock(side_effect=RuntimeError('log insert failed'))
    monkeypatch.setattr(
        history_logger,
        'create_historic_task_ffmpeg_log_entry',
        failed_insert,
    )

    assert history_logger.save_task_history(_failure(source_task_id=42)) is False

    monkeypatch.setattr(
        history_logger,
        'create_historic_task_ffmpeg_log_entry',
        original_log_insert,
    )
    assert history_logger.save_task_history(_failure(source_task_id=42)) is True
    assert CompletedTasks.select().count() == 1
    assert CompletedTasksCommandLogs.select().count() == 1


def test_reused_source_task_id_with_new_start_time_creates_new_history(history_database):
    history_logger = History()

    assert history_logger.save_task_history(_failure(source_task_id=42, start_time=100))
    assert history_logger.save_task_history(_failure(source_task_id=42, start_time=101))

    assert CompletedTasks.select().count() == 2
    assert CompletedTasksCommandLogs.select().count() == 2


def test_legacy_history_defaults_are_compatible(history_database):
    record = CompletedTasks.create(
        task_label='legacy.mkv',
        abspath=__file__,
        task_success=False,
        start_time=100,
        finish_time=200,
        processed_by_worker='Worker-1',
    )

    assert record.source_task_id is None
    assert record.failure_category == ''
    assert record.failure_message == ''
    assert record.failure_time is None
    assert record.dismissed_at is None


def test_dismissal_retains_history_and_diagnostics(history_database):
    History().save_task_history(_failure())

    assert completed_tasks.dismiss_failed_tasks([1])
    assert CompletedTasks.select().count() == 1
    record = CompletedTasks.get_by_id(1)
    assert record.dismissed_at is not None
    assert record.failure_message == 'The processor reported a failure.'
    assert completed_tasks.prepare_filtered_completed_tasks({
        'status': 'failed', 'start': 0, 'length': 10,
    })['recordsFiltered'] == 0
    assert completed_tasks.prepare_filtered_completed_tasks({
        'status': 'dismissed', 'start': 0, 'length': 10,
    })['recordsFiltered'] == 1


def test_successful_retry_keeps_history_and_dismisses_active_failure(history_database, monkeypatch):
    History().save_task_history(_failure())
    create = mock.Mock(return_value=True)
    monkeypatch.setattr(completed_tasks.task, 'Task', mock.Mock(
        return_value=mock.Mock(create_task_by_absolute_path=create)))

    assert completed_tasks.add_historic_tasks_to_pending_tasks_list([1], library_id=7) == {}

    create.assert_called_once_with(__file__, library_id=7)
    assert CompletedTasks.select().count() == 1
    assert CompletedTasks.get_by_id(1).dismissed_at is not None


def test_uncertain_retry_ownership_does_not_dismiss_failure(history_database, monkeypatch):
    History().save_task_history(_failure())
    create = mock.Mock(side_effect=TaskCreationOwnershipUncertain('timed out'))
    monkeypatch.setattr(completed_tasks.task, 'Task', mock.Mock(
        return_value=mock.Mock(create_task_by_absolute_path=create)))

    with pytest.raises(TaskCreationOwnershipUncertain):
        completed_tasks.add_historic_tasks_to_pending_tasks_list([1], library_id=7)

    assert CompletedTasks.get_by_id(1).dismissed_at is None


def test_task_failure_summary_is_sanitized_and_bounded():
    database = SqliteDatabase(':memory:')
    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        record = Tasks.create(abspath='/library/broken.mkv', status='in_progress')
        instance = Task.__new__(Task)
        instance.task = record
        instance.logger = logging.getLogger('test.failure')

        instance.set_failure('internal_error', ' secret\n\n' + ('x' * 600), failure_time=123)

        record = Tasks.get_by_id(record.id)
        assert record.failure_category == 'internal_error'
        assert '\\n' not in record.failure_message
        assert len(record.failure_message) == 500
        assert record.failure_time == 123


def test_worker_exception_records_safe_summary_and_completes_task():
    worker = Worker(1, 'Worker-1', 1, queue.Queue(), queue.Queue(), threading.Event())
    task = mock.Mock()
    task.task.start_time = 1
    worker.current_task = task
    worker.worker_runners_info = {}
    worker.worker_log = []

    worker._Worker__complete_exceptional_task()

    assert task.task.success is False
    assert task.task.failure_category == TaskFailureCategory.INTERNAL
    assert task.task.failure_message == (
        'An unexpected processing error occurred. Check application logs for details.')
    assert task.task.failure_time is not None
    task.persist_worker_completion.assert_called_once_with()
    assert worker.complete_queue.get_nowait() is task
    assert worker.current_task is None


def test_early_worker_exception_with_default_start_time_persists_history(
        history_database):
    record = Tasks.create(
        abspath=__file__,
        status='in_progress',
        processed_by_worker=None,
        finish_time=None,
    )
    current_task = Task()
    current_task.task = record
    worker = Worker(
        1, 'Worker-early', 1, queue.Queue(), queue.Queue(), threading.Event())
    worker.current_task = current_task
    worker.worker_runners_info = {}
    worker.worker_log = []

    worker._Worker__complete_exceptional_task()

    completed_task = worker.complete_queue.get_nowait()
    history_data = completed_task.task_dump()
    history_data['source_task_id'] = record.id
    assert History().save_task_history(history_data) is True
    historic = CompletedTasks.get()
    assert historic.processed_by_worker == 'Worker-early'
    assert historic.start_time is not None
    assert historic.finish_time is not None


def _startup_handler():
    handler = TaskHandler.__new__(TaskHandler)
    handler.settings = mock.Mock()
    handler.settings.get_clear_pending_tasks_on_restart.return_value = True
    handler.logger = logging.getLogger('test.worker.handoff.startup')
    return handler


def test_normal_worker_completion_survives_crash_before_foreman_drain(
        history_database):
    record = Tasks.create(
        abspath='/library/normal-crash-window.mkv',
        cache_path='/cache/unmanic_file_conversion-normal/result.mkv',
        status='in_progress',
        start_time=None,
        finish_time=None,
    )
    current_task = Task.__new__(Task)
    current_task.task = record
    complete_queue = queue.Queue()
    worker = Worker(
        1, 'Worker-normal', 1, queue.Queue(), complete_queue, threading.Event())
    worker.current_task = current_task
    worker.worker_log = ['completed log']
    worker._Worker__exec_worker_runners_on_set_task = mock.Mock(return_value=True)

    worker._Worker__process_task_queue_item()

    persisted = Tasks.get_by_id(record.id)
    assert persisted.status == 'processed'
    assert persisted.success is True
    assert persisted.processed_by_worker == 'Worker-normal'
    assert persisted.start_time is not None
    assert persisted.finish_time is not None
    assert complete_queue.qsize() == 1

    with mock.patch('unmanic.libs.task.TaskDataStore.clear_task'):
        _startup_handler().clear_tasks_on_startup()

    assert Tasks.get_by_id(record.id).status == 'processed'


def test_worker_completion_handoff_persists_downloaded_cache_path(
        history_database):
    record = Tasks.create(
        abspath='/library/remote-source.mkv',
        cache_path='/cache/original.mkv',
        status='in_progress',
        start_time=1,
    )
    current_task = Task.__new__(Task)
    current_task.task = record
    current_task.task.cache_path = '/cache/downloaded-result.mp4'
    current_task.task.success = True
    current_task.task.finish_time = 2
    current_task.task.processed_by_worker = 'RemoteTaskManager-1'

    assert current_task.persist_worker_completion() is True

    persisted = Tasks.get_by_id(record.id)
    assert persisted.status == 'processed'
    assert persisted.cache_path == '/cache/downloaded-result.mp4'
    assert persisted.success is True


def test_exceptional_worker_completion_survives_crash_before_foreman_drain(
        history_database):
    record = Tasks.create(
        abspath='/library/exception-crash-window.mkv',
        cache_path='/cache/unmanic_file_conversion-exception/result.mkv',
        status='in_progress',
        start_time=None,
        finish_time=None,
    )
    current_task = Task.__new__(Task)
    current_task.task = record
    complete_queue = queue.Queue()
    worker = Worker(
        1, 'Worker-exception', 1, queue.Queue(), complete_queue, threading.Event())
    worker.current_task = current_task
    worker.worker_log = []

    worker._Worker__complete_exceptional_task()

    persisted = Tasks.get_by_id(record.id)
    assert persisted.status == 'processed'
    assert persisted.success is False
    assert persisted.failure_category == TaskFailureCategory.INTERNAL
    assert persisted.processed_by_worker == 'Worker-exception'
    assert persisted.start_time is not None
    assert persisted.finish_time is not None
    assert complete_queue.qsize() == 1

    with mock.patch('unmanic.libs.task.TaskDataStore.clear_task'):
        _startup_handler().clear_tasks_on_startup()

    assert Tasks.get_by_id(record.id).status == 'processed'


def test_worker_completion_timeout_retains_worker_ownership_without_enqueue():
    worker = Worker(
        1, 'Worker-timeout', 1, queue.Queue(), queue.Queue(), threading.Event())
    current_task = mock.Mock()
    current_task.task.start_time = 1
    current_task.persist_worker_completion.side_effect = ResultTimeout(
        'completion timed out')
    worker.current_task = current_task
    worker.worker_log = []

    worker._Worker__complete_exceptional_task()

    assert worker.current_task is current_task
    assert worker._completion_pending is True
    assert worker.complete_queue.empty()


def test_completed_task_schema_exposes_failure_contract():
    payload = {
        'recordsTotal': 1,
        'recordsFiltered': 1,
        'successCount': 0,
        'failedCount': 1,
        'results': [{
            'id': 1,
            'task_label': 'broken.mkv',
            'task_success': False,
            'start_time': 100,
            'finish_time': 200,
            'has_metadata': False,
            'failure_category': 'processing_failed',
            'failure_message': 'Processing failed.',
            'failure_time': 190,
            'dismissed_at': None,
        }],
    }

    result = CompletedTasksSchema().dump(payload)

    assert result['results'][0]['failure_category'] == 'processing_failed'
    assert result['results'][0]['failure_time'] == 190
