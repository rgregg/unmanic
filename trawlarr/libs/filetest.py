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
from trawlarr.libs import donestate, extensions, history, common
from trawlarr.libs.logs import TrawlarrLogging
from trawlarr.libs.plugins import PluginsHandler
from trawlarr.libs.unplugins.executor import PLUGIN_RUNNER_EXCEPTION_KEY

# Trawlarr fork addition (see issue #33).
#
# THE PRECEDENCE MODEL, TOP TO BOTTOM
# -----------------------------------
#   Tier 0  native platform checks (this module, below)
#           Not overridable by anything in the plugin layer. These answer
#           questions about the file that are the platform's own business:
#           is it in scope for this library at all, and is it already done?
#   Tier 1  a guard plugin voting False - a veto (issue #32)
#   Tier 2  any plugin voting True - queue the file
#   Tier 3  a filter plugin voting False - skip the file
#
# Tier 0 is new. Before it, "already completed successfully" and "not a video
# file" were answered by two ordinary plugins whose votes sat in tiers 1-3
# along with everyone else's, which is how a file could be completed and then
# immediately re-queued, forever, while the installation looked healthy.
#
# WHAT THIS DID *NOT* CHANGE, AND WHY
# -----------------------------------
# Tiers 1-3 - the ADVISORY_SKIP_PLUGIN_IDS list and the veto-by-default
# heuristic below - are left exactly as issue #32 landed them. It is tempting
# to argue that with tier 0 in place the guard tier is redundant, since the
# two plugins that motivated it are now superseded by the platform. It is not:
#
#   * Those plugins are not uninstalled by this change. An existing library
#     still has them enabled and still gets their votes, and demoting those
#     votes to advisory in the same release that adds tier 0 would reopen the
#     reprocess loop for anyone whose plugin config we did not touch.
#   * The guard tier is not only about those two plugins. Any third-party
#     plugin that says "do not touch this file" relies on it.
#   * The full role-split redesign is issue #16, not this one.
#
# So the stopgap stays, tier 0 sits above it, and the two plugins become
# genuinely optional rather than load-bearing.
#
#
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


#: Issue id used for the file issue recorded when a plugin fails to vote.
#: Shown by /pending/test and logged by the file tester, so a broken plugin
#: names itself instead of disappearing.
ISSUE_PLUGIN_FAILED = 'filetestpluginfailed'


