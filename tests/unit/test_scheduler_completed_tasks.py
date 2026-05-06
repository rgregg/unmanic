#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    test_scheduler_completed_tasks.py

    Tests for ScheduledTasksManager.manage_completed_tasks.

    Regression: the method previously did `historic_task.id for historic_task
    in results` over peewee `dicts()` rows, raising AttributeError and killing
    the ScheduledTasksManager thread at startup. Both the compress branch and
    the delete branch had related bugs (delete branch passed the raw cursor
    into a function that wanted an ID list).
"""
import logging
from unittest import mock

import pytest

from unmanic.libs.scheduler import ScheduledTasksManager


def _bare_manager():
    """Construct a ScheduledTasksManager without running __init__ — that
    would set up a real `schedule` library scheduler and a real logger.
    The method under test only needs `self.logger`."""
    m = ScheduledTasksManager.__new__(ScheduledTasksManager)
    m.logger = logging.getLogger("test_scheduler")
    return m


def _make_settings(*, auto_manage=True, max_age=30, compress=False, keep_failed=True):
    """Build a config-like mock that returns the values the method reads."""
    settings = mock.Mock()
    settings.get_auto_manage_completed_tasks.return_value = auto_manage
    settings.get_max_age_of_completed_tasks.return_value = max_age
    settings.get_compress_completed_tasks_logs.return_value = compress
    settings.get_always_keep_failed_tasks.return_value = keep_failed
    return settings


def _make_history(rows, *, delete_returns=True, compress_returns=True):
    """Build a history.History mock that returns dict rows from
    get_historic_task_list_filtered_and_sorted (matching peewee `dicts()`
    behaviour). The returned query supports `.count()` and iteration."""
    history_logging = mock.Mock()

    def _query():
        # Each call returns a fresh query-like object so the production code's
        # two separate calls (one for count, one for iteration) both work.
        q = mock.MagicMock()
        q.count.return_value = len(rows)
        q.__iter__.return_value = iter(rows)
        return q

    history_logging.get_historic_task_list_filtered_and_sorted.side_effect = (
        lambda *a, **kw: _query()
    )
    history_logging.delete_historic_tasks_recursively.return_value = delete_returns
    history_logging.delete_historic_task_command_logs.return_value = compress_returns
    return history_logging


class TestManageCompletedTasks:

    def test_skips_when_auto_manage_disabled(self):
        m = _bare_manager()
        settings = _make_settings(auto_manage=False)
        history_logging = _make_history([])
        with mock.patch("unmanic.libs.scheduler.config.Config", return_value=settings), \
                mock.patch("unmanic.libs.history.History", return_value=history_logging):
            m.manage_completed_tasks()
        history_logging.get_historic_task_list_filtered_and_sorted.assert_not_called()

    def test_no_tasks_returns_early(self):
        m = _bare_manager()
        settings = _make_settings(compress=False)
        history_logging = _make_history([])  # zero rows
        with mock.patch("unmanic.libs.scheduler.config.Config", return_value=settings), \
                mock.patch("unmanic.libs.history.History", return_value=history_logging):
            m.manage_completed_tasks()
        history_logging.delete_historic_tasks_recursively.assert_not_called()
        history_logging.delete_historic_task_command_logs.assert_not_called()

    def test_delete_branch_extracts_ids_from_dict_rows(self):
        """Regression: previously did historic_task.id on dict rows."""
        m = _bare_manager()
        settings = _make_settings(compress=False)
        history_logging = _make_history(
            [{"id": 1, "task_label": "a"}, {"id": 2, "task_label": "b"}, {"id": 7, "task_label": "c"}])
        with mock.patch("unmanic.libs.scheduler.config.Config", return_value=settings), \
                mock.patch("unmanic.libs.history.History", return_value=history_logging):
            m.manage_completed_tasks()
        # Must be called with a list of IDs, not the dicts cursor.
        history_logging.delete_historic_tasks_recursively.assert_called_once_with([1, 2, 7])

    def test_compress_branch_extracts_ids_from_dict_rows(self):
        m = _bare_manager()
        settings = _make_settings(compress=True)
        history_logging = _make_history(
            [{"id": 11, "task_label": "x"}, {"id": 12, "task_label": "y"}])
        with mock.patch("unmanic.libs.scheduler.config.Config", return_value=settings), \
                mock.patch("unmanic.libs.history.History", return_value=history_logging):
            m.manage_completed_tasks()
        history_logging.delete_historic_task_command_logs.assert_called_once_with([11, 12])
        history_logging.delete_historic_tasks_recursively.assert_not_called()

    def test_delete_failure_logs_and_returns(self):
        m = _bare_manager()
        settings = _make_settings(compress=False)
        history_logging = _make_history(
            [{"id": 1, "task_label": "a"}], delete_returns=False)
        with mock.patch("unmanic.libs.scheduler.config.Config", return_value=settings), \
                mock.patch("unmanic.libs.history.History", return_value=history_logging):
            # Should not raise.
            m.manage_completed_tasks()
        history_logging.delete_historic_tasks_recursively.assert_called_once()

    def test_compress_failure_logs_and_returns(self):
        m = _bare_manager()
        settings = _make_settings(compress=True)
        history_logging = _make_history(
            [{"id": 1, "task_label": "a"}], compress_returns=False)
        with mock.patch("unmanic.libs.scheduler.config.Config", return_value=settings), \
                mock.patch("unmanic.libs.history.History", return_value=history_logging):
            m.manage_completed_tasks()
        history_logging.delete_historic_task_command_logs.assert_called_once()

    def test_keeps_failed_when_configured(self):
        """When always_keep_failed_tasks is True, the query must filter on
        task_success=True (only deletes successful tasks)."""
        m = _bare_manager()
        settings = _make_settings(keep_failed=True, compress=False)
        history_logging = _make_history([{"id": 1, "task_label": "a"}])
        with mock.patch("unmanic.libs.scheduler.config.Config", return_value=settings), \
                mock.patch("unmanic.libs.history.History", return_value=history_logging):
            m.manage_completed_tasks()
        # Inspect the call to confirm task_success kwarg.
        call = history_logging.get_historic_task_list_filtered_and_sorted.call_args_list[0]
        assert call.kwargs.get("task_success") is True

    def test_includes_failed_when_not_keeping(self):
        """When always_keep_failed_tasks is False, task_success=None passes
        through (deletes both successful and failed)."""
        m = _bare_manager()
        settings = _make_settings(keep_failed=False, compress=False)
        history_logging = _make_history([{"id": 1, "task_label": "a"}])
        with mock.patch("unmanic.libs.scheduler.config.Config", return_value=settings), \
                mock.patch("unmanic.libs.history.History", return_value=history_logging):
            m.manage_completed_tasks()
        call = history_logging.get_historic_task_list_filtered_and_sorted.call_args_list[0]
        assert call.kwargs.get("task_success") is None
