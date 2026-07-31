#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    trawlarr.postprocessor.py
 
    Written by:               Josh.5 <jsunnex@gmail.com>
    Date:                     23 Apr 2019, (7:33 PM)

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
import shutil
import threading
import time

from trawlarr import config
from trawlarr.libs import common, convergence, donestate, history, sanity, taskfailure
from trawlarr.libs.frontend_push_messages import FrontendPushMessages
from trawlarr.libs.library import Library
from trawlarr.libs.logs import TrawlarrLogging
from trawlarr.libs.metadata import TrawlarrFileMetadata
from trawlarr.libs.notifications import Notifications
from trawlarr.libs.plugins import PluginsHandler
from trawlarr.libs.task import TaskDataStore

"""

The post-processor handles all tasks carried out on completion of a workers task.
This may be on either success or failure of the task.

The post-processor runs as a single thread, processing completed jobs one at a time.
This prevents conflicting copy operations or deleting a file that is also being post processed.

"""


class PostProcessError(Exception):
    def __init__(self, expected_var, result_var):
        Exception.__init__(self, "Errors found during post process checks. Expected {}, but instead found {}".format(
            expected_var, result_var))
        self.expected_var = expected_var
        self.result_var = result_var


class PostProcessor(threading.Thread):
    """
    PostProcessor

    """

    def __init__(self, data_queues, task_queue, event):
        super(PostProcessor, self).__init__(name='PostProcessor')
        self.logger = TrawlarrLogging.get_logger(name=__class__.__name__)
        self.event = event
        self.data_queues = data_queues
        self.settings = config.Config()
        self.task_queue = task_queue
        self.abort_flag = threading.Event()
        self.current_task = None
        self._last_destination_files = []
        self._last_file_move_processes_success = False
        # The output probe the sanity checks (issue #35) already paid for, kept
        # so the convergence check (issue #34) does not probe the same bytes a
        # second time. Reset per task alongside _last_destination_files.
        self._last_output_probe = None
        self._last_output_size = None
        self.ffmpeg = None
        self.abort_flag.clear()

    def _log(self, message, message2='', level="info"):
        message = common.format_message(message, message2)
        getattr(self.logger, level)(message)

    def stop(self):
        self.abort_flag.set()

    def run(self):
        self._log("Starting PostProcessor Monitor loop...")
        while not self.abort_flag.is_set():
            self.event.wait(1)

            if not self.system_configuration_is_valid():
                self.event.wait(2)
                continue

            while not self.abort_flag.is_set() and not self.task_queue.task_list_processed_is_empty():
                self.event.wait(.2)
                self.current_task = self.task_queue.get_next_processed_tasks()
                if self.current_task:
                    # Reset per-task state before anything can read it.
                    # post_process_file() is the only writer, and run() calls it
                    # inside a try/except that logs and carries on -- so if it
                    # raises before assigning, whatever the PREVIOUS task
                    # delivered is still here, and record_completed_file() will
                    # happily record those files as done under THIS task's id.
                    # A file would be marked complete without having been
                    # processed, and never queued again.
                    self._last_destination_files = []
                    # Same reasoning for the file-move verdict (issue #86). It
                    # gates record_completed_file(), so a stale True from the
                    # previous task would let this task's untouched source be
                    # signed off. False is the only safe starting point:
                    # nothing has moved yet.
                    self._last_file_move_processes_success = False
                    # And the output probe the convergence check reuses
                    # (issue #34): a stale probe would have this task's
                    # convergence evaluated against the previous task's bytes.
                    self._last_output_probe = None
                    self._last_output_size = None

                    # Execute event plugin runners
                    plugin_handler = PluginsHandler()
                    plugin_handler.run_event_plugins_for_plugin_type('events.postprocessor_started', {
                        'library_id':  self.current_task.get_task_library_id(),
                        'task_id':     self.current_task.get_task_id(),
                        'task_type':   self.current_task.get_task_type(),
                        'cache_path':  self.current_task.get_cache_path(),
                        'source_data': self.current_task.get_source_data(),
                    })

                    try:
                        self._log("Post-processing task - {}".format(self.current_task.get_source_abspath()))
                    except Exception as e:
                        self._log("Exception in fetching task absolute path", message2=str(e), level="exception")
                    if self.current_task.get_task_type() == 'local':
                        try:
                            # Post processes the converted file (return it to original directory etc.)
                            self.post_process_file()
                        except Exception as e:
                            self._log("Exception in post-processing local task file",
                                      message2=str(e), level="exception")
                            # This is the stage that establishes delivery. If
                            # it did not finish, the task did not succeed, and
                            # every stage below it must be told (issue #82).
                            self.mark_task_failed_in_post_processing('post_process_file', e)
                        try:
                            # Write source and destination data to historic log
                            self.write_history_log()
                        except Exception as e:
                            self._log("Exception in writing history log", message2=str(e), level="exception")
                        try:
                            # Record the file as done so that the next library scan does not
                            # offer it back to the plugins (issue #33)
                            self.record_completed_file()
                        except Exception as e:
                            self._log("Exception in recording completed file state",
                                      message2=str(e), level="exception")
                        try:
                            # Ask whether the task actually achieved anything: does the
                            # delivered file still match the library's criteria? (issue #34)
                            # Runs after the done-state is recorded, on purpose - a
                            # non-converged file is reported, never re-queued.
                            self.run_convergence_check()
                        except Exception as e:
                            self._log("Exception in running the convergence check",
                                      message2=str(e), level="exception")
                        try:
                            # Commit task metadata to database after all plugin runners
                            self.commit_task_metadata()
                        except Exception as e:
                            self._log("Exception in committing task metadata", message2=str(e), level="exception")
                        try:
                            # Remove file from task queue
                            self.current_task.delete()
                        except Exception as e:
                            self._log("Exception in removing task from task list", message2=str(e), level="exception")
                    else:
                        try:
                            # Post processes the remote converted file (return it to original directory etc.)
                            self.post_process_remote_file()
                        except Exception as e:
                            self._log("Exception in post-processing remote task file",
                                      message2=str(e), level="exception")
                            # Same rule as the local branch: the delivery
                            # stage did not finish, so 'task_success' in the
                            # data.json this task hands back must not say it
                            # did (issue #82).
                            self.mark_task_failed_in_post_processing('post_process_remote_file', e)
                        try:
                            # Write source and destination data to historic log
                            self.dump_history_log()
                        except Exception as e:
                            self._log("Exception in dumping history log for remote task",
                                      message2=str(e), level="exception")
                        try:
                            # Update the task status to 'complete'
                            self.current_task.set_status('complete')
                        except Exception as e:
                            self._log("Exception in marking remote task as complete",
                                      message2=str(e), level="exception")

        self._log("Leaving PostProcessor Monitor loop...")

    def mark_task_failed_in_post_processing(self, stage, exception):
        """
        A post-processing stage raised. Record that the task failed.

        Trawlarr fork addition (see issue #82).

        WHAT THIS FIXES
        ---------------
        run() wraps every stage in a try/except that logs and moves to the
        next one. So a task whose delivery blew up half way through went on to
        write itself into history as a SUCCESS, and (before #79 and #86) to
        record its source file as done. A file the pipeline never delivered
        was signed off, removed from every future library scan, and nothing
        the operator can see said otherwise.

        Marking the task failed is enough to fix all of that, because the
        stages that follow already ask: record_completed_file() and
        run_convergence_check() both return early for an unsuccessful task,
        and write_history_log() records the failure and raises the "new failed
        task" notification. This method exists so that the answer they get is
        the true one.

        WHY THE LOOP STILL CONTINUES
        ---------------------------
        The remaining stages are not "more work on a task that failed", they
        are how the failure gets recorded and how the task leaves the queue.
        Aborting the loop instead would leave the row in the 'processed' state
        that run() selects on, and the post-processor would pick the same task
        up again immediately, forever.

        WHICH STAGES CALL THIS, AND WHICH DELIBERATELY DO NOT
        ----------------------------------------------------
        Only the delivery stages - post_process_file() and
        post_process_remote_file(). They are the ones whose failure means the
        success was never established.

        The bookkeeping stages after them (write_history_log,
        record_completed_file, commit_task_metadata, delete) stay as they are,
        logged and stepped over, because their failure does not make the
        delivery untrue and marking a correctly delivered file's task as
        failed would blacklist a good file from ever being queued again via
        FileTest.file_failed_in_history(). The rule is not "any exception
        fails the task"; it is "nothing may record an outcome an earlier stage
        did not establish".

        Never raises: this runs from an exception handler.

        :param stage:      name of the stage that raised
        :param exception:  what it raised
        :return: True when the task was successfully marked failed
        """
        marked = False
        try:
            self.current_task.set_success(False)
            marked = True
        except Exception as e:
            # Nothing else can be done from here, but this must be loud: the
            # task is about to be written to history as a success it did not
            # earn, which is the exact outcome this method exists to prevent.
            self._log("Unable to mark task as failed after post-processing stage '{}' raised".format(stage),
                      message2=str(e), level="exception")

        try:
            # Do not overwrite a more specific reason the worker already
            # recorded; a post-processing blow-up is often the consequence of
            # it rather than the cause.
            taskfailure.record(
                self.current_task.get_task_id(),
                taskfailure.CATEGORY_POSTPROCESSOR_ERROR,
                "Post-processing stage '{}' raised {}: {}".format(stage, type(exception).__name__, exception))
        except Exception as e:
            self._log("Unable to record the failure reason for post-processing stage '{}'".format(stage),
                      message2=str(e), level="exception")

        return marked

    def system_configuration_is_valid(self):
        """
        Check and ensure the system configuration is correct for running

        :return:
        """
        valid = True
        plugin_handler = PluginsHandler()
        if plugin_handler.get_incompatible_enabled_plugins():
            valid = False
        if not Library.within_library_count_limits():
            valid = False
        return valid

    def post_process_file(self):
        # Init plugins handler
        plugin_handler = PluginsHandler()

        # Read current task data
        # task_data = self.current_task.get_task_data()
        library_id = self.current_task.get_task_library_id()
        cache_path = self.current_task.get_cache_path()
        source_data = self.current_task.get_source_data()
        destination_data = self.current_task.get_destination_data()
        # Move file back to original folder and remove source
        file_move_processes_success = True
        # Create a list for filling with destination paths
        destination_files = []

        # Sanity-check the output before it is allowed anywhere near the
        # library. A failure marks the task failed, which skips the file
        # movement below and leaves the original file untouched.
        if self.current_task.task.success:
            self.run_output_sanity_checks(source_data, destination_data, cache_path)

        if self.current_task.task.success:
            # Run a postprocess file movement on the cache file for each plugin that configures it

            # Fetch all 'postprocessor.file_move' plugin modules
            plugin_modules = plugin_handler.get_enabled_plugin_modules_by_type('postprocessor.file_move',
                                                                               library_id=library_id)

            # Check if the source file needs to be removed by default (only if it does not match the destination file)
            remove_source_file = False
            if source_data['abspath'] != destination_data['abspath']:
                remove_source_file = True

            # Set initial data (some fields will be overwritten further down)
            # - 'library_id'                - The library ID for this task
            # - 'source_data'               - Dictionary of data pertaining to the source file
            # - 'remove_source_file'        - True to remove the original file (default is True if file name has changed)
            # - 'copy_file'                 - True to run a plugin initiated file copy (default is False unless the plugin says otherwise)
            # - 'file_in'                   - Source path to copy from (if 'copy_file' is True)
            # - 'file_out'                  - Destination path to copy to (if 'copy_file' is True)
            # - 'run_default_file_copy'     - Prevent the final Unmanic post-process file movement (if different from the original file name)
            data = {
                'library_id':            library_id,
                'task_id':               self.current_task.get_task_id(),
                'source_data':           None,
                'remove_source_file':    remove_source_file,
                'copy_file':             None,
                'file_in':               None,
                'file_out':              None,
                'run_default_file_copy': True,
            }

            for plugin_module in plugin_modules:
                # Always set source_data to the original file's source_data
                data["source_data"] = source_data
                # Always set copy_file to False
                data["copy_file"] = False
                # Always set file in to cache path
                data["file_in"] = cache_path
                # Always set file out to destination data absolute path
                data["file_out"] = destination_data.get('abspath')

                # Run plugin to update data
                if not plugin_handler.exec_plugin_runner(data, plugin_module.get('plugin_id'), 'postprocessor.file_move'):
                    # Do not continue with this plugin module's loop
                    continue

                if data.get('copy_file'):
                    # Copy the file
                    file_in = os.path.abspath(data.get('file_in'))
                    file_out = os.path.abspath(data.get('file_out'))
                    if not self.__copy_file(file_in, file_out, destination_files, plugin_module.get('plugin_id')):
                        file_move_processes_success = False
                else:
                    self._log("Plugin did not request a file copy ({})".format(
                        plugin_module.get('plugin_id')), level='debug')

            # Unmanic's default file movement process
            # Only carry out final post-processor file moments if all others were successful
            if file_move_processes_success and data.get('run_default_file_copy'):
                # Run the default post-process file movement.
                # This will always move the file back to the original location.
                # If that original location is the same file name, it will overwrite the original file.
                if destination_data.get('abspath') == source_data.get('abspath'):
                    # Only run the final file copy to overwrite the source file if the remove_source_file flag was never set
                    # The remove_source_file flag will remove the source file in later lines after this copy operation,
                    #   so if we did copy the file here, it would be a waste of time
                    if not data.get('remove_source_file'):
                        if not self.__copy_file(cache_path, destination_data.get('abspath'), destination_files, 'DEFAULT',
                                                move=True):
                            file_move_processes_success = False
                elif not self.__copy_file(cache_path, destination_data.get('abspath'), destination_files, 'DEFAULT',
                                          move=True):
                    file_move_processes_success = False

            # Source file removal process
            # Only run if all final post-processor file moments were successful
            if file_move_processes_success:
                # Check if the remove source flag is still True after all plugins have run. If so, we will remove the source file
                if data.get('remove_source_file'):
                    # Only carry out a source removal if the file exists and the final copy was also successful
                    if file_move_processes_success and os.path.exists(source_data.get('abspath')):
                        self._log("Removing source: {}".format(source_data.get('abspath')))
                        os.remove(source_data.get('abspath'))
                    else:
                        self._log("Keeping source file '{}'. Not all postprocessor file movement functions completed.".format(
                            source_data.get('abspath')), level="warning")

            # Log a final error if not all file moments were successful
            if not file_move_processes_success:
                self._log(
                    "Error while running postprocessor file movement on file '{}'. Not all postprocessor file movement functions completed.".format(
                        cache_path), level="error")

        else:
            self._log("Skipping file movement post-processor as the task was not successful '{}'".format(cache_path),
                      level='warning')

        # Fetch all 'postprocessor.task_result' plugin modules
        plugin_modules = plugin_handler.get_enabled_plugin_modules_by_type(
            'postprocessor.task_result', library_id=library_id)

        for plugin_module in plugin_modules:
            data = {
                'library_id':                  library_id,
                "task_id":                     self.current_task.get_task_id(),
                "task_type":                   self.current_task.get_task_type(),
                'final_cache_path':            cache_path,
                'task_processing_success':     self.current_task.get_task_success(),
                'file_move_processes_success': file_move_processes_success,
                'destination_files':           destination_files,
                'source_data':                 source_data,
                'start_time':                  self.current_task.get_start_time(),
                'finish_time':                 self.current_task.get_finish_time(),
            }

            # Run plugin to update data
            if not plugin_handler.exec_plugin_runner(data, plugin_module.get('plugin_id'), 'postprocessor.task_result'):
                # Do not continue with this plugin module's loop
                continue

        # Cleanup cache files
        self.__cleanup_cache_files(cache_path)
        self._last_destination_files = destination_files
        self._last_file_move_processes_success = file_move_processes_success

    def run_output_sanity_checks(self, source_data, destination_data, cache_path):
        """
        Compare the worker's output against the source file and refuse to
        deliver it if the comparison says the pipeline is damaging the file.

        See trawlarr/libs/sanity.py for what is checked and why. In short: a
        plugin bug that appends an audio track on every scan looks exactly like
        twenty-five successful tasks. This is the point where somebody finally
        looks at the result.

        On failure the task is marked failed. That is the whole enforcement
        mechanism, and it is deliberately reusing machinery that already
        exists:
          - the caller skips file movement for failed tasks, so the output is
            discarded and the original file is left exactly as it was;
          - write_history_log() raises the "new failed task" UI notification;
          - FileTest.file_failed_in_history() will not queue a file that has
            failed, so the next library scan does not start the cycle again.
        Halting is the point. Compounding the damage for another twenty-four
        passes is the alternative.

        The reason is appended to the task log so it is visible in the UI next
        to the failed task, not just in the daemon log.

        Fork addition; see issue #35.

        :param source_data:
        :param destination_data:
        :param cache_path:
        :return: True when the checks passed (or could not be run)
        """
        source_abspath = (source_data or {}).get('abspath')
        destination_abspath = (destination_data or {}).get('abspath')
        try:
            check_settings = sanity.SanityCheckSettings.from_settings(self.settings)
            if not check_settings.enabled:
                return True
            result = sanity.check_task_output(
                source_path=source_abspath,
                output_path=cache_path,
                settings=check_settings,
                task_id=self.current_task.get_task_id(),
            )
        except Exception as e:
            # A broken checker must not be able to stall the pipeline, but it
            # must not be silent about it either.
            self._log("Exception while running output sanity checks on '{}'".format(cache_path),
                      message2=str(e), level="exception")
            return True

        # Keep the output probe for the convergence check. It was taken of the
        # cache file, which the delivery below moves byte-for-byte into the
        # library, so it describes the delivered file too - but only if nothing
        # changed it on the way, which is what the recorded size is checked
        # against later.
        self._last_output_probe = getattr(result, 'output_probe', None)
        self._last_output_size = (result.state or {}).get('last_size')

        if not result.checked:
            self._log("Output sanity checks could not be run for '{}' (no usable probe data)".format(cache_path),
                      level='debug')
            return True

        if not result.failed:
            # Record the streak state against the path the file is about to
            # take, so the next task on it can see what this one did.
            sanity.save_state(destination_abspath or source_abspath, result.state,
                              previous_abspath=source_abspath)
            return True

        for failure in result.failures:
            self._log("Output sanity check '{}' failed for '{}': {}".format(
                failure.get('id'), source_abspath, failure.get('message')), level="error")

        # The output is being discarded, so the file on disk is still the
        # source. Keep the streak against the source path.
        sanity.save_state(source_abspath, result.state)

        try:
            self.current_task.save_command_log(result.report())
        except Exception as e:
            self._log("Unable to append sanity check report to task log", message2=str(e), level="warning")

        # Persist the reason alongside the failed task (issue #25). Forced,
        # because the worker may already have recorded a reason for a task it
        # nonetheless returned as successful; the sanity refusal is what
        # actually decided this task's outcome.
        taskfailure.record(
            self.current_task.get_task_id(),
            taskfailure.CATEGORY_SANITY_CHECK,
            "; ".join(f.get('message') for f in result.failures if f.get('message')),
            overwrite=True)
        try:
            self.current_task.set_success(False)
        except Exception as e:
            # If we cannot mark the task failed we cannot stop the file
            # movement, and a silent delivery is the outcome this whole check
            # exists to prevent. Raise so the caller's handler logs it.
            self._log("Unable to mark task as failed after sanity check failure",
                      message2=str(e), level="exception")
            raise

        TrawlarrLogging.data(
            "task_sanity_check_failed",
            data_search_key=source_abspath,
            task_id=self.current_task.get_task_id(),
            source_path=source_abspath,
            cache_path=cache_path,
            failed_checks=[f.get('id') for f in result.failures],
            messages=[f.get('message') for f in result.failures],
        )
        return False

    def _reusable_output_probe(self, destination_abspath):
        """
        The probe the sanity checks already took, if it still describes the
        file now sitting at `destination_abspath`.

        Re-probing costs a full ffprobe of a freshly written media file for
        every completed task, and the sanity checks have just paid for exactly
        that on the cache file that the delivery then moved into place. Reusing
        it is the difference between one probe per task and two.

        But handing a plugin the wrong file's probe is precisely how a check
        turns into a rubber stamp - sanity.probe_from_task_data_store() has the
        same trap and guards it the same way. A `postprocessor.file_move`
        plugin is free to transform the file on its way into the library, so
        the probe is only offered when the delivered file is still exactly the
        size the probed cache file was. Where it is not, None is returned and
        the file-test plugins probe the real file themselves; the check stays
        correct and merely costs more.

        :param destination_abspath:
        :return: probe dict or None
        """
        probe = self._last_output_probe
        if not probe or not destination_abspath or self._last_output_size is None:
            return None
        try:
            if os.path.getsize(destination_abspath) != self._last_output_size:
                return None
        except OSError:
            return None
        # The cache path is gone; say so, or a plugin that reads
        # format.filename is told about a file that no longer exists.
        probe = dict(probe)
        probe['format'] = dict(probe.get('format') or {})
        probe['format']['filename'] = destination_abspath
        return probe

    def run_convergence_check(self):
        """
        Did the task actually achieve anything?

        Re-runs the library's file-test PLUGINS against each file the task
        delivered. If they still want the file, the task completed without
        converging it, which is what issue #34 calls the single highest-value
        missing capability: files "landed in completed-task history [...] with
        no signal from the queue that anything was wrong".

        Three things this deliberately does not do:

          * it does not re-queue the file. record_completed_file() has just run
            and the file remains recorded as done (issue #33). Silent re-queue
            is the reprocess loop, which is the disease and not the cure.
          * it does not mark the task failed. The task succeeded; a
            non-converged file is a configuration or plugin problem, and
            filing it under the #25 failure categories would blacklist a good
            file and corrupt the failure health view. It gets its own state.
          * it does not fire on a library that has no file-test plugins. There
            are no criteria to converge against, so there is no finding to
            report - see trawlarr/libs/convergence.py.

        Runs after delivery, against the file's real library path, because
        that is the file the next library scan will see. Never raises.

        Fork addition; see issue #34.

        :return: list of (path, ConvergenceResult) for every file evaluated
        """
        evaluated = []
        try:
            if not self.current_task.get_task_success():
                return evaluated
            if not getattr(self.settings, 'get_convergence_check_enabled', None) or \
                    not self.settings.get_convergence_check_enabled():
                return evaluated

            library_id = self.current_task.get_task_library_id()
            task_id = self.current_task.get_task_id()
            destination_files = [p for p in (self._last_destination_files or []) if p]
            if not destination_files:
                # Nothing was delivered. record_completed_file() has already
                # complained about that; there is no output to evaluate.
                return evaluated

            # One FileTest for the whole task: constructing it loads the
            # library's plugin modules, and a task can deliver several files.
            from trawlarr.libs.filetest import FileTest
            file_test = FileTest(library_id)

            non_converged = []
            for destination_abspath in destination_files:
                # Per file, so that one unreadable delivery cannot stop the
                # others from being checked. A task can deliver several files
                # and a partial answer is still an answer.
                try:
                    probe = self._reusable_output_probe(destination_abspath)
                    result = convergence.evaluate_path(
                        destination_abspath,
                        library_id,
                        shared_info={'ffprobe': probe} if probe else None,
                        file_test=file_test,
                    )
                    evaluated.append((destination_abspath, result))

                    if result.converged:
                        convergence.clear(destination_abspath)
                        continue
                    if not result.evaluated:
                        self._log("Convergence could not be evaluated for '{}': {}".format(
                            destination_abspath, result.message), level='debug')
                        continue

                    occurrences = convergence.record(destination_abspath, library_id=library_id,
                                                     task_id=task_id, result=result)
                    non_converged.append((destination_abspath, result, occurrences))
                    if occurrences >= convergence.REPEAT_LIMIT:
                        self._log(
                            "{} This file has now completed {} times without converging, which is a configuration "
                            "or plugin bug rather than a one-off. It has NOT been re-queued.".format(
                                result.message, occurrences), level='error')
                    else:
                        self._log("{} It has NOT been re-queued.".format(result.message), level='warning')

                    # 'detail', not 'message': the latter is a reserved
                    # LogRecord attribute and logging refuses to overwrite it.
                    TrawlarrLogging.data(
                        "file_did_not_converge",
                        data_search_key=destination_abspath,
                        task_id=task_id,
                        library_id=library_id,
                        file_path=destination_abspath,
                        plugin_id=result.plugin_id,
                        plugin_name=result.plugin_name,
                        occurrences=occurrences,
                        detail=result.message,
                    )
                except Exception as e:
                    self._log("Exception while checking convergence of '{}'".format(destination_abspath),
                              message2=str(e), level="exception")

            if non_converged:
                self._raise_non_convergence_notification()
        except Exception as e:
            # A convergence check that raises must not damage a task that has
            # already been delivered successfully.
            self._log("Exception while running the convergence check", message2=str(e), level="exception")
        return evaluated

    @staticmethod
    def _raise_non_convergence_notification():
        """
        One aggregate notification, not one per file.

        A badly configured library can produce a non-converged file on every
        single task. Queueing a notification for each of them would bury every
        other notification the user has, which is how a warning stops being
        read. The notification queue de-duplicates on uuid, so this stays a
        single standing item that points at the health view.

        :return:
        """
        Notifications().add(
            {
                'uuid':       'filesDidNotConverge',
                'type':       'warning',
                'icon':       'sync_problem',
                'label':      'nonConvergedFilesLabel',
                'message':    ('One or more files still match their library\'s criteria after their task '
                               'completed. They have not been re-queued.'),
                'navigation': {
                    'push': '/ui/dashboard',
                },
            })

    def post_process_remote_file(self):
        """
        Process remote files.
        Remote files are not processed by plugins. They are just sent back to the OG installation and then the cache files are cleaned up here.
        A remote file's source_data will be the download path where this installation initial received and stored it.

        TODO: Should we move remote tasks to a permanent download location within the cache path? Possibly not...

        :return:
        """
        # Read current task data
        cache_path = self.current_task.get_cache_path()
        source_data = self.current_task.get_source_data()
        destination_data = self.current_task.get_destination_data()
        def_cache_path = self.settings.get_cache_path()

        # ``remove_source_file`` is True when the destination is itself inside
        # the cache (a fully cache-internal handoff). False when the destination
        # lives in the user's library, in which case the staged source download
        # must be preserved.
        remove_source_file = def_cache_path in destination_data['abspath']

        self._log(f"Cache path: {def_cache_path}", level='debug')
        self._log(
            f"Remote source: {source_data['abspath']}, destination file: {destination_data['abspath']}.", level='debug')
        self._log(f"Task cache path: {cache_path}", level='debug')

        # Attempt to deliver the processed file. Source removal is deferred until
        # a delivery has succeeded so a failed copy can never leave us with
        # neither the source nor the destination.
        move_success = False
        final_destination = None

        if not os.path.exists(cache_path):
            self._log(
                "Final cache file '{}' does not exist; nothing to deliver.".format(cache_path),
                level="warning")
        elif remove_source_file:
            if self.__copy_file(cache_path, destination_data.get('abspath'), [], 'DEFAULT', move=True):
                move_success = True
                final_destination = destination_data.get('abspath')
            else:
                self._log(
                    "Failed to move processed file '{}' to '{}'. Source will be retained.".format(
                        cache_path, destination_data.get('abspath')),
                    level="error")
        else:
            # Destination is in the library. Stage into a library-adjacent
            # directory first so the user can move the file into place; if the
            # library is unavailable, fall back to a cache-side staging dir.
            random_string = '{}-{}'.format(common.random_string(), int(time.time()))
            library_tdir = os.path.join(os.path.dirname(source_data.get('abspath')),
                                        "unmanic_remote_pending_library-" + random_string)
            cache_tdir = os.path.join(def_cache_path, "unmanic_remote_pending_library-" + random_string)

            try:
                os.mkdir(library_tdir)
                library_target = os.path.join(library_tdir, os.path.basename(cache_path))
                if self.__copy_file(cache_path, library_target, [], 'DEFAULT', move=True):
                    move_success = True
                    final_destination = library_target
                else:
                    raise Exception("Failed to copy back to library staging dir")
            except Exception as e:
                self._log(
                    "Library-side staging failed ({}); falling back to cache.".format(e),
                    level="warning")
                try:
                    os.mkdir(cache_tdir)
                    cache_target = os.path.join(cache_tdir, os.path.basename(cache_path))
                    if self.__copy_file(cache_path, cache_target, [], 'DEFAULT', move=True):
                        move_success = True
                        final_destination = cache_target
                    else:
                        self._log(
                            "Cache-side staging also failed for '{}'.".format(cache_path),
                            level="error")
                except Exception as e2:
                    self._log(
                        "Cache-side staging directory could not be created ({}).".format(e2),
                        level="error")

        # Now that the delivery has either succeeded or definitively failed it
        # is safe to drop the source. Removing it earlier would risk losing the
        # only remaining copy of the file.
        if not os.path.exists(source_data.get('abspath')):
            self._log("Remote source file '{}' does not exist!".format(source_data.get('abspath')), level="warning")
        elif not remove_source_file:
            self._log("Keep remote source: {}, remote file source is in library and not cache.".format(
                source_data.get('abspath')))
        elif move_success:
            self._log("Removing remote source: {}".format(source_data.get('abspath')))
            try:
                os.remove(source_data.get('abspath'))
            except OSError as e:
                self._log(
                    "Failed to remove remote source '{}': {}".format(source_data.get('abspath'), e),
                    level="error")
        else:
            self._log(
                "Retaining remote source '{}' because the processed file was not delivered.".format(
                    source_data.get('abspath')),
                level="warning")

        # Cleanup cache files
        self.__cleanup_cache_files(cache_path)
        self._last_file_move_processes_success = move_success
        self._last_destination_files = [final_destination] if final_destination else []

        # Modify the task abspath - this may be different now
        if final_destination:
            self.current_task.modify_path(final_destination)

    def __cleanup_cache_files(self, cache_path):
        """
        Remove cache files and the cache directory
        This ensures we are not simply blindly removing a whole directory.
        It ensures were are in-fact only deleting this task's cache files.

        :param cache_path:
        :return:
        """
        task_cache_directory = os.path.dirname(cache_path)
        if os.path.exists(task_cache_directory) and "unmanic_file_conversion" in task_cache_directory:
            self._log("Removing task cache directory '{}'".format(task_cache_directory))
            try:
                shutil.rmtree(task_cache_directory)
            except Exception as e:
                self._log("Exception while clearing cache path '{}'".format(str(e)), level='error')

    def __copy_file(self, file_in, file_out, destination_files, plugin_id, move=False):
        if move:
            self._log("Move file triggered by ({}) {} --> {}".format(plugin_id, file_in, file_out))
        else:
            self._log("Copy file triggered by ({}) {} --> {}".format(plugin_id, file_in, file_out))

        try:
            # Ensure the src and dst are not the same file
            if os.path.exists(file_out) and os.path.samefile(file_in, file_out):
                self._log("The file_in and file_out path are the same file. Nothing will be done! '{}'".format(file_in),
                          level="warning")
                return False

            # Get a checksum prior to copy
            if not os.path.exists(file_in):
                self._log("The file_in path does not exist! '{}'".format(file_in), level="warning")
                self.event.wait(1)
            self._log("Fetching checksum of source file '{}'.".format(file_in), level='debug')

            # Use a '.part' suffix for the file movement, then rename it after
            part_file_out = os.path.join("{}.unmanic.part".format(file_out))

            # Carry out the file movement
            if move:
                self._log("Moving file '{}' --> '{}'.".format(file_in, part_file_out), level='debug')
                if os.path.exists(part_file_out):
                    os.remove(part_file_out)
                shutil.move(file_in, part_file_out, copy_function=shutil.copyfile)
            else:
                self._log("Copying file '{}' --> '{}'.".format(file_in, part_file_out), level='debug')
                shutil.copyfile(file_in, part_file_out)

            # Remove dest file if it already exists (required only for moves)
            if os.path.exists(file_out):
                self._log("The file_out path already exists. Removing file '{}'".format(file_out), level="debug")
                os.remove(file_out)

            # Move file from part to final destination
            self._log("Renaming file '{}' --> '{}'.".format(part_file_out, file_out), level='debug')
            shutil.move(part_file_out, file_out, copy_function=shutil.copyfile)
            # Write final path to destination_files list
            destination_files.append(file_out)
            # Mark move process a success
            return True
        except Exception as e:
            self._log("Exception while copying file {} to {}:".format(file_in, file_out),
                      message2=str(e), level="exception")
            file_move_processes_success = False

        return file_move_processes_success

    def write_history_log(self):
        """
        Record task history

        :return:
        """
        self._log("Writing task history log.", level='debug')
        history_logging = history.History()
        task_dump = self.current_task.task_dump()
        destination_data = self.current_task.get_destination_data()
        source_data = self.current_task.get_source_data()

        # If task fails, the add a notification that a task has failed
        if not self.current_task.task.success:
            notifications = Notifications()
            notifications.add(
                {
                    'uuid':       'newFailedTask',
                    'type':       'error',
                    'icon':       'report',
                    'label':      'failedTaskLabel',
                    'message':    'You have a new failed task in your completed tasks list',
                    'navigation': {
                        'push':   '/ui/dashboard',
                        'events': [
                            'completedTasksShowFailed',
                        ],
                    },
                })

        self._log_completed_task_data(task_dump, source_data, destination_data)

        task_history = {
            'task_label':          task_dump.get('task_label', ''),
            'abspath':             task_dump.get('abspath', ''),
            'task_success':        task_dump.get('task_success', False),
            'start_time':          task_dump.get('start_time', ''),
            'finish_time':         task_dump.get('finish_time', ''),
            'processed_by_worker': task_dump.get('processed_by_worker', ''),
            'log':                 task_dump.get('log', ''),
        }
        # Durable failure state (issue #25). This is the point where the
        # in-memory record built during processing - by the stall detector,
        # a failed plugin, a non-zero command or the sanity checks - is
        # written to the database, which is what makes it survive a restart.
        task_history.update(
            taskfailure.build_history_fields(
                self.current_task.get_task_id(),
                task_history['task_success'],
                task_history['abspath'],
            )
        )
        if not task_history['task_success']:
            self._log("Task failed [{}]: {}".format(task_history.get('failure_category'),
                                                    task_history.get('failure_message')), level='error')

        history_logging.save_task_history(task_history)

        # Execute event plugin runners
        plugin_handler = PluginsHandler()
        plugin_handler.run_event_plugins_for_plugin_type('events.postprocessor_complete', {
            'library_id':          self.current_task.get_task_library_id(),
            'task_id':             self.current_task.get_task_id(),
            'task_type':           self.current_task.get_task_type(),
            'source_data':         self.current_task.get_source_data(),
            'destination_data':    self.current_task.get_destination_data(),
            'destination_files':   list(self._last_destination_files or []),
            'task_success':        task_dump.get('task_success', False),
            'file_move_processes_success': self._last_file_move_processes_success,
            'start_time':          task_dump.get('start_time', ''),
            'finish_time':         task_dump.get('finish_time', ''),
            'processed_by_worker': task_dump.get('processed_by_worker', ''),
            'log':                 task_dump.get('log', ''),
        })

    def record_completed_file(self):
        """
        Record the native "this file is done" state for a successful task.

        This is the write side of issue #33. Its counterpart is
        FileTest.file_already_completed_successfully(), which is what stops the
        file being picked up again by the next library scan.

        Only successful tasks are recorded. A failed task is already terminal
        by a different route - FileTest.file_failed_in_history() - and a task
        whose output was rejected by the sanity checks has been marked failed
        by the time we get here, so its (untouched) source file is correctly
        never recorded as done.

        The worker succeeding is not enough on its own (issue #86). If the
        post-processor's file movement failed, the file sitting at the
        destination path is the ORIGINAL, unprocessed one, and recording it as
        done would sign off work that was never delivered and remove the file
        from every future library scan. So the file movement must have reported
        success too.

        :return: list of paths recorded
        """
        if not self.current_task.get_task_success():
            return []

        if not self._last_file_move_processes_success:
            self._log("Post-processor file movement did not complete successfully. "
                      "Not recording this file as done - the file in the library has not been replaced.",
                      level="warning")
            return []

        source_data = self.current_task.get_source_data() or {}
        destination_data = self.current_task.get_destination_data() or {}
        source_abspath = source_data.get('abspath')

        # Record every file the task actually delivered. A plugin flow may
        # write the result to more than one place; each of those is a library
        # file in its own right.
        destination_files = [p for p in (self._last_destination_files or []) if p]
        if not destination_files and destination_data.get('abspath'):
            destination_files = [destination_data.get('abspath')]
        if not destination_files:
            self._log("Task reported success but delivered no destination file. Nothing to record as completed.",
                      level="warning")
            return []

        library_id = self.current_task.get_task_library_id()
        task_id = self.current_task.get_task_id()

        recorded = []
        for destination_abspath in destination_files:
            if donestate.record_completion(destination_abspath, library_id=library_id, task_id=task_id):
                recorded.append(destination_abspath)

        # Where the task renamed the file (a container change, say), the path
        # it came from no longer holds a file. Drop its record so it does not
        # sit there describing something that is gone - but only once every
        # destination has been recorded, and only when the source really is
        # not one of them.
        if source_abspath and source_abspath not in destination_files:
            donestate.forget_path(source_abspath)
        self._log("Recorded completed file state for: {}".format(recorded), level='debug')
        return recorded

    def commit_task_metadata(self):
        """
        Commit task metadata after all postprocessor runners have finished.
        """
        source_data = self.current_task.get_source_data()
        destination_data = self.current_task.get_destination_data()
        task_success = self.current_task.get_task_success()
        destination_files = list(self._last_destination_files or [])
        if not destination_files and destination_data:
            destination_files = [destination_data.get('abspath')]
        committed = TrawlarrFileMetadata.commit_task(
            task_id=self.current_task.get_task_id(),
            task_success=task_success,
            source_path=source_data.get('abspath'),
            destination_paths=destination_files,
        )
        if committed:
            self._log("Committed file metadata entries: {}".format(committed), level='debug')
        return committed

    def dump_history_log(self):
        self._log("Dumping remote task history log.", level='debug')
        task_dump = self.current_task.task_dump()
        destination_data = self.current_task.get_destination_data()

        # Dump history log & task state as metadata in the file's path
        tasks_data_file = os.path.join(os.path.dirname(destination_data.get('abspath')), 'data.json')
        task_state = TaskDataStore.export_task_state(self.current_task.get_task_id())
        result = common.json_dump_to_file(
            {
                'task_label':          task_dump.get('task_label', ''),
                'abspath':             task_dump.get('abspath', ''),
                'task_success':        task_dump.get('task_success', False),
                'start_time':          task_dump.get('start_time', ''),
                'finish_time':         task_dump.get('finish_time', ''),
                'processed_by_worker': task_dump.get('processed_by_worker', ''),
                'log':                 task_dump.get('log', ''),
                'checksum':            'UNKNOWN',
                'task_state':          task_state,
            }, tasks_data_file)
        if not result['success']:
            for message in result['errors']:
                self._log("Exception:", message2=str(message), level="exception")
            raise Exception("Exception in dumping completed task data to file")

    def _log_completed_task_data(self, task_dump, source_data, destination_data):
        status = "success" if task_dump.get('task_success', False) else "failed"
        start_time = task_dump.get('start_time', '')
        finish_time = task_dump.get('finish_time', '')
        command_error_log_tail = ""
        if status != "success":
            task_log = task_dump.get('log', '')
            if task_log:
                command_error_log_tail = "\n".join(task_log.splitlines()[-20:])
        try:
            library_id = self.current_task.get_task_library_id()
            library_name = self.current_task.get_task_library_name()
        except Exception:
            library_id = None
            library_name = None

        TrawlarrLogging.data(
            "completed_task",
            data_search_key=f"{library_id} | {finish_time} | {source_data.get('abspath', '')}",
            task_id=self.current_task.get_task_id(),
            task_type=self.current_task.get_task_type(),
            library_id=library_id,
            library_name=library_name,
            status=status,
            start_time=start_time,
            finish_time=finish_time,
            source_file=source_data.get('basename', ''),
            source_path=source_data.get('abspath', ''),
            dest_file=destination_data.get('basename', ''),
            dest_path=destination_data.get('abspath', ''),
            command_error_log_tail=command_error_log_tail,
        )