class FileTestPluginError(Exception):
    """
    A file-test plugin raised, so the file test has NO VERDICT for this file.

    Trawlarr fork addition (see issue #82).

    Before this, `execute_plugin_runner()` swallowed everything a third-party
    plugin threw and returned False, which the loop below treated exactly like
    "this plugin has no opinion". For a guard plugin - the one whose entire job
    is to say "do not touch this file" - that is the dangerous direction: the
    veto evaporates, the file is queued, and it gets modified. Nothing in any
    log the operator reads says the guard never ran.

    So a failed plugin no longer produces a (missing) vote; it produces this,
    and every caller decides for its own direction what an unanswered question
    means. Both existing callers resolve it to the answer that does NOT take
    irreversible action:

      * the scan/event path (should_file_be_added_to_task_list) does not queue
        the file, and records an issue naming the plugin. A wrongly unqueued
        file is visible in /pending/test and costs a scan cycle; a wrongly
        queued file is transcoded in place and may not be recoverable at all.
      * the convergence check (issue #34) lets it propagate and reports
        "not evaluated", never "converged" - the same rule that module already
        applies to a file test that raises.

    :ivar plugin_id:
    :ivar plugin_name:
    :ivar detail:  what the plugin actually threw
    """

    def __init__(self, plugin_id, plugin_name=None, detail=''):
        self.plugin_id = plugin_id
        self.plugin_name = plugin_name
        self.detail = detail
        super(FileTestPluginError, self).__init__(
            "File test plugin '{}' failed and cast no vote: {}".format(plugin_id, detail))

    @property
    def issue(self):
        return {
            'id':      ISSUE_PLUGIN_FAILED,
            'message': (
                "File test plugin '{}' raised and cast no vote ({}). The file has NOT been queued: "
                "a plugin that fails may be the one guarding this file, and its veto cannot be "
                "assumed away.").format(self.plugin_name or self.plugin_id, self.detail),
        }


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

    def __init__(self, library_id: int, ignore_completed_files: bool = False):
        """
        :param library_id:
        :param ignore_completed_files: skip the native completed-successfully
            check for this caller only. The seam for deliberate reprocessing
            (issue #41); see trawlarr/libs/donestate.py.
        """
        self.settings = config.Config()
        self.logger = TrawlarrLogging.get_logger(name=__class__.__name__)

        # Init plugins
        self.library_id = library_id
        self.plugin_handler = PluginsHandler()
        self.plugin_modules = self.plugin_handler.get_enabled_plugin_modules_by_type('library_management.file_test',
                                                                                     library_id=library_id)

        # Native tier-0 configuration
        self.ignore_completed_files = ignore_completed_files
        self.file_extension_allowlist = self.__read_library_extension_allowlist()

        # List of filed tasks
        self.failed_paths = []

    def __read_library_extension_allowlist(self):
        """
        Read this library's extension allow-list once, at construction.

        A failure here must not be allowed to gate the whole library: an
        unreadable allow-list falls back to the empty list, which allows
        everything, matching the behaviour of an installation that has not
        configured one.

        :return:
        """
        try:
            from trawlarr.libs.library import Library
            return tuple(Library(self.library_id).get_file_extension_allowlist())
        except Exception:
            self.logger.exception("Unable to read the file extension allow-list for library %s. "
                                  "No extension restriction will be applied.", self.library_id)
            return ()

    def file_extension_is_in_library_scope(self, path):
        """
        Is this file's extension within the library's allow-list?

        An empty allow-list allows everything - the default, and what every
        library gets on upgrade. See trawlarr/libs/extensions.py.

        :param path:
        :return:
        """
        return extensions.extension_is_allowed(path, getattr(self, 'file_extension_allowlist', ()))

    def file_already_completed_successfully(self, path):
        """
        Has this exact file already been processed successfully?

        Native counterpart to file_failed_in_history(), and the whole point of
        issue #33. See trawlarr/libs/donestate.py for what "this exact file"
        means and what it cannot detect.

        :param path:
        :return: (bool, message)
        """
        if getattr(self, 'ignore_completed_files', False):
            return False, ''
        return donestate.file_is_already_completed(path)

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

        # --- Tier 0: native platform checks ---------------------------------
        # These are not overridable by any plugin. Each one that rejects the
        # file records an issue, which is what /pending/test shows and what
        # the file tester logs - a file skipped here always says why.

        # Is the file even in scope for this library?
        if not self.file_extension_is_in_library_scope(path):
            file_issues.append({
                'id':      'extensionnotallowed',
                'message': "File extension is not in this library's allow-list ({}) - '{}'".format(
                    ', '.join(getattr(self, 'file_extension_allowlist', ())) or 'empty', path),
            })
            return_value = False

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

        # Check if this exact file has already been completed successfully.
        if return_value is None:
            already_completed, completed_message = self.file_already_completed_successfully(path)
            if already_completed:
                file_issues.append({
                    'id':      'alreadycompleted',
                    'message': completed_message,
                })
                return_value = False

        # Only run checks with plugins if other tests were not conclusive
        priority_score_modification = 0
        if return_value is None:
            try:
                return_value, file_issues, priority_score_modification, decision_plugin = self.run_file_test_plugins(
                    path, file_issues=file_issues)
            except FileTestPluginError as e:
                # Fail CLOSED. See FileTestPluginError for why this direction:
                # the plugin that broke may be the guard, and there is no way
                # to tell from here. The file is not queued and says why.
                self.logger.error("Not queueing '%s': %s", path, e)
                file_issues.append(e.issue)
                return_value = False
                decision_plugin = {
                    'plugin_id':   e.plugin_id,
                    'plugin_name': e.plugin_name,
                }

        return return_value, file_issues, priority_score_modification, decision_plugin

    def run_file_test_plugins(self, path, file_issues=None, shared_info=None):
        """
        Tiers 1-3 only: what do this library's file-test PLUGINS make of this
        path, with no native tier-0 gate in front of them?

        Split out of should_file_be_added_to_task_list() so that the library's
        criteria can be asked as a post-condition and not only as a
        pre-condition (issue #16's request, and what issue #34's convergence
        check is built on). The split is a pure extraction - the scan path
        calls straight through to it and behaves exactly as before.

        Why convergence must use THIS and not the full test: tier 0 answers
        "is this file in scope, and is it already done?". A file that has just
        completed is, by construction, recorded as done (issue #33), so the
        full test would answer "no, do not queue" for every completed file and
        report universal convergence while reporting nothing at all. The
        question convergence actually asks is narrower and is entirely a
        plugin-layer question: *given what the library is configured to want,
        does this file still qualify?*

        :param path:
        :param file_issues:  issues collected by earlier checks, if any
        :param shared_info:  seed for the plugin `shared_info` dict, so a
                             caller that has already probed this exact file
                             can offer the result to plugins that honour the
                             convention rather than making them probe again
        :raises FileTestPluginError: a plugin raised, so there is no verdict.
                             Deliberately not caught here: what "no verdict"
                             means depends on what the caller would do next,
                             and this method has no business guessing. See
                             FileTestPluginError.
        :return: (return_value, file_issues, priority_score_modification, decision_plugin)
        """
        file_issues = [] if file_issues is None else file_issues
        decision_plugin = None
        return_value = None

        # Set the initial data with just the priority score.
        data = {
            'priority_score': 0,
            'shared_info':    dict(shared_info) if shared_info else {},
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
            data.pop(PLUGIN_RUNNER_EXCEPTION_KEY, None)

            # Run plugin to update data
            try:
                ran = self.plugin_handler.exec_plugin_runner(data, plugin_module.get('plugin_id'),
                                                             'library_management.file_test')
            except Exception as e:
                # The executor swallows what the plugin throws, but not what
                # the loading machinery around it throws. Treat both the same.
                ran = False
                data[PLUGIN_RUNNER_EXCEPTION_KEY] = {
                    'plugin_id':      plugin_module.get('plugin_id'),
                    'exception_type': type(e).__name__,
                    'exception':      str(e),
                }

            plugin_error = data.get(PLUGIN_RUNNER_EXCEPTION_KEY)
            if plugin_error:
                # A plugin that raised cast no vote, and we cannot know which
                # vote it would have been (issue #82).
                #
                # The one exception is a plugin already known to be a FILTER.
                # A filter can only ever vote False, and a filter's False is
                # overridable by any True, so dropping it cannot change
                # whether the file is queued: with the vote, the answer is
                # False; without it and with no other vote, the answer is None
                # - and neither queues the file. Nothing irreversible follows
                # from losing it, so a broken filter is reported and stepped
                # over rather than halting the whole library.
                if file_test_skip_vote_is_advisory(plugin_module, data):
                    self.logger.warning("File test filter plugin '%s' raised while testing '%s': %s",
                                        plugin_module.get('plugin_id'), path, plugin_error.get('exception'))
                    file_issues.append({
                        'id':      ISSUE_PLUGIN_FAILED,
                        'message': (
                            "File test plugin '{}' raised and cast no vote ({}). It is a content filter, "
                            "whose vote cannot change whether a file is queued, so the test continued."
                        ).format(plugin_module.get('name') or plugin_module.get('plugin_id'),
                                 plugin_error.get('exception')),
                    })
                    continue
                raise FileTestPluginError(
                    plugin_module.get('plugin_id'),
                    plugin_name=plugin_module.get('name'),
                    detail='{}: {}'.format(plugin_error.get('exception_type'), plugin_error.get('exception')))

            if not ran:
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

        return return_value, file_issues, data.get('priority_score', 0), decision_plugin


class FileTesterThread(threading.Thread):
    def __init__(self, name, files_to_test, files_to_process, status_updates, library_id, event):
        super(FileTesterThread, self).__init__(name=name)
        self.settings = config.Config()
        self.logger = TrawlarrLogging.get_logger(name=__class__.__name__)
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
