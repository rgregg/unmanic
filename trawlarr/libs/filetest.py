#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    trawlarr.filetest.py

    Written by:               Josh.5 <jsunnex@gmail.com>
    Date:                     28 Mar 2021, (7:28 PM)

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
import os
import queue
import threading
import time
from copy import deepcopy

from trawlarr import config
from trawlarr.libs import history, common
from trawlarr.libs.logs import UnmanicLogging
from trawlarr.libs.plugins import PluginsHandler

# Trawlarr fork addition (see issue #32).
#
# The 'library_management.file_test' API gives a plugin only one way to say
# "no": returning add_file_to_pending_tasks = False. But two very different
# kinds of plugin use it:
#
#   guard   - "this file is out of scope / must not be touched"
#             (limit_library_search_by_file_extension, ignore_completed_tasks)
#   filter  - "I looked at the content and I have no work for this file"
#             (skip_files_matching_ffprobe_data)
#
# A guard's "no" must not be overridable by another plugin asking for work,
# or a file that never converges gets re-queued on every library scan. A
# filter's "no" must be overridable, or a filter locks out requester plugins
# that do have legitimate follow-up work.
#
# Until the plugin API can carry the role explicitly (issue #16), a False vote
# is treated as a guard veto by default and is only demoted to an advisory
# filter vote if the plugin says so. That default is the fail-safe direction:
# wrongly vetoing means work is not queued, which is visible in /pending/test
# and recoverable; wrongly overriding a veto means a silent re-queue loop.
#
# A plugin can declare itself a filter at runtime by setting
# data['file_test_role'] = 'filter' (forward-compatible with issue #16).
# Plugins that predate that key are listed here by plugin ID.
FILE_TEST_ROLE_KEY = 'file_test_role'
FILE_TEST_ROLE_FILTER = 'filter'
ADVISORY_SKIP_PLUGIN_IDS = frozenset([
    'skip_files_matching_ffprobe_data',
])


def file_test_skip_vote_is_advisory(plugin_module, data):
    """
    Determine whether a plugin's False vote is an advisory "no work for me"
    (overridable by a plugin requesting the file) or a hard veto.

    :param plugin_module:
    :param data:
    :return:
    """
    declared_role = data.get(FILE_TEST_ROLE_KEY)
    if declared_role is not None:
        return str(declared_role).lower() == FILE_TEST_ROLE_FILTER
    return plugin_module.get('plugin_id') in ADVISORY_SKIP_PLUGIN_IDS


class FileTest(object):
    """
    FileTest

    Object to manage tests carried out on files discovered
    during a library scan or inode event

    """

    def __init__(self, library_id: int):
        self.settings = config.Config()
        self.logger = UnmanicLogging.get_logger(name=__class__.__name__)

        # Init plugins
        self.library_id = library_id
        self.plugin_handler = PluginsHandler()
        self.plugin_modules = self.plugin_handler.get_enabled_plugin_modules_by_type('library_management.file_test',
                                                                                     library_id=library_id)

        # List of filed tasks
        self.failed_paths = []

    def set_file(self):
        pass

    def file_failed_in_history(self, path):
        """
        Check if file has already failed in history

        :return:
        """
        # Fetch historical tasks
        history_logging = history.History()
        if not self.failed_paths:
            failed_tasks = history_logging.get_historic_tasks_list_with_source_probe(task_success=False)
            for task in failed_tasks:
                self.failed_paths.append(task.get('abspath'))
        if path in self.failed_paths:
            # That pathname was found in the results of failed historic tasks
            return True
        # No results were found matching that pathname
        return False

    def file_in_unmanic_ignore_lockfile(self, path):
        """
        Check if folder contains a '.unmanicignore' lockfile

        :return:
        """
        # Get file parent directory
        dirname = os.path.dirname(path)
        # Check if lockfile (.unmanicignore) exists
        unmanic_ignore_file = os.path.join(dirname, '.unmanicignore')
        if os.path.exists(unmanic_ignore_file):
            # Get file basename
            basename = os.path.basename(path)
            # Read the file and check for any entry with this file name
            with open(unmanic_ignore_file) as f:
                for line in f:
                    if basename in line:
                        return True
        return False

    def should_file_be_added_to_task_list(self, path):
        """
        Test if this file needs to be added to the task list

        :return:
        """
        return_value = None
        decision_plugin = None
        file_issues = []

        # TODO: Remove this
        if self.file_in_unmanic_ignore_lockfile(path):
            file_issues.append({
                'id':      'unmanicignore',
                'message': "File found in unmanic ignore file - '{}'".format(path),
            })
            return_value = False

        # Check if file has failed in history.
        if self.file_failed_in_history(path):
            file_issues.append({
                'id':      'blacklisted',
                'message': "File found already failed in history - '{}'".format(path),
            })
            return_value = False

        # Only run checks with plugins if other tests were not conclusive
        priority_score_modification = 0
        if return_value is None:
            # Set the initial data with just the priority score.
            data = {
                'priority_score': 0,
                'shared_info':    {},
            }
            # Run the file-test plugins and collect their votes. Precedence,
            # highest first (see issue #32 and the notes at the top of this
            # module):
            #   1. a guard voting False vetoes the file outright. Nothing can
            #      override it, and the loop stops there so the expensive
            #      requester plugins are not run against a file that has
            #      already been ruled out.
            #   2. any plugin voting True ("queue this file") wins over a
            #      filter voting False, so requester-style plugins are not
            #      locked out by a filter that found nothing to do.
            #   3. otherwise a filter voting False skips the file.
            # Within a tier, the first plugin to cast the winning vote is
            # recorded as the decision plugin (used by /pending/test).
            # A future API revision should let plugins declare their role
            # explicitly; see issue #16.
            queue_decision_plugin = None
            veto_decision_plugin = None
            advisory_skip_plugin = None

            for plugin_module in self.plugin_modules:
                data['library_id'] = self.library_id
                data['path'] = path
                data['issues'] = deepcopy(file_issues)
                data['add_file_to_pending_tasks'] = None
                data[FILE_TEST_ROLE_KEY] = None

                # Run plugin to update data
                if not self.plugin_handler.exec_plugin_runner(data, plugin_module.get('plugin_id'),
                                                              'library_management.file_test'):
                    continue

                # Append any file issues found during previous tests
                file_issues = data.get('issues')

                vote = data.get('add_file_to_pending_tasks')
                if vote is True:
                    if queue_decision_plugin is None:
                        queue_decision_plugin = {
                            'plugin_id':   plugin_module.get('plugin_id'),
                            'plugin_name': plugin_module.get('name'),
                        }
                elif vote is False:
                    if file_test_skip_vote_is_advisory(plugin_module, data):
                        if advisory_skip_plugin is None:
                            advisory_skip_plugin = {
                                'plugin_id':   plugin_module.get('plugin_id'),
                                'plugin_name': plugin_module.get('name'),
                            }
                    else:
                        veto_decision_plugin = {
                            'plugin_id':   plugin_module.get('plugin_id'),
                            'plugin_name': plugin_module.get('name'),
                        }
                        # A veto is final. Stop here rather than running the
                        # remaining plugins (probes, etc.) against a file that
                        # will not be queued regardless of what they say.
                        break

            if veto_decision_plugin is not None:
                return_value = False
                decision_plugin = veto_decision_plugin
            elif queue_decision_plugin is not None:
                return_value = True
                decision_plugin = queue_decision_plugin
            elif advisory_skip_plugin is not None:
                return_value = False
                decision_plugin = advisory_skip_plugin
            # Set the priority score modification
            priority_score_modification = data.get('priority_score', 0)

        return return_value, file_issues, priority_score_modification, decision_plugin


