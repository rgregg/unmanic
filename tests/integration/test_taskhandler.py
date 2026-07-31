#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    trawlarr.test_taskhandler.py

    Written by:               Josh.5 <jsunnex@gmail.com>
    Date:                     08 May 2020, (12:28 PM)

    Copyright:
           Copyright (C) Josh Sunnex - All Rights Reserved

           Permission is hereby granted, free of charge, to any person obtaining a copy
           of this software and associated documentation files (the "Software"), to deal
           in the Software without restriction, including without limitation the rights
           to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
           copies of the Software, and to permit persons to whom the Software is
           furnished to do so, subject to the following conditions:

           The above copyright notice and this permission notice shall be included in all
           copies or substantial portions of the Software.

           THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
           EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
           MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
           IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
           DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
           OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
           OR OTHER DEALINGS IN THE SOFTWARE.

    ------------------------------------------------------------------------
    Trawlarr fork note (issue #84).

    Two of the four tests in this module carried
    `@pytest.mark.skip("This test needs to be re-written to work with the
    TaskHandler DB integration")`. They asserted that a path put on a data
    queue arrived on a mock task queue - which stopped being how the
    TaskHandler works when it gained its database integration. They had been
    skipped ever since, and CI now runs this suite, so it was running a job
    that proved almost nothing about the component it names. A permanently
    skipped test is a lie about coverage.

    They are rewritten here rather than deleted, because the behaviour they
    were reaching for is worth pinning: the TaskHandler is the only thing that
    turns a discovered path into a queued task, and these two methods are
    where a change to that contract would land. What changed is the
    assertion - a real `Tasks` row in a real migrated database, instead of a
    mock's last-seen argument.

    The setup grew accordingly: a temp HOME so `Config` cannot touch the
    developer's `~/.trawlarr`, and a migrated database rather than a bare
    `Tasks` table, because creating a task reads the library's priority score.
    ------------------------------------------------------------------------

"""
import inspect
import os
import queue
import sys
import threading
import time

import pytest
from peewee import Model, SqliteDatabase

from trawlarr.libs.taskhandler import TaskHandler
from trawlarr.libs.unmodels.tasks import Tasks

#: A stopping thread has to be given a moment; two seconds is what the
#: original test allowed, and the loop's own wait is shorter than that.
THREAD_STOP_TIMEOUT = 2


class _RecordingTaskQueue:
    """
    Stands in for TaskQueue.

    The TaskHandler no longer hands anything to the task queue - it writes a
    row and the Foreman reads it back - so this exists only to satisfy the
    constructor. Any call to it is recorded rather than ignored, so a future
    change that starts using it is visible rather than silently absorbed.
    """

    def __init__(self):
        self.name = 'TaskQueue'
        self.calls = []

    def __getattr__(self, item):
        def _record(*args, **kwargs):
            self.calls.append((item, args, kwargs))
            return True

        return _record


class Environment:
    """The pieces a test needs from the fixture."""

    def __init__(self, handler, data_queues, task_queue, event, library_id, library_path):
        self.handler = handler
        self.data_queues = data_queues
        self.task_queue = task_queue
        self.event = event
        self.library_id = library_id
        self.library_path = library_path

    def library_file(self, name):
        path = os.path.join(self.library_path, name)
        with open(path, 'wb') as handle:
            handle.write(b'not really a video')
        return path


def _all_models():
    discovered = inspect.getmembers(sys.modules['trawlarr.libs.unmodels'], inspect.isclass)
    return [model for _name, model in discovered if issubclass(model, Model)]


@pytest.fixture
def task_handler(tmp_path, monkeypatch):
    """
    A started TaskHandler with a real migrated database behind it.

    peewee-migrate rebinds every model's Meta.database as a side effect of
    creating tables, so the models are bound inside a `bind_ctx` that restores
    the original bindings on the way out and leaves the rest of the suite
    alone.
    """
    from trawlarr import config
    from trawlarr.libs import runtimepaths
    from trawlarr.libs.db_migrate import Migrations
    from trawlarr.libs.library import Library
    from trawlarr.libs.unmodels.lib.basemodel import db as db_proxy

    home = tmp_path / 'home'
    library_path = tmp_path / 'library'
    home.mkdir()
    library_path.mkdir()

    monkeypatch.delenv('HOME_DIR', raising=False)
    monkeypatch.setenv('HOME', str(home))
    registry = type(config.Config)._instances
    registry.pop(config.Config, None)

    settings = config.Config()
    assert str(home) in settings.get_config_path(), \
        "Config did not resolve against the temp HOME; refusing to run"
    settings.set_config_item('debugging', True, save_settings=False)

    config_path = settings.get_config_path()
    os.makedirs(config_path, exist_ok=True)
    db_file = os.path.join(config_path, runtimepaths.DATABASE_FILE_NAME)
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(
        sys.modules['trawlarr.libs.db_migrate'].__file__)))
    database_settings = {
        "TYPE":                       "SQLITE",
        "FILE":                       db_file,
        "MIGRATIONS_DIR":             os.path.join(app_dir, 'migrations_v1'),
        "MIGRATIONS_HISTORY_VERSION": "v1",
    }

    previous_db = db_proxy.obj
    scratch_db = SqliteDatabase(db_file)
    binding = scratch_db.bind_ctx(_all_models())
    binding.__enter__()
    handler = None
    try:
        Migrations(database_settings).update_schema()

        library = Library.create({'name': 'TaskHandlerTests', 'path': str(library_path)})

        data_queues = {
            "library_scanner_triggers": queue.Queue(maxsize=1),
            "scheduledtasks":           queue.Queue(),
            "inotifytasks":             queue.Queue(),
            "progress_reports":         queue.Queue(),
        }
        task_queue = _RecordingTaskQueue()
        event = threading.Event()

        handler = TaskHandler(data_queues, task_queue, event)
        handler.daemon = True
        handler.start()

        yield Environment(handler, data_queues, task_queue, event,
                          library.get_id(), str(library_path))
    finally:
        if handler is not None:
            handler.stop()
            handler.join(THREAD_STOP_TIMEOUT + 3)
        binding.__exit__(None, None, None)
        scratch_db.close()
        db_proxy.initialize(previous_db)
        registry.pop(config.Config, None)


@pytest.mark.integrationtest
class TestTaskHandlerThreadLifecycle:

    def test_task_handler_runs_as_a_thread(self, task_handler):
        assert task_handler.handler.is_alive()

    def test_task_handler_thread_can_stop_in_less_than_two_seconds(self, task_handler):
        task_handler.event.set()
        task_handler.handler.stop()
        time.sleep(THREAD_STOP_TIMEOUT)
        assert not task_handler.handler.is_alive()


@pytest.mark.integrationtest
class TestTaskHandlerQueueProcessing:
    """
    What the two skipped tests were reaching for, said against the database.

    The TaskHandler is the only component that turns "the library scanner (or
    the inotify watcher) found this path" into a queued task. If either of
    these methods stops writing a task row, the application discovers files
    for ever and processes none of them, and nothing raises.
    """

    def test_a_scheduled_task_becomes_a_pending_task_row(self, task_handler):
        path = task_handler.library_file('scheduled.mkv')
        task_handler.data_queues['scheduledtasks'].put({
            'pathname':       path,
            'library_id':     task_handler.library_id,
            'priority_score': 3,
        })

        task_handler.handler.process_scheduledtasks_queue()

        row = Tasks.get_or_none(Tasks.abspath == os.path.abspath(path))
        assert row is not None, (
            "The scheduled task queue was drained without a task being "
            "created. Every file the library scanner finds is dropped.")
        assert row.status == 'pending', (
            "A local task has to reach 'pending' or no worker will ever claim "
            "it; it is sitting at '{}'.".format(row.status))
        assert row.library_id == task_handler.library_id
        assert row.type == 'local'
        assert row.cache_path, "A task with no cache path cannot be processed"

    def test_an_inotify_event_becomes_a_pending_task_row(self, task_handler):
        path = task_handler.library_file('inotify.mkv')
        task_handler.data_queues['inotifytasks'].put({
            'pathname':   path,
            'library_id': task_handler.library_id,
        })

        task_handler.handler.process_inotifytasks_queue()

        row = Tasks.get_or_none(Tasks.abspath == os.path.abspath(path))
        assert row is not None, (
            "The inotify queue was drained without a task being created. "
            "Files added to a watched library are never processed.")
        assert row.status == 'pending'
        assert row.library_id == task_handler.library_id

    def test_the_same_path_is_not_queued_twice(self, task_handler):
        """
        add_path_to_task_queue() promises a path is only queued once. Both
        queues call it, and the library scanner and the inotify watcher will
        routinely offer the same file within seconds of each other.
        """
        path = task_handler.library_file('duplicate.mkv')
        item = {'pathname': path, 'library_id': task_handler.library_id}

        task_handler.data_queues['scheduledtasks'].put(dict(item))
        task_handler.handler.process_scheduledtasks_queue()
        assert task_handler.handler.add_path_to_task_queue(path, task_handler.library_id) is False

        task_handler.data_queues['inotifytasks'].put(dict(item))
        task_handler.handler.process_inotifytasks_queue()

        assert Tasks.select().where(Tasks.abspath == os.path.abspath(path)).count() == 1, (
            "The same path was queued more than once. Two workers can now "
            "process one file at the same time.")

    def test_a_path_in_an_unknown_library_does_not_become_a_pending_task(self, task_handler):
        """
        The queue processors swallow exceptions so that one bad item cannot
        stop the loop. That is right, but it must not leave a half-built task
        row behind in 'pending' for the Foreman to claim.
        """
        path = task_handler.library_file('orphan.mkv')
        task_handler.data_queues['scheduledtasks'].put({
            'pathname':   path,
            'library_id': 999999,
        })

        task_handler.handler.process_scheduledtasks_queue()

        row = Tasks.get_or_none(Tasks.abspath == os.path.abspath(path))
        assert row is None or row.status != 'pending', (
            "A task was queued as pending against a library that does not "
            "exist. A worker will claim it and fail.")


if __name__ == '__main__':
    pytest.main(['-s', '--log-cli-level=INFO', __file__])
