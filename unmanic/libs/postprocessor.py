#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    unmanic.postprocessor.py
 
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
import errno
import hashlib
import json
import os
import shutil
import threading
import time
import uuid

from unmanic import config
from unmanic.libs import common, history
from unmanic.libs.task import Task, TaskDataStore, TaskFailureCategory
from unmanic.libs.frontend_push_messages import FrontendPushMessages
from unmanic.libs.library import Library
from unmanic.libs.logs import UnmanicLogging
from unmanic.libs.metadata import UnmanicFileMetadata
from unmanic.libs.notifications import Notifications
from unmanic.libs.plugins import PluginsHandler
from unmanic.libs.unmodels.tasks import Tasks

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
    HISTORY_RETRY_DELAY_SECONDS = 5
    CHECKPOINT_RECONCILE_DELAY_SECONDS = 2

    def __init__(self, data_queues, task_queue, event):
        super(PostProcessor, self).__init__(name='PostProcessor')
        self.logger = UnmanicLogging.get_logger(name=__class__.__name__)
        self.event = event
        self.data_queues = data_queues
        self.settings = config.Config()
        self.task_queue = task_queue
        self.abort_flag = threading.Event()
        self.current_task = None
        self._last_destination_files = []
        self._last_file_move_processes_success = False
        self.ffmpeg = None
        self._history_retry_not_before = 0
        self._checkpoint_quarantine = {}
        self._remote_delivery_quarantine = {}
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

            while not self.abort_flag.is_set():
                self.current_task = self._get_next_postprocessor_task()
                if not self.current_task:
                    break
                self.event.wait(.2)
                task_status = getattr(self.current_task.task, 'status', None)
                if not isinstance(task_status, str):
                    task_status = 'processed'
                if self.current_task.get_task_type() == 'remote':
                    if not self._process_remote_delivery(task_status):
                        self._set_retry_delay()
                        break
                    continue
                if task_status == 'deletion_pending':
                    if not self._remove_completed_local_task():
                        self._history_retry_not_before = (
                            time.monotonic() + self.HISTORY_RETRY_DELAY_SECONDS)
                        break
                    continue
                if task_status == 'completion_dispatching':
                    if not self._finish_claimed_completion():
                        self._history_retry_not_before = (
                            time.monotonic() + self.HISTORY_RETRY_DELAY_SECONDS)
                        break
                    continue
                if task_status in ('postprocessing', 'checkpoint_pending'):
                    if not self._recover_interrupted_postprocessing(task_status):
                        self._set_retry_delay()
                        break
                    continue
                history_retry = task_status == 'history_pending'
                if history_retry:
                    if not self._persist_local_history_and_remove_task():
                        self._history_retry_not_before = (
                            time.monotonic() + self.HISTORY_RETRY_DELAY_SECONDS)
                        break
                    continue
                completion_payload_valid = False
                if (task_status == 'processed'
                        and self.current_task.get_task_type() == 'local'):
                    try:
                        completion_payload_valid = (
                            self.current_task.has_valid_completion_payload() is True)
                    except Exception as e:
                        self._log("Exception in checking completion recovery data",
                                  message2=str(e), level="exception")
                        self._history_retry_not_before = (
                            time.monotonic() + self.HISTORY_RETRY_DELAY_SECONDS)
                        break
                if completion_payload_valid:
                    try:
                        if not self.current_task.transition_status(
                                'processed', 'history_pending'):
                            raise Exception(
                                'Completion payload recovery lost task ownership.')
                    except Exception as e:
                        self._log("Exception in recovering completion checkpoint",
                                  message2=str(e), level="exception")
                        self._history_retry_not_before = (
                            time.monotonic() + self.HISTORY_RETRY_DELAY_SECONDS)
                        break
                    if not self._persist_local_history_and_remove_task():
                        self._history_retry_not_before = (
                            time.monotonic() + self.HISTORY_RETRY_DELAY_SECONDS)
                        break
                    continue

                if (task_status == 'processed'
                        and self.current_task.get_task_type() == 'local'):
                    try:
                        if not self.current_task.transition_status(
                                'processed', 'postprocessing'):
                            continue
                        if not self.current_task.transition_status(
                                'postprocessing', 'checkpoint_pending'):
                            raise Exception(
                                'Unable to durably enter the file-operation checkpoint.')
                    except Exception as e:
                        self._log("Exception in claiming local postprocessing",
                                  message2=str(e), level="exception")
                        self._quarantine_checkpoint(pre_operations=True)
                        self._set_retry_delay()
                        break

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
                try:
                    # Post processes the converted file (return it to original directory etc.)
                    self.post_process_file()
                    if (self.current_task.get_task_success()
                            and not self._last_file_move_processes_success):
                        self.current_task.set_success(False)
                        self.current_task.set_failure(
                            TaskFailureCategory.POSTPROCESSING,
                            'The processed file could not be safely moved into place.',
                        )
                except Exception as e:
                    self._log("Exception in post-processing local task file",
                              message2=str(e), level="exception")
                    self.current_task.set_success(False)
                    self.current_task.set_failure(
                        TaskFailureCategory.INTERNAL,
                        'An unexpected post-processing error occurred. Check application logs for details.',
                    )
                try:
                    # Commit task metadata with the file-processing phase.
                    self.commit_task_metadata()
                except Exception as e:
                    self._log("Exception in committing task metadata", message2=str(e), level="exception")
                try:
                    # Persist the phase boundary before attempting history. A
                    # restart can now retry history without touching files.
                    self.current_task.save_completion_checkpoint(
                        self._last_destination_files,
                        self._last_file_move_processes_success,
                    )
                except Exception as e:
                    self._log("Exception in persisting completion recovery data",
                              message2=str(e), level="exception")
                    self._quarantine_checkpoint(
                        destination_files=self._last_destination_files,
                        file_move_processes_success=(
                            self._last_file_move_processes_success),
                    )
                    self._set_retry_delay()
                    break
                if not self._persist_local_history_and_remove_task():
                    self._history_retry_not_before = (
                        time.monotonic() + self.HISTORY_RETRY_DELAY_SECONDS)
                    break

        self._log("Leaving PostProcessor Monitor loop...")

    def _get_next_postprocessor_task(self):
        retry_not_before = getattr(self, '_history_retry_not_before', 0)
        if time.monotonic() < retry_not_before:
            return None
        self._reconcile_checkpoint_quarantine()
        self._reconcile_remote_delivery_quarantine()
        if getattr(self, '_checkpoint_quarantine', {}):
            return None
        if getattr(self, '_remote_delivery_quarantine', {}):
            return None
        if not self.task_queue.task_list_remote_metadata_pending_is_empty():
            return self.task_queue.get_next_remote_metadata_pending_task()
        if not self.task_queue.task_list_remote_delivery_is_empty():
            return self.task_queue.get_next_remote_delivery_task()
        if not self.task_queue.task_list_checkpoint_pending_is_empty():
            return self.task_queue.get_next_checkpoint_pending_task()
        if not self.task_queue.task_list_postprocessing_is_empty():
            return self.task_queue.get_next_postprocessing_task()
        if not self.task_queue.task_list_processed_is_empty():
            return self.task_queue.get_next_processed_tasks()
        if not self.task_queue.task_list_deletion_pending_is_empty():
            return self.task_queue.get_next_deletion_pending_task()
        if not self.task_queue.task_list_completion_dispatching_is_empty():
            return self.task_queue.get_next_completion_dispatching_task()
        if not self.task_queue.task_list_history_pending_is_empty():
            return self.task_queue.get_next_history_pending_task()
        return None

    def _set_retry_delay(self):
        self._history_retry_not_before = (
            time.monotonic() + self.HISTORY_RETRY_DELAY_SECONDS)

    def _quarantine_remote_delivery(self):
        task_id = self.current_task.get_task_id()
        quarantine = getattr(self, '_remote_delivery_quarantine', None)
        if quarantine is None:
            quarantine = {}
            self._remote_delivery_quarantine = quarantine
        quarantine[task_id] = {
            'not_before': (
                time.monotonic() + self.CHECKPOINT_RECONCILE_DELAY_SECONDS),
        }

    def _reconcile_remote_delivery_quarantine(self):
        """Resolve ambiguous queued delivery writes without moving output again."""
        quarantine = getattr(self, '_remote_delivery_quarantine', {})
        now = time.monotonic()
        for task_id, recovery in list(quarantine.items()):
            if now < recovery['not_before'] or self._queued_database_is_busy():
                continue
            recovered = self._load_task_by_id(task_id)
            if recovered is None:
                quarantine.pop(task_id, None)
                continue
            status = recovered.task.status
            try:
                if recovered.has_valid_remote_delivery_checkpoint():
                    if status in ('processed', 'remote_delivery'):
                        recovered.apply_remote_delivery_checkpoint()
                    quarantine.pop(task_id, None)
                    continue
                if recovered.has_valid_remote_delivery_claim():
                    if status == 'processed' and not recovered.transition_status(
                            'processed', 'remote_delivery'):
                        raise Exception(
                            'Unable to publish a committed remote delivery claim.')
                    quarantine.pop(task_id, None)
                    continue
                # Writes are ordered: without a claim, no delivery operation
                # could have started.
                if status in ('processed', 'remote_delivery', 'complete'):
                    quarantine.pop(task_id, None)
            except Exception as e:
                recovery['not_before'] = (
                    time.monotonic() + self.CHECKPOINT_RECONCILE_DELAY_SECONDS)
                self._log(
                    "Remote delivery checkpoint is still settling",
                    message2=str(e),
                    level="warning",
                )

    def _quarantine_checkpoint(
            self, destination_files=None, file_move_processes_success=False,
            pre_operations=False, abandoned=False):
        """Keep an ambiguous queued write away from file-operation selection."""
        task_id = self.current_task.get_task_id()
        quarantine = getattr(self, '_checkpoint_quarantine', None)
        if quarantine is None:
            quarantine = {}
            self._checkpoint_quarantine = quarantine
        quarantine[task_id] = {
            'destination_files': (
                None if destination_files is None else list(destination_files)),
            'file_move_processes_success': bool(file_move_processes_success),
            'pre_operations': bool(pre_operations),
            'abandoned': bool(abandoned),
            'not_before': (
                time.monotonic() + self.CHECKPOINT_RECONCILE_DELAY_SECONDS),
        }

    @staticmethod
    def _load_task_by_id(task_id):
        record = Tasks.get_or_none(Tasks.id == task_id)
        if record is None:
            return None
        recovered = Task()
        recovered.task = record
        return recovered

    @staticmethod
    def _queued_database_is_busy():
        database = getattr(Tasks._meta.database, 'obj', Tasks._meta.database)
        queue_size = getattr(database, 'queue_size', None)
        return callable(queue_size) and queue_size() > 0

    def _reconcile_checkpoint_quarantine(self):
        """Resolve timed-out writes without ever selecting their file work again."""
        quarantine = getattr(self, '_checkpoint_quarantine', {})
        now = time.monotonic()
        for task_id, recovery in list(quarantine.items()):
            if now < recovery['not_before'] or self._queued_database_is_busy():
                continue
            recovered = self._load_task_by_id(task_id)
            if recovered is None:
                quarantine.pop(task_id, None)
                continue
            status = recovered.task.status
            try:
                if recovered.has_valid_completion_payload():
                    if status in ('processed', 'postprocessing', 'checkpoint_pending'):
                        if not recovered.transition_status(status, 'history_pending'):
                            raise Exception(
                                'Unable to advance a committed completion payload.')
                    quarantine.pop(task_id, None)
                    continue

                if recovery['pre_operations']:
                    if status in ('postprocessing', 'checkpoint_pending'):
                        if not recovered.transition_status(status, 'processed'):
                            raise Exception(
                                'Unable to release an unused postprocessing claim.')
                    quarantine.pop(task_id, None)
                    continue

                if status == 'processed':
                    if not recovered.transition_status(
                            'processed', 'checkpoint_pending'):
                        raise Exception(
                            'Unable to restore the postprocessing quarantine.')
                    status = 'checkpoint_pending'
                if status == 'checkpoint_pending':
                    if recovery['abandoned']:
                        recovered.save_abandoned_postprocessing_checkpoint(
                            'Postprocessing was interrupted before its result '
                            'checkpoint could be recovered.')
                    else:
                        recovered.save_completion_checkpoint(
                            recovery['destination_files'],
                            recovery['file_move_processes_success'],
                        )
                    quarantine.pop(task_id, None)
                    continue
                if status in (
                        'history_pending', 'completion_dispatching',
                        'deletion_pending', 'complete'):
                    quarantine.pop(task_id, None)
            except Exception as e:
                recovery['not_before'] = (
                    time.monotonic() + self.CHECKPOINT_RECONCILE_DELAY_SECONDS)
                self._log("Completion checkpoint is still settling",
                          message2=str(e), level="warning")

    def _recover_interrupted_postprocessing(self, task_status):
        """Recover a durable owner state without repeating uncertain file work."""
        recovered_destination = None
        try:
            if self.current_task.has_valid_completion_payload():
                return self.current_task.transition_status(
                    task_status, 'history_pending')
            recovered_destination = self._recover_staged_default_replacement()
            if recovered_destination:
                if task_status == 'postprocessing':
                    if not self.current_task.transition_status(
                            'postprocessing', 'checkpoint_pending'):
                        raise Exception(
                            'Unable to claim recovered staged replacement.')
                self.current_task.save_completion_checkpoint(
                    [recovered_destination], True)
                return True
            if task_status == 'postprocessing':
                # The checkpoint_pending write is the boundary before plugins
                # and file operations, so this state is always safe to release.
                return self.current_task.transition_status(
                    'postprocessing', 'processed')
            self.current_task.save_abandoned_postprocessing_checkpoint(
                'Postprocessing was interrupted before its result checkpoint '
                'was durably recorded; file operations were not repeated.')
            return True
        except Exception as e:
            self._log("Exception in recovering interrupted postprocessing",
                      message2=str(e), level="exception")
            if recovered_destination:
                self._quarantine_checkpoint(
                    destination_files=[recovered_destination],
                    file_move_processes_success=True,
                )
            elif task_status == 'postprocessing':
                self._quarantine_checkpoint(pre_operations=True)
            else:
                self._quarantine_checkpoint(
                    destination_files=[],
                    file_move_processes_success=False,
                    abandoned=True,
                )
            return False

    def _task_part_path(
            self, file_in, file_out, plugin_id='DEFAULT', move=True):
        """Return the staging path owned by this task and exact operation."""
        operation = '\0'.join((
            os.path.abspath(file_in),
            str(plugin_id),
            str(bool(move)),
        ))
        source_key = hashlib.sha256(
            operation.encode('utf-8')).hexdigest()[:12]
        return '{}.unmanic.{}.{}.part'.format(
            file_out, self.current_task.get_task_id(), source_key)

    @staticmethod
    def _sync_file(pathname):
        with open(pathname, 'rb') as staged_file:
            os.fsync(staged_file.fileno())

    @staticmethod
    def _sync_directory(pathname):
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
        try:
            directory_fd = os.open(pathname or '.', flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            unsupported = {
                errno.EACCES,
                errno.EBADF,
                errno.EINVAL,
                getattr(errno, 'ENOTSUP', errno.EINVAL),
                getattr(errno, 'EOPNOTSUPP', errno.EINVAL),
            }
            if error.errno not in unsupported:
                raise

    def _finalize_staged_replacement(
            self, file_in, file_out, move, plugin_id='DEFAULT'):
        part_file_out = self._task_part_path(
            file_in, file_out, plugin_id=plugin_id, move=move)
        if not os.path.isfile(part_file_out) or os.path.islink(part_file_out):
            return False

        self._sync_file(part_file_out)
        self._log(
            "Atomically replacing '{}' with staged file '{}'.".format(
                file_out, part_file_out),
            level='debug',
        )
        os.replace(part_file_out, file_out)
        self._sync_directory(os.path.dirname(file_out))

        # Keep the source until the replacement and its directory entry are
        # durable. This also makes cross-filesystem moves crash-safe.
        if move and os.path.exists(file_in):
            os.remove(file_in)
        return True

    def _recover_staged_default_replacement(self):
        """Finish only the exact default replacement owned by this task."""
        if self.current_task.get_task_type() != 'local':
            return None
        file_in = self.current_task.get_cache_path()
        file_out = self.current_task.get_destination_data().get('abspath')
        if not file_out:
            return None
        if not self._finalize_staged_replacement(
                file_in, file_out, move=True, plugin_id='DEFAULT'):
            return None
        self._log(
            "Recovered staged replacement for task {}.".format(
                self.current_task.get_task_id()),
            level='warning',
        )
        return file_out

    def _persist_local_history_and_remove_task(self):
        self._load_completion_payload()
        try:
            history_persisted = self.write_history_log()
        except Exception as e:
            self._log("Exception in writing history log", message2=str(e), level="exception")
            history_persisted = False
        if not history_persisted:
            self._log(
                "Task history was not persisted; retaining the parked source task record.",
                level="error")
            return False
        try:
            # This durable task-keyed claim is intentionally persisted before
            # external effects. Recovery never redispatches a claimed task:
            # a crash after this write may skip effects, but cannot duplicate them.
            self.current_task.set_status('completion_dispatching')
        except Exception as e:
            self._log("Exception in claiming completion side effects",
                      message2=str(e), level="exception")
            return False
        try:
            self._dispatch_completion_side_effects()
        except Exception as e:
            # The durable claim makes this attempt terminal even when payload
            # preparation fails; retrying any subset could duplicate effects.
            self._log("Exception in completion side-effect dispatch",
                      message2=str(e), level="exception")
        return self._finish_claimed_completion()

    def _load_completion_payload(self):
        try:
            payload = self.current_task.get_completion_payload()
        except Exception as e:
            self._log("Exception in loading completion recovery data",
                      message2=str(e), level="exception")
            payload = None
        if not isinstance(payload, dict):
            return
        destination_files = payload.get('destination_files', [])
        if isinstance(destination_files, list):
            self._last_destination_files = list(destination_files)
        self._last_file_move_processes_success = bool(
            payload.get('file_move_processes_success', False))

    def _finish_claimed_completion(self):
        try:
            self.current_task.set_status('deletion_pending')
        except Exception as e:
            self._log("Exception in parking completed task for deletion",
                      message2=str(e), level="exception")
            return False
        return self._remove_completed_local_task()

    def _remove_completed_local_task(self):
        try:
            self.current_task.delete()
        except Exception as e:
            self._log("Exception in removing task from task list", message2=str(e), level="exception")
            return False
        return True

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

    @staticmethod
    def _remote_file_fingerprint(pathname):
        if (not pathname or not os.path.isfile(pathname)
                or os.path.islink(pathname)):
            return None
        return {
            'size': os.path.getsize(pathname),
            'checksum': common.get_file_checksum(pathname),
        }

    @classmethod
    def _remote_file_matches(cls, pathname, fingerprint):
        if not Task._valid_remote_fingerprint(fingerprint) or fingerprint is None:
            return False
        try:
            return cls._remote_file_fingerprint(pathname) == fingerprint
        except OSError:
            return False

    def _build_remote_delivery_claim(self):
        cache_path = os.path.abspath(self.current_task.get_cache_path())
        source_path = os.path.abspath(
            self.current_task.get_source_data()['abspath'])
        destination_path = os.path.abspath(
            self.current_task.get_destination_data()['abspath'])
        cache_root = os.path.realpath(os.path.abspath(
            self.settings.get_cache_path()))
        resolved_destination = os.path.realpath(destination_path)
        try:
            destination_in_cache = (
                os.path.commonpath([cache_root, resolved_destination])
                == cache_root
            )
        except ValueError:
            destination_in_cache = False

        if destination_in_cache:
            target_paths = [destination_path]
            staging_directories = []
        else:
            staging_token = uuid.uuid4().hex
            staging_name = '{}task-{}-{}'.format(
                Task.REMOTE_UPLOAD_PREFIX,
                self.current_task.get_task_id(),
                staging_token,
            )
            basename = os.path.basename(cache_path)
            target_paths = [
                os.path.join(
                    os.path.dirname(source_path),
                    staging_name,
                    basename,
                ),
                os.path.join(
                    cache_root,
                    staging_name,
                    basename,
                ),
            ]
            staging_directories = [
                os.path.dirname(path) for path in target_paths
            ]
        try:
            fingerprint = self._remote_file_fingerprint(cache_path)
        except OSError:
            fingerprint = None
        return {
            'version': 1,
            'phase': 'claimed',
            'source_path': source_path,
            'cache_path': cache_path,
            'destination_path': destination_path,
            'target_paths': target_paths,
            'destination_in_cache': destination_in_cache,
            'input_fingerprint': fingerprint,
            'owner_task_id': self.current_task.get_task_id(),
            'staging_token': (
                None if destination_in_cache else staging_token),
            'staging_directories': staging_directories,
        }

    @staticmethod
    def _write_remote_staging_marker(claim, staging_directory):
        marker_path = os.path.join(
            staging_directory, Task.REMOTE_STAGING_MARKER)
        marker = {
            'version': 1,
            'task_id': claim['owner_task_id'],
            'token': claim['staging_token'],
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        marker_fd = os.open(marker_path, flags, 0o600)
        try:
            encoded = json.dumps(marker, sort_keys=True).encode('utf-8')
            os.write(marker_fd, encoded)
            os.fsync(marker_fd)
        finally:
            os.close(marker_fd)

    @classmethod
    def _claimed_staging_directory(cls, claim, output_path):
        if not output_path or not claim.get('staging_token'):
            return None
        staging_directory = os.path.dirname(os.path.abspath(output_path))
        if staging_directory not in claim.get('staging_directories', []):
            return None
        marker_path = os.path.join(
            staging_directory, Task.REMOTE_STAGING_MARKER)
        if os.path.islink(marker_path) or not os.path.isfile(marker_path):
            return None
        try:
            with open(marker_path, 'r', encoding='utf-8') as marker_file:
                marker = json.load(marker_file)
        except (OSError, TypeError, ValueError):
            return None
        if marker != {
                'version': 1,
                'task_id': claim.get('owner_task_id'),
                'token': claim.get('staging_token')}:
            return None
        return staging_directory

    @staticmethod
    def _failure_timestamp(value):
        if value is None:
            return None
        timestamp = getattr(value, 'timestamp', None)
        if callable(timestamp):
            return timestamp()
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _build_remote_delivery_checkpoint(
            self, claim, output_path, delivery_success, recovery_message=None):
        output_fingerprint = None
        if output_path:
            try:
                output_fingerprint = self._remote_file_fingerprint(output_path)
            except OSError:
                output_fingerprint = None
        retrieval_path = output_path or claim['source_path']
        task_success = bool(self.current_task.get_task_success())
        failure_category = str(
            getattr(self.current_task.task, 'failure_category', '') or '')
        failure_message = str(
            getattr(self.current_task.task, 'failure_message', '') or '')
        failure_time = self._failure_timestamp(
            getattr(self.current_task.task, 'failure_time', None))
        if task_success and not delivery_success:
            task_success = False
            failure_category = TaskFailureCategory.POSTPROCESSING
            failure_message = (
                recovery_message
                or 'The processed file could not be safely staged for retrieval.'
            )
            failure_time = time.time()
        task_state = TaskDataStore.export_task_state(
            self.current_task.get_task_id())
        if not isinstance(task_state, dict):
            task_state = {}

        checkpoint = dict(claim)
        checkpoint.update({
            'phase': 'checkpointed',
            'retrieval_path': retrieval_path,
            'output_path': output_path,
            'output_fingerprint': output_fingerprint,
            'delivery_success': bool(delivery_success),
            'metadata_path': Task.remote_metadata_path(retrieval_path),
            'task_success': task_success,
            'failure_category': failure_category[:64],
            'failure_message': ' '.join(failure_message.split())[:500],
            'failure_time': failure_time,
            'task_state': task_state,
            'staging_directory': self._claimed_staging_directory(
                claim, output_path),
        })
        return checkpoint

    def _process_remote_delivery(self, task_status):
        if task_status == 'remote_metadata_pending':
            return self._persist_remote_metadata_and_complete()
        if task_status == 'remote_delivery':
            return self._recover_interrupted_remote_delivery()
        if task_status != 'processed':
            self._log(
                "Unsupported remote delivery state '{}'.".format(task_status),
                level='error',
            )
            return False

        try:
            if self.current_task.has_valid_remote_delivery_checkpoint():
                self.current_task.apply_remote_delivery_checkpoint()
                return self._persist_remote_metadata_and_complete()
            if self.current_task.has_valid_remote_delivery_claim():
                if not self.current_task.transition_status(
                        'processed', 'remote_delivery'):
                    raise Exception(
                        'Unable to restore the persisted remote delivery claim.')
                return self._recover_interrupted_remote_delivery()
            claim = self._build_remote_delivery_claim()
            self.current_task.save_remote_delivery_claim(claim)
        except Exception as e:
            self._log(
                "Exception in claiming remote delivery",
                message2=str(e),
                level="exception",
            )
            self._quarantine_remote_delivery()
            return False

        PluginsHandler().run_event_plugins_for_plugin_type(
            'events.postprocessor_started',
            {
                'library_id': self.current_task.get_task_library_id(),
                'task_id': self.current_task.get_task_id(),
                'task_type': self.current_task.get_task_type(),
                'cache_path': self.current_task.get_cache_path(),
                'source_data': self.current_task.get_source_data(),
            },
        )

        try:
            result = self.post_process_remote_file(claim)
            checkpoint = self._build_remote_delivery_checkpoint(
                claim,
                result.get('output_path'),
                result.get('delivery_success', False),
            )
            self.current_task.save_remote_delivery_checkpoint(checkpoint)
        except Exception as e:
            self._log(
                "Exception in checkpointing remote task delivery",
                message2=str(e),
                level="exception",
            )
            self._quarantine_remote_delivery()
            return False
        return self._persist_remote_metadata_and_complete()

    def _recover_interrupted_remote_delivery(self):
        """Resolve exact artifacts without rerunning or overwriting delivery."""
        payload = self.current_task.get_remote_delivery_payload()
        if Task._is_valid_remote_delivery_checkpoint(payload):
            try:
                self.current_task.apply_remote_delivery_checkpoint()
            except Exception as e:
                self._log(
                    "Exception in restoring remote delivery checkpoint",
                    message2=str(e),
                    level='exception',
                )
                self._quarantine_remote_delivery()
                return False
            return self._persist_remote_metadata_and_complete()

        valid_claim = Task._is_valid_remote_delivery_claim(payload)
        claim = payload if valid_claim else self._build_remote_delivery_claim()
        output_path = None
        delivery_success = False
        if valid_claim:
            matching_targets = [
                path for path in claim['target_paths']
                if self._remote_file_matches(
                    path, claim.get('input_fingerprint'))
            ]
            if matching_targets:
                output_path = matching_targets[0]
                delivery_success = True
            elif self._remote_file_matches(
                    claim['cache_path'], claim.get('input_fingerprint')):
                output_path = claim['cache_path']
            else:
                existing_targets = [
                    path for path in claim['target_paths']
                    if os.path.isfile(path) and not os.path.islink(path)
                ]
                if existing_targets:
                    output_path = existing_targets[0]

        if output_path is None:
            for candidate in (
                    claim['cache_path'],
                    claim['source_path'],
                    claim['destination_path']):
                if os.path.isfile(candidate) and not os.path.islink(candidate):
                    output_path = candidate
                    break

        recovery_message = None
        if not delivery_success:
            recovery_message = (
                'Remote delivery was interrupted before its exact result '
                'checkpoint was durable; the surviving artifact was preserved '
                'without overwriting any path.'
            )
        try:
            checkpoint = self._build_remote_delivery_checkpoint(
                claim,
                output_path,
                delivery_success,
                recovery_message=recovery_message,
            )
            self.current_task.save_remote_delivery_checkpoint(checkpoint)
        except Exception as e:
            self._log(
                "Exception in recording interrupted remote delivery",
                message2=str(e),
                level='exception',
            )
            self._quarantine_remote_delivery()
            return False
        return self._persist_remote_metadata_and_complete()

    def _persist_remote_metadata_and_complete(self):
        payload = self.current_task.get_remote_delivery_payload()
        if not Task._is_valid_remote_delivery_checkpoint(payload):
            return self._recover_interrupted_remote_delivery()
        self._last_destination_files = (
            [payload['output_path']] if payload.get('output_path') else [])
        self._last_file_move_processes_success = payload['delivery_success']
        try:
            self.dump_history_log(payload)
        except Exception as e:
            self._log(
                "Exception in dumping history log for remote task",
                message2=str(e),
                level="exception",
            )
            return False
        try:
            self.current_task.set_status('complete')
        except Exception as e:
            self._log(
                "Exception in marking remote task as complete",
                message2=str(e),
                level="exception",
            )
            return False
        return True

    def post_process_remote_file(self, claim=None):
        """Move the processed artifact according to an already durable claim."""
        claim = claim or self._build_remote_delivery_claim()
        cache_path = claim['cache_path']
        self._log(
            "Remote source: {}, delivery targets: {}.".format(
                claim['source_path'], claim['target_paths']),
            level='debug',
        )
        move_success = False
        final_destination = None

        if not self._remote_file_matches(
                cache_path, claim.get('input_fingerprint')):
            self._log(
                "Remote delivery input is missing or changed; preserving all "
                "surviving artifacts.",
                level='error',
            )
        else:
            for index, target_path in enumerate(claim['target_paths']):
                try:
                    if not claim.get('destination_in_cache'):
                        staging_directory = os.path.dirname(target_path)
                        os.mkdir(staging_directory, mode=0o700)
                        self._write_remote_staging_marker(
                            claim, staging_directory)
                    if self.__copy_file(
                            cache_path, target_path, [], 'DEFAULT', move=True):
                        final_destination = target_path
                        move_success = self._remote_file_matches(
                            target_path, claim.get('input_fingerprint'))
                        if not move_success:
                            self._log(
                                "Delivered remote output did not match its "
                                "claimed fingerprint.",
                                level='error',
                            )
                        break
                    raise Exception('Failed to stage claimed remote output')
                except Exception as e:
                    if index + 1 < len(claim['target_paths']):
                        self._log(
                            "Remote delivery target failed ({}); trying the "
                            "claimed fallback.".format(e),
                            level='warning',
                        )
                    else:
                        self._log(
                            "All claimed remote delivery targets failed ({}).".format(
                                e),
                            level='error',
                        )

        self._last_file_move_processes_success = move_success
        self._last_destination_files = (
            [final_destination] if final_destination else [])
        return {
            'output_path': final_destination,
            'delivery_success': move_success,
        }

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
            part_file_out = self._task_part_path(
                file_in, file_out, plugin_id=plugin_id, move=move)
            copying_file_out = '{}.copying'.format(part_file_out)

            # A complete task-owned part can be left by a failed final
            # replacement or a process exit. It is safe to finish directly.
            if self._finalize_staged_replacement(
                    file_in, file_out, move, plugin_id=plugin_id):
                destination_files.append(file_out)
                return True

            # Ensure the src and dst are not the same file
            if os.path.exists(file_out) and os.path.samefile(file_in, file_out):
                if move and os.path.abspath(file_in) != os.path.abspath(file_out):
                    self._sync_file(file_out)
                    self._sync_directory(os.path.dirname(file_out))
                    os.remove(file_in)
                    destination_files.append(file_out)
                    return True
                self._log("The file_in and file_out path are the same file. Nothing will be done! '{}'".format(file_in),
                          level="warning")
                return False

            # Get a checksum prior to copy
            if not os.path.exists(file_in):
                self._log("The file_in path does not exist! '{}'".format(file_in), level="warning")
                self.event.wait(1)
            self._log("Fetching checksum of source file '{}'.".format(file_in), level='debug')

            # Incomplete copies use a distinct suffix and are never recovered
            # as complete task-owned parts.
            if os.path.lexists(copying_file_out):
                os.remove(copying_file_out)

            if move:
                self._log("Staging move '{}' --> '{}'.".format(file_in, copying_file_out), level='debug')
                try:
                    # A hard link preserves ownership and permissions while
                    # retaining the source until replacement is durable.
                    os.link(file_in, copying_file_out)
                except OSError:
                    # Cross-filesystem and filesystems without hard-link
                    # support safely fall back to a metadata-preserving copy.
                    shutil.copy2(file_in, copying_file_out)
            else:
                self._log("Staging copy '{}' --> '{}'.".format(file_in, copying_file_out), level='debug')
                shutil.copyfile(file_in, copying_file_out)

            self._sync_file(copying_file_out)
            os.replace(copying_file_out, part_file_out)
            self._sync_directory(os.path.dirname(file_out))
            self._finalize_staged_replacement(
                file_in, file_out, move, plugin_id=plugin_id)
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

        history_persisted = history_logging.save_task_history(
            {
                'source_task_id':       self.current_task.get_task_id(),
                'task_label':          task_dump.get('task_label', ''),
                'abspath':             task_dump.get('abspath', ''),
                'task_success':        task_dump.get('task_success', False),
                'start_time':          task_dump.get('start_time', ''),
                'finish_time':         task_dump.get('finish_time', ''),
                'processed_by_worker': task_dump.get('processed_by_worker', ''),
                'failure_category':    task_dump.get('failure_category', ''),
                'failure_message':     task_dump.get('failure_message', ''),
                'failure_time':        task_dump.get('failure_time'),
                'log':                 task_dump.get('log', ''),
            }
        )
        if not history_persisted:
            self._log("Failed to persist task history; the source task will be retained.", level="error")
            return False

        return True

    def _dispatch_completion_side_effects(self):
        task_dump = self.current_task.task_dump()
        source_data = self.current_task.get_source_data()
        destination_data = self.current_task.get_destination_data()

        if not self.current_task.task.success:
            self._attempt_completion_effect(
                'failed-task notification',
                lambda: Notifications().add(
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
                    }),
            )

        self._attempt_completion_effect(
            'completed-task data log',
            lambda: self._log_completed_task_data(task_dump, source_data, destination_data),
        )

        event_data = {
            'library_id':          self.current_task.get_task_library_id(),
            'task_id':             self.current_task.get_task_id(),
            'task_type':           self.current_task.get_task_type(),
            'source_data':         source_data,
            'destination_data':    destination_data,
            'destination_files':   list(self._last_destination_files or []),
            'task_success':        task_dump.get('task_success', False),
            'file_move_processes_success': self._last_file_move_processes_success,
            'start_time':          task_dump.get('start_time', ''),
            'finish_time':         task_dump.get('finish_time', ''),
            'processed_by_worker': task_dump.get('processed_by_worker', ''),
            'log':                 task_dump.get('log', ''),
        }
        self._attempt_completion_effect(
            'postprocessor-complete plugins',
            lambda: PluginsHandler().run_event_plugins_for_plugin_type(
                'events.postprocessor_complete', event_data),
        )

    def _attempt_completion_effect(self, label, callback):
        try:
            callback()
        except Exception as e:
            self._log("Exception in {}".format(label), message2=str(e), level="exception")

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
        committed = UnmanicFileMetadata.commit_task(
            task_id=self.current_task.get_task_id(),
            task_success=task_success,
            source_path=source_data.get('abspath'),
            destination_paths=destination_files,
        )
        if committed:
            self._log("Committed file metadata entries: {}".format(committed), level='debug')
        return committed

    def _write_remote_metadata(self, metadata, pathname):
        metadata_directory = os.path.dirname(pathname)
        if os.path.lexists(metadata_directory):
            if (os.path.islink(metadata_directory)
                    or not os.path.isdir(metadata_directory)):
                raise Exception(
                    'Remote metadata directory collides with an unsafe path.')
        else:
            os.mkdir(metadata_directory, mode=0o700)
            self._sync_directory(os.path.dirname(metadata_directory))

        encoded = json.dumps(
            metadata, sort_keys=True, indent=4, default=str).encode('utf-8')
        temporary_path = '{}.{}.writing'.format(
            pathname, self.current_task.get_task_id())
        if os.path.lexists(temporary_path):
            if (os.path.islink(temporary_path)
                    or not os.path.isfile(temporary_path)):
                raise Exception('Unsafe remote metadata temporary path.')
            os.remove(temporary_path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        file_descriptor = os.open(temporary_path, flags, 0o600)
        try:
            with os.fdopen(file_descriptor, 'wb') as metadata_file:
                file_descriptor = None
                metadata_file.write(encoded)
                metadata_file.flush()
                os.fsync(metadata_file.fileno())
            os.replace(temporary_path, pathname)
            self._sync_directory(metadata_directory)
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            if os.path.isfile(temporary_path) and not os.path.islink(
                    temporary_path):
                os.remove(temporary_path)

    def dump_history_log(self, delivery_checkpoint=None):
        self._log("Dumping remote task history log.", level='debug')
        task_dump = self.current_task.task_dump()
        checkpoint = (
            delivery_checkpoint
            or self.current_task.get_remote_delivery_payload()
        )
        if not Task._is_valid_remote_delivery_checkpoint(checkpoint):
            raise Exception('Remote delivery checkpoint is missing or invalid.')

        output_fingerprint = checkpoint.get('output_fingerprint') or {}
        metadata = {
            'task_label':          task_dump.get('task_label', ''),
            'abspath':             checkpoint.get('output_path')
            or checkpoint['retrieval_path'],
            'task_success':        checkpoint['task_success'],
            'start_time':          task_dump.get('start_time', ''),
            'finish_time':         task_dump.get('finish_time', ''),
            'processed_by_worker': task_dump.get('processed_by_worker', ''),
            'failure_category':    checkpoint['failure_category'],
            'failure_message':     checkpoint['failure_message'],
            'failure_time':        checkpoint['failure_time'],
            'log':                 task_dump.get('log', ''),
            'checksum':            output_fingerprint.get(
                'checksum', 'UNKNOWN'),
            'task_state':          checkpoint['task_state'],
        }
        self._write_remote_metadata(metadata, checkpoint['metadata_path'])
        return checkpoint['metadata_path']

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

        UnmanicLogging.data(
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