class FileTesterThread(threading.Thread):
    def __init__(self, name, files_to_test, files_to_process, status_updates, library_id, event):
        super(FileTesterThread, self).__init__(name=name)
        self.settings = config.Config()
        self.logger = UnmanicLogging.get_logger(name=__class__.__name__)
        self.event = event
        self.files_to_test = files_to_test
        self.files_to_process = files_to_process
        self.library_id = library_id
        self.status_updates = status_updates
        self.abort_flag = threading.Event()
        self.abort_flag.clear()
        self._testing_lock = threading.Lock()
        self._currently_testing = False

    def stop(self):
        self.abort_flag.set()

    def _set_testing_state(self, state):
        with self._testing_lock:
            self._currently_testing = state

    def is_testing_file(self):
        with self._testing_lock:
            return self._currently_testing

    def run(self):
        self.logger.info("Starting %s", self.name)
        file_test = FileTest(self.library_id)
        plugin_handler = PluginsHandler()
        while not self.abort_flag.is_set():
            try:
                # Pending task queue has an item available. Fetch it.
                next_file = self.files_to_test.get_nowait()
                self._set_testing_state(True)
                self.status_updates.put(next_file)
            except queue.Empty:
                self._set_testing_state(False)
                self.event.wait(2)
                continue
            except Exception as e:
                self.logger.exception("Exception in fetching library scan result for path %s:", self.name)
                self._set_testing_state(False)
                continue

            # Test file to be added to task list. Add it if required
            try:
                result, issues, priority_score, _ = file_test.should_file_be_added_to_task_list(next_file)
                # Log any error messages
                for issue in issues:
                    if type(issue) is dict:
                        self.logger.info(issue.get('message'))
                    else:
                        self.logger.info(issue)
                # If file needs to be added, then add it
                if result:
                    self.add_path_to_queue({
                        'path':           next_file,
                        'priority_score': priority_score,
                    })
                    # Execute event plugin runners (only when added to queue)
                    plugin_handler.run_event_plugins_for_plugin_type('events.file_queued', {
                        'library_id':     self.library_id,
                        'file_path':      next_file,
                        'priority_score': priority_score,
                        'issues':         issues,
                    })

            except UnicodeEncodeError:
                self.logger.warning("File contains Unicode characters that cannot be processed. Ignoring.")
            except Exception as e:
                self.logger.exception("Exception testing file path in %s. Ignoring.", self.name)
            finally:
                self._set_testing_state(False)

        self.logger.info("Exiting %s", self.name)

    def add_path_to_queue(self, item):
        self.files_to_process.put(item)
