#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# One task's delivered files must never be attributed to another task.
#
# PostProcessor is a single long-lived thread. `_last_destination_files`
# records what the current task actually delivered, and
# `record_completed_file()` uses it to write the done-state rows that stop
# a file being queued again (#33).
#
# The writer is `post_process_file()`. The reader is
# `record_completed_file()`. `run()` calls the first inside a try/except
# that logs the exception and carries on to the second. So when
# post-processing failed early -- a missing cache file, a plugin raising,
# a permissions error -- the attribute still held whatever the PREVIOUS
# task delivered, and the next task recorded those files as done under
# its own library and task id.
#
# The consequence is the one this milestone exists to eliminate: a file is
# marked complete without having been processed, so it is never offered to
# the plugins again. Nothing errors, and the queue looks healthy.

import pytest

from trawlarr.libs.postprocessor import PostProcessor


class _Task:
    """Minimal stand-in for the task object run() pulls off the queue."""

    def __init__(self, name):
        self.name = name

    def get_task_id(self):
        return self.name

    def get_task_library_id(self):
        return 1

    def get_task_type(self):
        return 'local'

    def get_task_success(self):
        return True

    def get_source_abspath(self):
        return '/library/{}.mkv'.format(self.name)

    def get_source_data(self):
        return {'abspath': self.get_source_abspath()}

    def get_destination_data(self):
        return {}


def _bare_postprocessor():
    """PostProcessor without its thread/queue machinery.

    __init__ needs data queues and a task queue this test has no use for,
    so build it bare and set only what these assertions touch -- the same
    approach the existing post-processor tests take.
    """
    pp = PostProcessor.__new__(PostProcessor)
    pp._last_destination_files = []
    # _log() reaches for a logger the bare object has none of.
    pp._log = lambda *args, **kwargs: None
    return pp


@pytest.mark.unittest
class TestDestinationFilesDoNotLeakBetweenTasks:

    def test_a_task_that_delivered_nothing_records_nothing(self, monkeypatch):
        """The core regression.

        Task A delivers a file. Task B fails before assigning anything. B
        must not record A's file.
        """
        recorded = []
        monkeypatch.setattr(
            'trawlarr.libs.postprocessor.donestate.record_completion',
            lambda path, library_id=None, task_id=None: recorded.append((path, task_id)) or True)

        pp = _bare_postprocessor()

        # Task A completes normally and delivers one file.
        pp.current_task = _Task('task_a')
        pp._last_destination_files = ['/library/task_a.mkv']
        pp.record_completed_file()
        assert recorded == [('/library/task_a.mkv', 'task_a')]

        # Task B arrives. run() resets per-task state before touching the
        # task; post_process_file() then fails and assigns nothing.
        pp._last_destination_files = []
        pp.current_task = _Task('task_b')
        pp.record_completed_file()

        assert recorded == [('/library/task_a.mkv', 'task_a')], (
            "Task B recorded a file it did not deliver: {}. A file marked "
            "complete without being processed is never queued again.".format(recorded)
        )

    def test_the_run_loop_clears_per_task_state_before_using_it(self):
        """Pins the fix at its call site.

        The reset has to happen in run(), because that is the only place
        that survives an exception from post_process_file(). Asserting on
        the source is crude, but the alternative is standing up the whole
        thread and queue, and an unpinned call site is exactly how this bug
        reached production.
        """
        import inspect
        source = inspect.getsource(PostProcessor.run)
        assert 'self._last_destination_files = []' in source, (
            "run() no longer clears per-task destination state; a failed "
            "task will inherit the previous task's delivered files."
        )

    def test_state_is_cleared_before_post_processing_not_after(self):
        """Order matters: clearing after post_process_file() would discard
        the very thing record_completed_file() needs."""
        import inspect
        source = inspect.getsource(PostProcessor.run)
        reset_at = source.index('self._last_destination_files = []')
        post_process_at = source.index('self.post_process_file()')
        assert reset_at < post_process_at, (
            "per-task state is cleared after post-processing, which would "
            "erase the files the task actually delivered"
        )
