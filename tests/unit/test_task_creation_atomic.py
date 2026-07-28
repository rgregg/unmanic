import logging
from unittest import mock

import pytest
from peewee import SqliteDatabase
from playhouse.sqliteq import ResultTimeout

from unmanic.libs.task import Task, TaskCreationOwnershipUncertain
from unmanic.libs.unmodels.tasks import Tasks
from unmanic.webserver.helpers import pending_tasks


def _bare_task():
    task = Task.__new__(Task)
    task.task = None
    task.task_dict = None
    task.settings = mock.Mock()
    task.settings.get_cache_path.return_value = '/cache'
    task.logger = logging.getLogger('test.task.creation')
    task.statistics = {}
    task.errors = []
    task.get_task_library_priority_score = mock.Mock(return_value=0)
    return task


def test_failure_after_row_creation_rolls_back_task():
    database = SqliteDatabase(':memory:')
    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        new_task = _bare_task()
        new_task.get_task_data = mock.Mock(side_effect=RuntimeError('serialization failed'))

        with mock.patch.object(Tasks, 'delete_instance',
                               side_effect=RuntimeError('compensating delete failed')):
            with pytest.raises(RuntimeError, match='serialization failed'):
                new_task.create_task_by_absolute_path(
                    '/library/video.mkv',
                    return_task_data=True,
                )

        assert Tasks.select().count() == 0
        assert new_task.task is None
        assert new_task.task_dict is None


def test_result_timeout_reports_uncertain_ownership(monkeypatch):
    database = SqliteDatabase(':memory:')
    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        new_task = _bare_task()
        monkeypatch.setattr(
            Tasks,
            'create',
            mock.Mock(side_effect=ResultTimeout('insert timed out')),
        )
        monkeypatch.setattr('unmanic.libs.task.SqliteQueueDatabase', SqliteDatabase)

        with pytest.raises(TaskCreationOwnershipUncertain, match='timed out'):
            new_task.create_task_by_absolute_path('/library/video.mkv')

        assert new_task.task is None


def test_queued_creation_failure_never_issues_compensating_delete(monkeypatch):
    database = SqliteDatabase(':memory:')
    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        new_task = _bare_task()
        new_task.get_task_data = mock.Mock(side_effect=RuntimeError('serialization failed'))
        monkeypatch.setattr('unmanic.libs.task.SqliteQueueDatabase', SqliteDatabase)
        delete_instance = mock.Mock()
        monkeypatch.setattr(Tasks, 'delete_instance', delete_instance)

        with pytest.raises(TaskCreationOwnershipUncertain, match='ownership became uncertain'):
            new_task.create_task_by_absolute_path(
                '/library/video.mkv',
                return_task_data=True,
            )

        assert Tasks.select().count() == 1
        assert new_task.task is None
        delete_instance.assert_not_called()


def test_queued_remote_creation_failure_marks_only_ambiguous_row(monkeypatch):
    database = SqliteDatabase(':memory:')
    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        new_task = _bare_task()
        new_task.get_task_data = mock.Mock(
            side_effect=RuntimeError('serialization failed'))
        monkeypatch.setattr(
            'unmanic.libs.task.SqliteQueueDatabase', SqliteDatabase)

        with pytest.raises(TaskCreationOwnershipUncertain):
            new_task.create_task_by_absolute_path(
                '/cache/ambiguous.mkv',
                task_type='remote',
                return_task_data=True,
            )

        row = Tasks.get()
        assert row.status == 'creating'
        assert row.ownership_uncertain is True


@pytest.mark.parametrize(
    ('task_type', 'expected_status'),
    [('local', 'pending'), ('remote', 'creating')],
)
def test_successful_task_creation_behavior_is_preserved(task_type, expected_status):
    database = SqliteDatabase(':memory:')
    with database.bind_ctx([Tasks]):
        database.create_tables([Tasks])
        new_task = _bare_task()
        new_task.get_task_data = mock.Mock(return_value={'type': task_type, 'status': expected_status})

        result = new_task.create_task_by_absolute_path(
            '/library/{}.mkv'.format(task_type),
            task_type=task_type,
        )

        assert result is True
        assert Tasks.select().count() == 1
        assert new_task.task.status == expected_status
        assert new_task.task.type == task_type
        assert new_task.task.ownership_uncertain is False
        new_task.get_task_data.assert_not_called()


def test_add_remote_tasks_returns_data_built_during_creation(monkeypatch):
    created_data = {
        'id': 1,
        'abspath': '/cache/video.mkv',
        'priority': 1,
        'type': 'remote',
        'status': 'creating',
    }
    new_task = mock.Mock()
    new_task.create_task_by_absolute_path.return_value = created_data
    monkeypatch.setattr(pending_tasks.task, 'Task', mock.Mock(return_value=new_task))

    assert pending_tasks.add_remote_tasks('/cache/video.mkv') == created_data
    new_task.create_task_by_absolute_path.assert_called_once_with(
        '/cache/video.mkv',
        task_type='remote',
        return_task_data=True,
    )
    new_task.get_task_data.assert_not_called()
