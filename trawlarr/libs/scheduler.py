#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    trawlarr.scheduler.py

    Written by:               Josh.5 <jsunnex@gmail.com>
    Date:                     11 Sep 2021, (11:15 AM)

    Modified 2026 by Ryan Gregg as part of Trawlarr.

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

"""
import threading
from datetime import datetime, timedelta

import schedule

from trawlarr import config
from trawlarr.libs.logs import UnmanicLogging
from trawlarr.libs.plugins import PluginsHandler
from trawlarr.libs.session import Session


class ScheduledTasksManager(threading.Thread):
    """
    Manage any tasks that Unmanic needs to execute at regular intervals
    """

    def __init__(self, event):
        super(ScheduledTasksManager, self).__init__(name='ScheduledTasksManager')
        self.logger = UnmanicLogging.get_logger(name=__class__.__name__)
        self.event = event
        self.abort_flag = threading.Event()
        self.abort_flag.clear()
        self.scheduler = schedule.Scheduler()

    def stop(self):
        self.abort_flag.set()

    def run(self):
        self.logger.info("Starting ScheduledTasks Monitor loop")

        # Create scheduled tasks
        # Local fork: no scheduled session check-in. The upstream heartbeat
        # called api.unmanic.app every 60 minutes; here register_unmanic
        # is a local-only no-op stub, so a recurring schedule has no value.
        # Run the plugin repo update every 3 hours
        self.scheduler.every(3).hours.do(self.plugin_repo_update)
        # Run a completed task cleanup every 60 minutes and on startup
        self.scheduler.every(12).hours.do(self.manage_completed_tasks)
        self.manage_completed_tasks()

        # Loop every 2 seconds to check if a task is due to be run
        while not self.abort_flag.is_set():
            self.event.wait(2)
            # Check if scheduled task is due
            self.scheduler.run_pending()

        # Clear any tasks and exit
        self.scheduler.clear()
        self.logger.info("Leaving ScheduledTasks Monitor loop...")

    def register_unmanic(self):
        self.logger.info("Updating session data")
        s = Session()
        s.register_unmanic(force=True)

    def plugin_repo_update(self):
        self.logger.info("Checking for updates to plugin repos")
        plugin_handler = PluginsHandler()
        plugin_handler.update_plugin_repos()

    def manage_completed_tasks(self):
        settings = config.Config()
        # Only run if configured to auto manage completed tasks
        if not settings.get_auto_manage_completed_tasks():
            return

        self.logger.info("Running completed task cleanup for this installation")
        max_age_in_days = settings.get_max_age_of_completed_tasks()
        compress_completed_tasks_logs = settings.get_compress_completed_tasks_logs()
        date_x_days_ago = datetime.now() - timedelta(days=int(max_age_in_days))
        before_time = date_x_days_ago.timestamp()

        task_success = True
        inc_status = 'successfully'
        if not settings.get_always_keep_failed_tasks():
            inc_status = 'successfully or failed'
            task_success = None

        # Fetch completed tasks
        from trawlarr.libs import history
        history_logging = history.History()
        count = history_logging.get_historic_task_list_filtered_and_sorted(task_success=task_success,
                                                                           before_time=before_time).count()
        results = history_logging.get_historic_task_list_filtered_and_sorted(task_success=task_success,
                                                                             before_time=before_time)

        if count == 0:
            self.logger.info("Found no %s completed tasks older than %s days", inc_status, max_age_in_days)
            return

        # `get_historic_task_list_filtered_and_sorted` returns peewee `dicts()`
        # rows (see history.py — the query ends with `query.dicts()`), so each
        # row is a `dict`. Accessing `.id` previously raised AttributeError
        # at startup and killed the ScheduledTasksManager thread, so cleanup
        # silently never ran. Materialise IDs once for both branches below;
        # this also avoids re-iterating the cursor.
        task_ids = [historic_task['id'] for historic_task in results]

        if compress_completed_tasks_logs:
            self.logger.info("Found %s %s completed tasks older than %s days that should be compressed", count,
                             inc_status, max_age_in_days)
            if not history_logging.delete_historic_task_command_logs(task_ids):
                self.logger.error("Failed to compress %s %s completed tasks", count, inc_status)
                return
            self.logger.info("Compressed %s %s completed tasks", count, inc_status)
            return

        self.logger.info("Found %s %s completed tasks older than %s days that should be removed", count, inc_status,
                         max_age_in_days)
        # `delete_historic_tasks_recursively` filters via `CompletedTasks.id.in_(id_list)`,
        # so it needs a list of IDs, not the dicts cursor. Previously this passed
        # the raw cursor and silently no-op'd because peewee's `in_` against
        # dict objects matches nothing.
        if not history_logging.delete_historic_tasks_recursively(task_ids):
            self.logger.error("Failed to delete %s %s completed tasks", count, inc_status)
            return

        self.logger.info("Deleted %s %s completed tasks", count, inc_status)
