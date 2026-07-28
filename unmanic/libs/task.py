#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    unmanic.task.py
 
    Written by:               Josh.5 <jsunnex@gmail.com>
    Date:                     27 Apr 2019, (2:08 PM)
 
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
import datetime
import json
import os
import re
import shutil
import stat
import threading
import time
from contextlib import nullcontext
from copy import deepcopy
from operator import attrgetter

from playhouse.shortcuts import model_to_dict
from playhouse.sqliteq import ResultTimeout, SqliteQueueDatabase

try:
    import fcntl
except ImportError:  # pragma: no cover - remote uploads are served on POSIX hosts
    fcntl = None

from unmanic import config
from unmanic.libs import common
from unmanic.libs.library import Library
from unmanic.libs.logs import UnmanicLogging
from unmanic.libs.unmodels.tasklifecycle import TaskLifecycle
from unmanic.libs.unmodels.taskmetadata import TaskMetadata
from unmanic.libs.unmodels.tasks import IntegrityError, Tasks


def prepare_file_destination_data(pathname, file_extension):
    basename = os.path.basename(pathname)
    dirname = os.path.dirname(os.path.abspath(pathname))
    # Fetch the file's name without the file extension (this is going to be reset)
    file_name_without_extension = os.path.splitext(basename)[0]

    # Set destination dict
    basename = "{}.{}".format(file_name_without_extension, file_extension)
    abspath = os.path.join(dirname, basename)
    file_data = {
        'basename': basename,
        'abspath':  abspath
    }

    return file_data


class TaskCreationOwnershipUncertain(Exception):
    pass


class TaskFailureCategory:
    PROCESSING = 'processing_failed'
    POSTPROCESSING = 'postprocessing_failed'
    INTERNAL = 'internal_error'
    STALLED = 'stalled'  # Reserved for issue #15; no stall detection is implemented here.


class Task(object):
    """
    Task

    Contains the stage and all data pertaining to a transcode task

    """
    CREATING_RECONCILIATION_GRACE_SECONDS = 30
    REMOTE_UPLOAD_DIRECTORY = 'remote_library'
    REMOTE_UPLOAD_PREFIX = 'unmanic_remote_pending_library-'
    REMOTE_UPLOAD_ACTIVE_MARKER = '.upload-active'
    REMOTE_STAGING_MARKER = '.unmanic-remote-staging'
    REMOTE_UPLOAD_MARKER_GRACE_SECONDS = 300
    REMOTE_METADATA_DIRECTORY = '.unmanic'
    REMOTE_METADATA_FILENAME = 'data.json'
    RECOVERABLE_LOCAL_STATES = (
        'processed',
        'postprocessing',
        'checkpoint_pending',
        'history_pending',
        'completion_dispatching',
        'deletion_pending',
    )
    RECOVERABLE_REMOTE_STATES = (
        'processed',
        'remote_delivery',
        'remote_metadata_pending',
        'complete',
    )
    _lifecycle_lock = threading.RLock()

    def __init__(self):
        self.name = 'Task'
        self.task = None
        self.task_dict = None
        self.settings = config.Config()
        self.logger = UnmanicLogging.get_logger(name=__class__.__name__)
        self.statistics = {}
        self.errors = []

    def set_cache_path(self, cache_directory=None, file_extension=None):
        if not self.task:
            raise Exception('Unable to set cache path. Task has not been set!')
        # Fetch the file's name without the file extension (this is going to be reset)
        split_file_name = os.path.splitext(self.get_source_basename())
        file_name_without_extension = split_file_name[0]

        if not file_extension:
            # Get file extension
            file_extension = split_file_name[1].lstrip('.')

        # Parse an output cache path
        random_string = '{}-{}'.format(common.random_string(), int(time.time()))
        out_file = "{}-{}.{}".format(file_name_without_extension, random_string, file_extension)
        if not cache_directory:
            out_folder = "unmanic_file_conversion-{}".format(random_string)
            cache_directory = os.path.join(self.settings.get_cache_path(), out_folder)

        # Set cache path class attribute
        self.task.cache_path = os.path.join(cache_directory, out_file)

    def get_cache_path(self):
        if not self.task:
            raise Exception('Unable to fetch cache path. Task has not been set!')
        if not self.task.cache_path:
            raise Exception('Unable to fetch cache path. Task cache path has not been set!')
        return self.task.cache_path

    def get_task_data(self):
        if not self.task:
            raise Exception('Unable to fetch task dictionary. Task has not been set!')
        self.task_dict = model_to_dict(self.task, backrefs=True)
        return self.task_dict

    def get_task_id(self):
        if not self.task:
            raise Exception('Unable to fetch task ID. Task has not been set!')
        return self.task.id

    def get_task_type(self):
        if not self.task:
            raise Exception('Unable to fetch task type. Task has not been set!')
        return self.task.type

    def get_task_library_id(self):
        if not self.task:
            raise Exception('Unable to fetch task library ID. Task has not been set!')
        return self.task.library_id

    def get_task_library_name(self):
        if not self.task:
            raise Exception('Unable to fetch task library ID. Task has not been set!')
        library = Library(self.task.library_id)
        return library.get_name()

    def get_task_library_priority_score(self):
        if not self.task:
            raise Exception('Unable to fetch task library ID. Task has not been set!')
        library = Library(self.task.library_id)
        return library.get_priority_score()

    def get_destination_data(self):
        if not self.task:
            raise Exception('Unable to fetch destination data. Task has not been set!')

        cache_path = self.get_cache_path()

        # Get the current cache path's file extension
        split_file_name = os.path.splitext(os.path.basename(cache_path))
        file_extension = split_file_name[1].lstrip('.')

        return prepare_file_destination_data(self.task.abspath, file_extension)

    def get_source_data(self):
        if not self.task:
            raise Exception('Unable to fetch source absolute path. Task has not been set!')
        if not self.task.abspath:
            raise Exception('Unable to fetch source absolute path. Task absolute path has not been set!')
        return {
            'abspath':  self.task.abspath,
            'basename': os.path.basename(self.task.abspath),
        }

    def get_source_basename(self):
        return self.get_source_data().get('basename')

    def get_source_abspath(self):
        return self.get_source_data().get('abspath')

    def get_task_success(self):
        if not self.task:
            raise Exception('Unable to fetch task success. Task has not been set!')
        return self.task.success

    def get_start_time(self):
        if not self.task:
            raise Exception('Unable to fetch task start time. Task has not been set!')
        return self.task.start_time

    def get_finish_time(self):
        if not self.task:
            raise Exception('Unable to fetch task finish time. Task has not been set!')
        return self.task.finish_time

    def task_dump(self):
        # Generate a copy of this class as a dict
        task_dict = {
            'task_label':          self.get_source_basename(),
            'abspath':             self.get_source_abspath(),
            'task_success':        self.task.success,
            'start_time':          self.task.start_time,
            'finish_time':         self.task.finish_time,
            'processed_by_worker': self.task.processed_by_worker,
            'failure_category':    self.task.failure_category,
            'failure_message':     self.task.failure_message,
            'failure_time':        self.task.failure_time,
            'errors':              self.errors,
            'log':                 self.task.log,
        }
        return task_dict

    def read_and_set_task_by_absolute_path(self, abspath):
        """
        Sets the task by it's absolute path.
        If the task already exists in the list, then return that task.
        If the task does not yet exist in the list, create it first.

        :param abspath:
        :return:
        """
        # Get task matching the abspath
        self.task = Tasks.get(abspath=abspath)

    def create_task_by_absolute_path(
            self, abspath, task_type='local', library_id=1, priority_score=0, return_task_data=False):
        """
        Creates the task by its absolute path.
        If the task already exists in the list, then this will throw an exception and return false

        Calls to read_and_set_task_by_absolute_path() to read back all data out of the database.

        :param abspath:
        :param task_type:
        :param library_id:
        :param priority_score:
        :param return_task_data:
        :return:
        """
        self.task = None
        self.task_dict = None
        database = getattr(Tasks._meta.database, 'obj', Tasks._meta.database)
        # SqliteQueueDatabase does not support transactions. Its durable
        # creating row is reconciled later instead of issuing an unsafe
        # compensating delete whose result could also be ambiguous.
        uses_queue_database = isinstance(database, SqliteQueueDatabase)
        transaction = nullcontext() if uses_queue_database else database.atomic()
        try:
            with transaction:
                self.task = Tasks.create(
                    abspath=abspath,
                    status='creating',
                    library_id=library_id,
                    ownership_uncertain=False,
                )
                self.save()
                self.logger.debug("Created new task with ID: %s for %s", self.task, abspath)

                # Set the cache path to use during the transcoding
                self.set_cache_path()

                # Fetch the library priority score also for this task
                library_priority_score = self.get_task_library_priority_score()

                # Set the default priority to the ID of the task
                self.task.priority = int(self.task.id) + int(library_priority_score) + int(priority_score)

                # Set the task type
                self.task.type = task_type

                # Only local tasks should be progressed automatically
                # Remote tasks need to be progressed to pending by a remote trigger
                if task_type == 'local':
                    # Now set the status to pending. Only then will it be picked up by a worker.
                    # This will also save the task.
                    self.set_status('pending')
                else:
                    # Save the tasks updates without settings status to pending
                    self.save()

                task_data = self.get_task_data() if return_task_data else None
            return task_data if return_task_data else True
        except ResultTimeout as e:
            self._mark_creation_ownership_uncertain(abspath)
            self.task = None
            self.task_dict = None
            raise TaskCreationOwnershipUncertain(
                "Task creation timed out before ownership could be confirmed"
            ) from e
        except IntegrityError as e:
            self.task = None
            self.task_dict = None
            self.logger.info("Cancel creating new task for %s - %s", abspath, e)
            return False
        except Exception as error:
            if uses_queue_database and self.task is not None:
                self._mark_creation_ownership_uncertain(
                    abspath, task_id=self.task.id)
                self.logger.exception(
                    "Task creation failed after its queued row was created for %s",
                    abspath,
                )
                self.task = None
                self.task_dict = None
                raise TaskCreationOwnershipUncertain(
                    "Task creation failed after ownership became uncertain"
                ) from error
            self.task = None
            self.task_dict = None
            raise

    @staticmethod
    def _mark_creation_ownership_uncertain(abspath, task_id=None):
        """Queue a marker behind an ambiguous create without deleting its row."""
        predicate = (
            (Tasks.abspath == os.path.abspath(abspath))
            & (Tasks.status == 'creating')
        )
        if task_id is not None:
            predicate &= (Tasks.id == task_id)
        try:
            (Tasks.update(ownership_uncertain=True)
             .where(predicate)
             .execute())
        except Exception:
            # The marker is ordered behind the create on SqliteQueueDatabase
            # and may still commit after a timeout. Any unmarked row is left
            # untouched rather than risking loss of caller ownership.
            pass

    @classmethod
    def reconcile_stale_creating_tasks(cls, grace_seconds=None, now=None):
        """Recover only stale local rows explicitly marked ownership-uncertain."""
        if grace_seconds is None:
            grace_seconds = cls.CREATING_RECONCILIATION_GRACE_SECONDS
        database = getattr(Tasks._meta.database, 'obj', Tasks._meta.database)
        queue_size = getattr(database, 'queue_size', None)
        if callable(queue_size) and queue_size() > 0:
            return {'pending': 0, 'deleted': 0}
        cutoff = (now or datetime.datetime.now()) - datetime.timedelta(seconds=grace_seconds)
        stale_tasks = (Tasks.select()
                       .where((Tasks.type == 'local')
                              & (Tasks.status == 'creating')
                              & (Tasks.ownership_uncertain == True)
                              & ((Tasks.start_time <= cutoff) | Tasks.start_time.is_null(True))))
        reconciled = {'pending': 0, 'deleted': 0}
        for stale_task in stale_tasks:
            initialized = (
                bool(stale_task.abspath)
                and bool(stale_task.cache_path)
                and stale_task.priority is not None
                and stale_task.library_id is not None
            )
            predicate = (
                (Tasks.id == stale_task.id)
                & (Tasks.type == 'local')
                & (Tasks.status == 'creating')
                & (Tasks.ownership_uncertain == True)
            )
            if initialized:
                reconciled['pending'] += (
                    Tasks.update(
                        status='pending',
                        ownership_uncertain=False,
                    ).where(predicate).execute()
                )
                continue
            deleted = Tasks.delete().where(predicate).execute()
            if deleted:
                TaskDataStore.clear_task(stale_task.id)
                reconciled['deleted'] += deleted
        return reconciled

    @classmethod
    def _remote_upload_directory_for_path(cls, cache_directory, pathname):
        if not cache_directory or not pathname:
            return None
        cache_root = os.path.realpath(os.path.abspath(cache_directory))
        remote_root = os.path.join(cache_root, cls.REMOTE_UPLOAD_DIRECTORY)
        resolved_path = os.path.realpath(os.path.abspath(pathname))
        upload_directory = os.path.dirname(resolved_path)
        if not os.path.basename(upload_directory).startswith(
                cls.REMOTE_UPLOAD_PREFIX):
            return None
        try:
            in_remote_root = os.path.commonpath(
                [remote_root, upload_directory]) == remote_root
        except ValueError:
            return None
        if os.path.dirname(upload_directory) != cache_root and not in_remote_root:
            return None
        return upload_directory

    @classmethod
    def is_task_owned_remote_upload_path(cls, cache_directory, pathname):
        """Return whether pathname is a regular direct child of an upload dir."""
        upload_directory = cls._remote_upload_directory_for_path(
            cache_directory, pathname)
        if not upload_directory:
            return False
        lexical_path = os.path.abspath(pathname)
        if os.path.islink(lexical_path):
            return False
        if os.path.realpath(os.path.dirname(lexical_path)) != upload_directory:
            return False
        resolved_path = os.path.realpath(lexical_path)
        if (os.path.dirname(resolved_path) != upload_directory
                or resolved_path != os.path.join(
                    upload_directory, os.path.basename(lexical_path))):
            return False
        try:
            return stat.S_ISREG(os.stat(
                lexical_path, follow_symlinks=False).st_mode)
        except OSError:
            return False

    @classmethod
    def _find_remote_upload_directories(cls, cache_directory):
        cache_root = os.path.realpath(os.path.abspath(cache_directory))
        if not os.path.isdir(cache_root):
            return set()

        candidates = set()
        try:
            cache_entries = list(os.scandir(cache_root))
        except OSError:
            return candidates
        for entry in cache_entries:
            if (entry.name.startswith(cls.REMOTE_UPLOAD_PREFIX)
                    and not entry.is_symlink()
                    and entry.is_dir(follow_symlinks=False)):
                candidates.add(os.path.realpath(entry.path))

        remote_root = os.path.join(cache_root, cls.REMOTE_UPLOAD_DIRECTORY)
        if not os.path.isdir(remote_root) or os.path.islink(remote_root):
            return candidates
        for current_root, directories, _files in os.walk(
                remote_root, topdown=True, followlinks=False):
            safe_directories = []
            for directory in directories:
                candidate = os.path.join(current_root, directory)
                if os.path.islink(candidate):
                    continue
                resolved = os.path.realpath(candidate)
                try:
                    if os.path.commonpath([remote_root, resolved]) != remote_root:
                        continue
                except ValueError:
                    continue
                if directory.startswith(cls.REMOTE_UPLOAD_PREFIX):
                    candidates.add(resolved)
                    # An upload directory is one cleanup unit. Never follow
                    # arbitrary content nested inside it.
                    continue
                safe_directories.append(directory)
            directories[:] = safe_directories
        return candidates

    @classmethod
    def reconcile_remote_uploads(
            cls, cache_directory, grace_seconds=None, now=None):
        """Publish marked uncertain creates and remove unowned stale uploads."""
        if grace_seconds is None:
            grace_seconds = cls.CREATING_RECONCILIATION_GRACE_SECONDS
        database = getattr(Tasks._meta.database, 'obj', Tasks._meta.database)
        queue_size = getattr(database, 'queue_size', None)
        if callable(queue_size) and queue_size() > 0:
            return {'pending': 0, 'orphaned': 0}
        current_time = now or datetime.datetime.now()
        if isinstance(current_time, datetime.datetime):
            current_timestamp = current_time.timestamp()
        else:
            current_timestamp = float(current_time)
            current_time = datetime.datetime.fromtimestamp(current_timestamp)
        cutoff = current_time - datetime.timedelta(seconds=grace_seconds)

        reconciled = {'pending': 0, 'orphaned': 0}
        stale_remote_tasks = (Tasks.select()
                              .where((Tasks.type == 'remote')
                                     & (Tasks.status == 'creating')
                                     & (Tasks.ownership_uncertain == True)
                                     & ((Tasks.start_time <= cutoff)
                                        | Tasks.start_time.is_null(True))))
        for stale_task in stale_remote_tasks:
            upload_directory = cls._remote_upload_directory_for_path(
                cache_directory, stale_task.abspath)
            initialized = (
                bool(stale_task.abspath)
                and bool(upload_directory)
                and os.path.isfile(stale_task.abspath)
                and bool(stale_task.cache_path)
                and stale_task.priority is not None
                and stale_task.library_id is not None
            )
            if not initialized:
                continue
            reconciled['pending'] += (
                Tasks.update(
                    status='pending',
                    ownership_uncertain=False,
                )
                .where((Tasks.id == stale_task.id)
                       & (Tasks.type == 'remote')
                       & (Tasks.status == 'creating')
                       & (Tasks.ownership_uncertain == True))
                .execute()
            )

        stale_upload_directories = []
        stale_marker_fds = {}
        for upload_directory in cls._find_remote_upload_directories(cache_directory):
            marker_path = os.path.join(
                upload_directory, cls.REMOTE_UPLOAD_ACTIVE_MARKER)
            if os.path.lexists(marker_path):
                marker_fd = None
                try:
                    marker_flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
                    marker_fd = os.open(marker_path, marker_flags)
                    marker_stat = os.fstat(marker_fd)
                    if not stat.S_ISREG(marker_stat.st_mode):
                        os.close(marker_fd)
                        continue
                    if fcntl is None:
                        os.close(marker_fd)
                        continue
                    try:
                        fcntl.flock(
                            marker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except (BlockingIOError, OSError):
                        os.close(marker_fd)
                        continue
                    marker_cutoff = (
                        current_timestamp
                        - max(grace_seconds,
                              cls.REMOTE_UPLOAD_MARKER_GRACE_SECONDS)
                    )
                    if marker_stat.st_mtime > marker_cutoff:
                        os.close(marker_fd)
                        continue
                    stale_marker_fds[upload_directory] = marker_fd
                except OSError:
                    if marker_fd is not None:
                        os.close(marker_fd)
                    # Any marker that cannot be safely inspected is treated as
                    # active rather than risking an in-flight upload.
                    continue
            try:
                modified = os.stat(
                    upload_directory, follow_symlinks=False).st_mtime
            except OSError:
                marker_fd = stale_marker_fds.pop(upload_directory, None)
                if marker_fd is not None:
                    os.close(marker_fd)
                continue
            if (upload_directory not in stale_marker_fds
                    and modified > current_timestamp - grace_seconds):
                continue
            stale_upload_directories.append(upload_directory)

        try:
            if (stale_upload_directories
                    and isinstance(database, SqliteQueueDatabase)):
                # A queue can be empty while its writer is still executing a
                # timed-out insert. This no-op write is a barrier: cleanup only
                # proceeds after every earlier ownership write has settled.
                try:
                    (Tasks.update({Tasks.status: Tasks.status})
                     .where(Tasks.id == -1)
                     .execute())
                except ResultTimeout:
                    return reconciled

            owned_paths = set()
            for (pathname,) in Tasks.select(Tasks.abspath).tuples():
                if pathname:
                    owned_paths.add(os.path.realpath(os.path.abspath(pathname)))

            for upload_directory in stale_upload_directories:
                owned = False
                for pathname in owned_paths:
                    try:
                        if (pathname != upload_directory
                                and os.path.commonpath(
                                    [upload_directory, pathname])
                                == upload_directory):
                            owned = True
                            break
                    except ValueError:
                        continue
                if owned:
                    continue
                try:
                    shutil.rmtree(upload_directory)
                    reconciled['orphaned'] += 1
                except OSError:
                    continue
        finally:
            for marker_fd in stale_marker_fds.values():
                try:
                    os.close(marker_fd)
                except OSError:
                    pass
        return reconciled

    @classmethod
    def get_recoverable_cache_directories(cls, cache_directory):
        """Return exact cache children still owned by recoverable tasks."""
        cache_root = os.path.realpath(os.path.abspath(cache_directory))
        preserved = set()
        local_query = (Tasks.select(Tasks.cache_path)
                       .where((Tasks.type == 'local')
                              & Tasks.status.in_(cls.RECOVERABLE_LOCAL_STATES)
                              & Tasks.cache_path.is_null(False)))
        remote_query = (Tasks.select(Tasks.abspath, Tasks.cache_path)
                        .where((Tasks.type == 'remote')
                               & Tasks.status.in_(cls.RECOVERABLE_REMOTE_STATES)
                               & (
                                   Tasks.abspath.is_null(False)
                                   | Tasks.cache_path.is_null(False)
                               )))
        paths = [row[0] for row in local_query.tuples()]
        for abspath, cache_path in remote_query.tuples():
            paths.extend((abspath, cache_path))
        for pathname in paths:
            if not pathname:
                continue
            resolved_file = os.path.realpath(os.path.abspath(pathname))
            resolved_directory = os.path.dirname(resolved_file)
            if (os.path.dirname(resolved_directory) != cache_root
                    or not os.path.basename(resolved_directory).startswith(
                        'unmanic_file_conversion-')):
                continue
            preserved.add(resolved_directory)
        return preserved

    def set_status(self, status):
        """
        Sets the task status to a supported lifecycle state.

        :param status:
        :return:
        """
        allowed = [
            'pending',
            'in_progress',
            'processed',
            'postprocessing',
            'checkpoint_pending',
            'history_pending',
            'completion_dispatching',
            'deletion_pending',
            'remote_delivery',
            'remote_metadata_pending',
            'complete',
        ]
        if status not in allowed:
            raise Exception('Unable to set status to "{}". Status must be one of [{}].'.format(status, ', '.join(allowed)))
        if not self.task:
            raise Exception('Unable to set status. Task has not been set!')
        self.task.status = status
        self.save()
        if status == 'complete':
            TaskDataStore.clear_task(self.task.id)

    def transition_status(self, expected_status, new_status):
        """Conditionally move a task between lifecycle owners."""
        allowed = (
            'pending',
            'in_progress',
            'processed',
            'postprocessing',
            'checkpoint_pending',
            'history_pending',
            'completion_dispatching',
            'deletion_pending',
            'remote_delivery',
            'remote_metadata_pending',
            'complete',
        )
        if expected_status not in allowed or new_status not in allowed:
            raise Exception('Unsupported task status transition.')
        if not self.task:
            raise Exception('Unable to transition status. Task has not been set!')
        updated = (Tasks.update(status=new_status)
                   .where((Tasks.id == self.task.id)
                          & (Tasks.status == expected_status))
                   .execute())
        if updated == 1:
            self.task.status = new_status
            if new_status == 'complete':
                TaskDataStore.clear_task(self.task.id)
            return True
        return False

    def persist_worker_completion(self):
        """Publish all final worker data with one conditional ownership handoff."""
        if not self.task:
            raise Exception('Unable to persist completion. Task has not been set!')
        values = {
            Tasks.status:              'processed',
            Tasks.cache_path:          self.task.cache_path,
            Tasks.success:             self.task.success,
            Tasks.start_time:          self.task.start_time,
            Tasks.finish_time:         self.task.finish_time,
            Tasks.processed_by_worker: self.task.processed_by_worker,
            Tasks.failure_category:    self.task.failure_category,
            Tasks.failure_message:     self.task.failure_message,
            Tasks.failure_time:        self.task.failure_time,
            Tasks.log:                 self.task.log,
        }
        updated = (Tasks.update(values)
                   .where((Tasks.id == self.task.id)
                          & (Tasks.status == 'in_progress'))
                   .execute())
        if updated == 1:
            self.task.status = 'processed'
            return True

        current_status = (Tasks.select(Tasks.status)
                          .where(Tasks.id == self.task.id)
                          .scalar())
        if current_status != 'in_progress':
            self.task.status = current_status
            return False
        raise Exception('Unable to publish completed worker task.')

    def set_success(self, success):
        """
        Sets the task success flag to either 'true' or 'false'

        :param success:
        :return:
        """
        if not self.task:
            raise Exception('Unable to set status. Task has not been set!')
        if success:
            self.task.success = True
        else:
            self.task.success = False
        self.save()

    def set_failure(self, category, message, failure_time=None):
        """Persist a safe, concise failure summary for completed-task history."""
        if not self.task:
            raise Exception('Unable to set failure. Task has not been set!')
        self.task.failure_category = str(category or TaskFailureCategory.INTERNAL)[:64]
        self.task.failure_message = ' '.join(str(message or '').split())[:500]
        self.task.failure_time = failure_time or time.time()
        self.save()

    def clear_failure(self):
        if not self.task:
            raise Exception('Unable to clear failure. Task has not been set!')
        self.task.failure_category = ''
        self.task.failure_message = ''
        self.task.failure_time = None
        self.save()

    @staticmethod
    def _completion_payload(destination_files, file_move_processes_success):
        return {
            'version': 1,
            'destination_files': [
                str(path) for path in (destination_files or []) if path
            ],
            'file_move_processes_success': bool(file_move_processes_success),
        }

    def _save_internal_payload(self, key, payload):
        if not self.task:
            raise Exception('Unable to save task payload. Task has not been set!')
        with self._lifecycle_lock:
            metadata, row = self._load_lifecycle_metadata()
            metadata[key] = payload
            encoded = json.dumps(metadata)
            if row is None:
                TaskLifecycle.create(
                    task=self.task.id,
                    json_blob=encoded,
                    updated_at=datetime.datetime.now(),
                )
            else:
                row.json_blob = encoded
                row.updated_at = datetime.datetime.now()
                row.save()
        return payload

    def _load_lifecycle_metadata(self):
        """Load lifecycle-only state and migrate legacy TaskMetadata payloads."""
        row = TaskLifecycle.get_or_none(TaskLifecycle.task == self.task.id)
        if row is not None:
            try:
                metadata = json.loads(row.json_blob or '{}')
            except (TypeError, ValueError):
                metadata = {}
            return (metadata if isinstance(metadata, dict) else {}), row

        legacy_row = TaskMetadata.get_or_none(
            TaskMetadata.task == self.task.id)
        legacy_internal = {}
        if legacy_row is not None:
            try:
                legacy_metadata = json.loads(legacy_row.json_blob or '{}')
            except (TypeError, ValueError):
                legacy_metadata = {}
            if isinstance(legacy_metadata, dict):
                candidate = legacy_metadata.get('__meta__')
                if isinstance(candidate, dict):
                    legacy_internal = {
                        key: deepcopy(candidate[key])
                        for key in ('completion', 'remote_delivery')
                        if key in candidate
                    }
        if legacy_internal:
            row, _created = TaskLifecycle.get_or_create(
                task=self.task.id,
                defaults={
                    'json_blob': json.dumps(legacy_internal),
                    'updated_at': datetime.datetime.now(),
                },
            )
            try:
                migrated = json.loads(row.json_blob or '{}')
            except (TypeError, ValueError):
                migrated = {}
            return (migrated if isinstance(migrated, dict) else {}), row
        return {}, None

    def _read_internal_payload(self, key):
        if not self.task:
            raise Exception('Unable to load task payload. Task has not been set!')
        with self._lifecycle_lock:
            metadata, _row = self._load_lifecycle_metadata()
            payload = metadata.get(key)
            return deepcopy(payload) if isinstance(payload, dict) else None

    def _save_completion_payload(self, payload):
        return self._save_internal_payload('completion', payload)

    def save_completion_payload(self, destination_files, file_move_processes_success):
        """Persist a validated postprocessor-completion payload for recovery."""
        payload = self._completion_payload(
            destination_files, file_move_processes_success)
        self._save_completion_payload(payload)
        return {
            'destination_files': payload['destination_files'],
            'file_move_processes_success': payload['file_move_processes_success'],
        }

    def save_completion_checkpoint(self, destination_files, file_move_processes_success):
        """Persist completion data before atomically parking history where possible."""
        if not self.task:
            raise Exception('Unable to save completion checkpoint. Task has not been set!')
        payload = self._completion_payload(
            destination_files, file_move_processes_success)
        database = getattr(Tasks._meta.database, 'obj', Tasks._meta.database)
        if isinstance(database, SqliteQueueDatabase):
            # Queued writes execute in order. If either write has ambiguous
            # ownership, recovery recognizes the valid payload while the task
            # is quarantined and advances it without repeating file work.
            self._save_completion_payload(payload)
            if not self.transition_status('checkpoint_pending', 'history_pending'):
                current_status = (Tasks.select(Tasks.status)
                                  .where(Tasks.id == self.task.id)
                                  .scalar())
                if current_status != 'history_pending':
                    raise Exception('Unable to park completion checkpoint task.')
                self.task.status = 'history_pending'
        else:
            with database.atomic():
                self._save_completion_payload(payload)
                updated = (Tasks.update(status='history_pending')
                           .where((Tasks.id == self.task.id)
                                  & (Tasks.status == 'checkpoint_pending'))
                           .execute())
                if updated != 1:
                    raise Exception('Unable to park completion checkpoint task.')
            self.task.status = 'history_pending'
        return {
            'destination_files': payload['destination_files'],
            'file_move_processes_success': payload['file_move_processes_success'],
        }

    def save_abandoned_postprocessing_checkpoint(self, message):
        """Conservatively finish a postprocessing claim that lost its runner."""
        if not self.task:
            raise Exception('Unable to recover postprocessing. Task has not been set!')
        failure_time = time.time()
        failure_message = ' '.join(str(message or '').split())[:500]
        values = {
            Tasks.success:          False,
            Tasks.failure_category: TaskFailureCategory.INTERNAL,
            Tasks.failure_message:  failure_message,
            Tasks.failure_time:     failure_time,
        }
        updated = (Tasks.update(values)
                   .where((Tasks.id == self.task.id)
                          & (Tasks.status == 'checkpoint_pending'))
                   .execute())
        if updated != 1:
            current_status = (Tasks.select(Tasks.status)
                              .where(Tasks.id == self.task.id)
                              .scalar())
            if current_status == 'history_pending':
                self.task.status = 'history_pending'
                return self.get_completion_payload()
            raise Exception('Unable to persist abandoned postprocessing failure.')
        self.task.success = False
        self.task.failure_category = TaskFailureCategory.INTERNAL
        self.task.failure_message = failure_message
        self.task.failure_time = failure_time
        return self.save_completion_checkpoint([], False)

    def _read_completion_payload(self):
        return self._read_internal_payload('completion')

    @staticmethod
    def _is_valid_completion_payload(payload):
        return bool(
            payload
            and payload.get('version') == 1
            and isinstance(payload.get('destination_files'), list)
            and all(isinstance(path, str) and path
                    for path in payload['destination_files'])
            and isinstance(payload.get('file_move_processes_success'), bool)
        )

    def has_valid_completion_payload(self):
        """Return whether recovery can safely skip postprocessing file operations."""
        return self._is_valid_completion_payload(self._read_completion_payload())

    def get_completion_payload(self):
        """Load a recovery payload, returning safe defaults for legacy tasks."""
        default = {
            'destination_files': [],
            'file_move_processes_success': False,
        }
        payload = self._read_completion_payload()
        if not self._is_valid_completion_payload(payload):
            return default
        return {
            'destination_files': list(payload['destination_files']),
            'file_move_processes_success': payload['file_move_processes_success'],
        }

    @staticmethod
    def _valid_remote_fingerprint(fingerprint):
        return (
            fingerprint is None
            or (
                isinstance(fingerprint, dict)
                and isinstance(fingerprint.get('size'), int)
                and fingerprint['size'] >= 0
                and isinstance(fingerprint.get('checksum'), str)
                and bool(fingerprint['checksum'])
            )
        )

    @classmethod
    def _is_valid_remote_delivery_claim(cls, payload):
        valid = bool(
            isinstance(payload, dict)
            and payload.get('version') == 1
            and payload.get('phase') in ('claimed', 'checkpointed')
            and all(
                isinstance(payload.get(key), str) and payload[key]
                for key in ('source_path', 'cache_path', 'destination_path')
            )
            and isinstance(payload.get('target_paths'), list)
            and bool(payload['target_paths'])
            and all(
                isinstance(path, str) and path
                for path in payload['target_paths']
            )
            and cls._valid_remote_fingerprint(
                payload.get('input_fingerprint'))
        )
        if not valid:
            return False
        ownership_fields = (
            'owner_task_id',
            'staging_token',
            'staging_directories',
        )
        if not any(field in payload for field in ownership_fields):
            return True
        if not all(field in payload for field in ownership_fields):
            return False
        owner_task_id = payload.get('owner_task_id')
        staging_token = payload.get('staging_token')
        staging_directories = payload.get('staging_directories')
        if not isinstance(owner_task_id, int) or owner_task_id <= 0:
            return False
        if staging_token is None:
            return staging_directories == []
        if (not isinstance(staging_token, str)
                or re.fullmatch(r'[0-9a-f]{32}', staging_token) is None
                or not isinstance(staging_directories, list)
                or len(staging_directories) != len(payload['target_paths'])):
            return False
        expected_name = '{}task-{}-{}'.format(
            cls.REMOTE_UPLOAD_PREFIX, owner_task_id, staging_token)
        for target_path, staging_directory in zip(
                payload['target_paths'], staging_directories):
            if (not isinstance(staging_directory, str)
                    or not staging_directory
                    or target_path != os.path.abspath(target_path)
                    or staging_directory != os.path.abspath(
                        staging_directory)
                    or os.path.dirname(os.path.abspath(target_path))
                    != os.path.abspath(staging_directory)
                    or os.path.basename(staging_directory) != expected_name):
                return False
        return True

    @classmethod
    def _is_valid_remote_delivery_checkpoint(cls, payload):
        return bool(
            cls._is_valid_remote_delivery_claim(payload)
            and payload.get('phase') == 'checkpointed'
            and isinstance(payload.get('retrieval_path'), str)
            and bool(payload['retrieval_path'])
            and (
                payload.get('output_path') is None
                or (
                    isinstance(payload.get('output_path'), str)
                    and bool(payload['output_path'])
                )
            )
            and cls._valid_remote_fingerprint(
                payload.get('output_fingerprint'))
            and isinstance(payload.get('delivery_success'), bool)
            and isinstance(payload.get('metadata_path'), str)
            and bool(payload['metadata_path'])
            and isinstance(payload.get('task_success'), bool)
            and isinstance(payload.get('failure_category'), str)
            and isinstance(payload.get('failure_message'), str)
            and (
                payload.get('failure_time') is None
                or isinstance(payload.get('failure_time'), (int, float))
            )
            and isinstance(payload.get('task_state'), dict)
            and (
                payload.get('staging_directory') is None
                or (
                    isinstance(payload.get('staging_directory'), str)
                    and bool(payload['staging_directory'])
                )
            )
        )

    def _read_remote_delivery_payload(self):
        return self._read_internal_payload('remote_delivery')

    def get_remote_delivery_payload(self):
        payload = self._read_remote_delivery_payload()
        return deepcopy(payload) if isinstance(payload, dict) else None

    def has_valid_remote_delivery_claim(self):
        return self._is_valid_remote_delivery_claim(
            self._read_remote_delivery_payload())

    def has_valid_remote_delivery_checkpoint(self):
        return self._is_valid_remote_delivery_checkpoint(
            self._read_remote_delivery_payload())

    @classmethod
    def remote_metadata_path(cls, retrieval_path):
        return os.path.join(
            os.path.dirname(os.path.abspath(retrieval_path)),
            cls.REMOTE_METADATA_DIRECTORY,
            cls.REMOTE_METADATA_FILENAME,
        )

    def save_remote_delivery_claim(self, claim):
        """Persist the exact delivery intent before any output is moved."""
        if not self.task:
            raise Exception(
                'Unable to claim remote delivery. Task has not been set!')
        payload = deepcopy(claim)
        payload['version'] = 1
        payload['phase'] = 'claimed'
        if not self._is_valid_remote_delivery_claim(payload):
            raise Exception('Invalid remote delivery claim.')
        if (payload.get('owner_task_id') is not None
                and payload['owner_task_id'] != self.get_task_id()):
            raise Exception('Remote delivery claim belongs to another task.')

        database = getattr(Tasks._meta.database, 'obj', Tasks._meta.database)
        if isinstance(database, SqliteQueueDatabase):
            self._save_internal_payload('remote_delivery', payload)
            if not self.transition_status('processed', 'remote_delivery'):
                current_status = (Tasks.select(Tasks.status)
                                  .where(Tasks.id == self.task.id)
                                  .scalar())
                if current_status != 'remote_delivery':
                    raise Exception('Unable to claim remote delivery.')
                self.task.status = 'remote_delivery'
        else:
            with database.atomic():
                self._save_internal_payload('remote_delivery', payload)
                updated = (Tasks.update(status='remote_delivery')
                           .where((Tasks.id == self.task.id)
                                  & (Tasks.status == 'processed'))
                           .execute())
                if updated != 1:
                    raise Exception('Unable to claim remote delivery.')
            self.task.status = 'remote_delivery'
        return deepcopy(payload)

    @staticmethod
    def _remote_checkpoint_failure_time(payload):
        value = payload.get('failure_time')
        if value is None:
            return None
        return datetime.datetime.fromtimestamp(float(value))

    def _apply_remote_delivery_checkpoint(self, payload):
        values = {
            Tasks.status:           'remote_metadata_pending',
            Tasks.abspath:          payload['retrieval_path'],
            Tasks.success:          payload['task_success'],
            Tasks.failure_category: payload['failure_category'],
            Tasks.failure_message:  payload['failure_message'],
            Tasks.failure_time:     self._remote_checkpoint_failure_time(payload),
        }
        updated = (Tasks.update(values)
                   .where((Tasks.id == self.task.id)
                          & Tasks.status.in_(
                              (
                                  'processed',
                                  'remote_delivery',
                                  'remote_metadata_pending',
                              )))
                   .execute())
        if updated != 1:
            current_status = (Tasks.select(Tasks.status)
                              .where(Tasks.id == self.task.id)
                              .scalar())
            if current_status != 'remote_metadata_pending':
                raise Exception(
                    'Unable to publish remote delivery checkpoint.')
        self.task.status = 'remote_metadata_pending'
        self.task.abspath = payload['retrieval_path']
        self.task.success = payload['task_success']
        self.task.failure_category = payload['failure_category']
        self.task.failure_message = payload['failure_message']
        self.task.failure_time = self._remote_checkpoint_failure_time(payload)
        return True

    def save_remote_delivery_checkpoint(self, checkpoint):
        """Persist exact retrieval data before making a remote task complete."""
        if not self.task:
            raise Exception(
                'Unable to checkpoint remote delivery. Task has not been set!')
        payload = deepcopy(checkpoint)
        payload['version'] = 1
        payload['phase'] = 'checkpointed'
        if not self._is_valid_remote_delivery_checkpoint(payload):
            raise Exception('Invalid remote delivery checkpoint.')
        if (payload.get('owner_task_id') is not None
                and payload['owner_task_id'] != self.get_task_id()):
            raise Exception('Remote delivery checkpoint belongs to another task.')

        database = getattr(Tasks._meta.database, 'obj', Tasks._meta.database)
        if isinstance(database, SqliteQueueDatabase):
            self._save_internal_payload('remote_delivery', payload)
            self._apply_remote_delivery_checkpoint(payload)
        else:
            with database.atomic():
                self._save_internal_payload('remote_delivery', payload)
                self._apply_remote_delivery_checkpoint(payload)
        return deepcopy(payload)

    def apply_remote_delivery_checkpoint(self):
        """Finish a checkpoint whose queued status update committed late."""
        payload = self._read_remote_delivery_payload()
        if not self._is_valid_remote_delivery_checkpoint(payload):
            raise Exception('No valid remote delivery checkpoint is available.')
        self._apply_remote_delivery_checkpoint(payload)
        return deepcopy(payload)

    def cleanup_remote_delivery_staging(self):
        """Remove only the exact task-owned staging directory after retrieval."""
        payload = self._read_remote_delivery_payload()
        if (not self._is_valid_remote_delivery_checkpoint(payload)
                or payload.get('owner_task_id') != self.get_task_id()
                or not payload.get('delivery_success')):
            return False
        staging_directory = payload.get('staging_directory')
        staging_token = payload.get('staging_token')
        output_path = payload.get('output_path')
        output_fingerprint = payload.get('output_fingerprint')
        if (not staging_directory or not staging_token or not output_path
                or not isinstance(output_fingerprint, dict)):
            return False

        if (staging_directory != os.path.abspath(staging_directory)
                or output_path != os.path.abspath(output_path)):
            return False
        expected_name = '{}task-{}-{}'.format(
            self.REMOTE_UPLOAD_PREFIX,
            self.get_task_id(),
            staging_token,
        )
        if (os.path.basename(staging_directory) != expected_name
                or os.path.dirname(output_path) != staging_directory
                or staging_directory not in {
                    os.path.abspath(path)
                    for path in payload.get('staging_directories', [])
                }):
            return False
        if (os.path.islink(staging_directory)
                or os.path.realpath(staging_directory) != staging_directory
                or not os.path.isdir(staging_directory)):
            return False

        allowed_parents = {
            os.path.realpath(os.path.dirname(
                os.path.abspath(payload['source_path']))),
            os.path.realpath(os.path.abspath(
                self.settings.get_cache_path())),
        }
        if os.path.dirname(staging_directory) not in allowed_parents:
            return False
        if (os.path.islink(output_path)
                or os.path.realpath(output_path) != output_path):
            return False
        try:
            output_stat = os.stat(output_path, follow_symlinks=False)
        except OSError:
            return False
        if (not stat.S_ISREG(output_stat.st_mode)
                or output_stat.st_size != output_fingerprint.get('size')
                or common.get_file_checksum(output_path)
                != output_fingerprint.get('checksum')):
            return False

        marker_path = os.path.join(
            staging_directory, self.REMOTE_STAGING_MARKER)
        if (os.path.islink(marker_path)
                or os.path.realpath(marker_path) != marker_path):
            return False
        try:
            marker_stat = os.stat(marker_path, follow_symlinks=False)
            if not stat.S_ISREG(marker_stat.st_mode):
                return False
            with open(marker_path, 'r', encoding='utf-8') as marker_file:
                marker = json.load(marker_file)
        except (OSError, TypeError, ValueError):
            return False
        if marker != {
                'version': 1,
                'task_id': self.get_task_id(),
                'token': staging_token}:
            return False

        shutil.rmtree(staging_directory)
        return True

    def modify_path(self, new_path):
        """
        Modifies the abspath attribute of this task

        :param new_path:
        :return:
        """
        if not self.task:
            raise Exception('Unable to update abspath. Task has not been set!')
        self.task.abspath = new_path
        self.save()

    def save_command_log(self, log):
        """
        Sets the task command log

        :param log:
        :return:
        """
        if not self.task:
            raise Exception('Unable to set status. Task has not been set!')
        self.task.log += ''.join(log)
        self.save()

    def save(self):
        """
        Save task model object

        :return:
        """
        if not self.task:
            raise Exception('Unable to save Task. Task has not been set!')
        self.task.save()

    def delete(self):
        """
        Delete a task model object

        :return:
        """
        if not self.task:
            raise Exception('Unable to save Task. Task has not been set!')
        TaskDataStore.clear_task(self.task.id)
        self.task.delete_instance()

    def get_total_task_list_count(self):
        task_query = Tasks.select().order_by(Tasks.id.desc())
        return task_query.count()

    def get_task_list_filtered_and_sorted(self, order=None, start=0, length=None, search_value=None, id_list=None,
                                          status=None, task_type=None, library_ids=None):
        try:
            query = (Tasks.select())

            if id_list:
                query = query.where(Tasks.id.in_(id_list))

            if search_value:
                query = query.where(Tasks.abspath.contains(search_value))

            if status:
                query = query.where(Tasks.status.in_([status]))

            if task_type:
                query = query.where(Tasks.type.in_([task_type]))

            if library_ids:
                query = query.where(Tasks.library_id.in_(library_ids))

            # Get order by
            order_by = None
            if order:
                if order.get("dir") == "asc":
                    order_by = attrgetter(order.get("column"))(Tasks).asc()
                else:
                    order_by = attrgetter(order.get("column"))(Tasks).desc()

            if order_by and length:
                query = query.order_by(order_by).limit(length).offset(start)

        except Tasks.DoesNotExist:
            # No task entries exist yet
            self.logger.warning("No tasks exist yet.")
            query = []

        return query.dicts()

    def delete_tasks_recursively(
            self, id_list, cleanup_remote_staging=False):
        """
        Deletes a given list of tasks based on their IDs

        :param id_list:
        :return:
        """
        # Prevent running if no list of IDs was given
        if not id_list:
            return False

        try:
            query = (Tasks.select())

            if id_list:
                query = query.where(Tasks.id.in_(id_list))

            for task_id in query:
                try:
                    # Remote tasks need to be cleaned up from the cache partition also
                    if task_id.type == 'remote':
                        owned_task = Task()
                        owned_task.task = task_id
                        owned_task.settings = self.settings
                        delivery_payload = (
                            owned_task.get_remote_delivery_payload())
                        if (task_id.status == 'complete'
                                and cleanup_remote_staging):
                            owned_task.cleanup_remote_delivery_staging()
                        remote_source = (
                            None if task_id.status == 'complete'
                            else task_id.abspath
                        )
                        if self._is_valid_remote_delivery_claim(delivery_payload):
                            remote_source = delivery_payload['source_path']
                        upload_directory = (
                            self._remote_upload_directory_for_path(
                                self.settings.get_cache_path(), remote_source)
                            if remote_source else None
                        )
                        if upload_directory and os.path.exists(upload_directory):
                            self.logger.info(
                                "Removing remote pending library task '%s'.",
                                remote_source,
                            )
                            shutil.rmtree(upload_directory)

                    TaskDataStore.clear_task(task_id.id)
                    task_id.delete_instance(recursive=True)
                except Exception as e:
                    # Catch delete exceptions
                    self.logger.exception("An error occurred while deleting task ID: %s. %s", task_id, e)
                    return False

            return True

        except Tasks.DoesNotExist:
            # No task entries exist yet
            self.logger.warning("No tasks currently exist.")

    def reorder_tasks(self, id_list, direction):
        # Get the task with the highest ID
        order = {
            "column": 'priority',
            "dir":    'desc',
        }
        pending_task_results = self.get_task_list_filtered_and_sorted(order=order, start=0, length=1,
                                                                      search_value=None, id_list=None, status=None)

        task_top_priority = 1
        for pending_task_result in pending_task_results:
            task_top_priority = pending_task_result.get('priority')
            break

        # Add 500 to that number to offset it above all others.
        new_priority_offset = (int(task_top_priority) + 500)

        # Update the list of tasks by ID from the database adding the priority offset to their current priority
        # If the direction is to send it to the bottom, then set the priority as 0
        query = Tasks.update(priority=Tasks.priority + new_priority_offset if (direction == "top") else 0).where(
            Tasks.id.in_(id_list))
        return query.execute()

    @staticmethod
    def set_tasks_status(id_list, status):
        """
        Updates the task status for a given list of tasks by ID

        :param id_list:
        :param status:
        :return:
        """
        if not id_list:
            return False
        if status == 'pending':
            ids = set(id_list)
            rows = list(
                Tasks.select(Tasks.id, Tasks.status)
                .where(Tasks.id.in_(ids))
                .tuples()
            )
            if {task_id for task_id, _status in rows} != ids:
                return False
            (Tasks.update(
                status='pending',
                ownership_uncertain=False,
            ).where(
                Tasks.id.in_(ids)
                & (Tasks.status == 'creating')
            ).execute())
            current_statuses = {
                current_status
                for current_status, in (
                    Tasks.select(Tasks.status)
                    .where(Tasks.id.in_(ids))
                    .tuples()
                )
            }
            return bool(current_statuses) and current_statuses.issubset({
                'pending',
                'in_progress',
                'processed',
                'postprocessing',
                'checkpoint_pending',
                'history_pending',
                'completion_dispatching',
                'deletion_pending',
                'remote_delivery',
                'remote_metadata_pending',
                'complete',
            })

        query = Tasks.update(status=status).where(Tasks.id.in_(id_list))
        result = query.execute()
        if status == 'complete' and id_list:
            for task_id in id_list:
                TaskDataStore.clear_task(task_id)
        return result

    @staticmethod
    def set_tasks_library_id(id_list, library_id):
        """
        Updates the task library_id for a given list of tasks by ID

        :param id_list:
        :param library_id:
        :return:
        """
        if not id_list:
            return False
        ids = set(id_list)
        rows = list(
            Tasks.select(Tasks.id, Tasks.status, Tasks.library_id)
            .where(Tasks.id.in_(ids))
            .tuples()
        )
        if {task_id for task_id, _status, _library_id in rows} != ids:
            return False
        (Tasks.update(library_id=library_id)
         .where(
             Tasks.id.in_(ids)
             & (Tasks.status == 'creating')
         )
         .execute())
        current_rows = list(
            Tasks.select(Tasks.status, Tasks.library_id)
            .where(Tasks.id.in_(ids))
            .tuples()
        )
        return all(
            current_library_id == library_id
            for _current_status, current_library_id in current_rows
        )


class TaskDataStore:
    """
    Thread-safe in-memory store for task lifecycle data, shared across all plugins and threads.

    There are two separate stores:

    1. Runner State (immutable)
       - Stores data emitted by individual plugin runners.
       - Once a key is set under a (task_id, plugin_id, runner), it cannot be overwritten.
       - Structure:
           {
               task_id: {
                   plugin_id: {
                       runner_function_name: {
                           key: value,
                           ...
                       },
                       ...
                   },
                   ...
               },
               ...
           }
       - Example:
           {
               42: {
                   "video_file_tester": {
                       "on_worker_process": {
                           "ffprobe": {
                               "streams": [...],
                               "format": {...}
                           }
                       }
                   }
               }
           }

    2. Task State (mutable)
       - Stores arbitrary state values for a task that plugins may update freely.
       - Structure:
           {
               task_id: {
                   key: value,
                   ...
               },
               ...
           }
       - Example:
           {
               42: {
                   "progress": 0.75,
                   "status": "running"
               }
           }
    """

    _runner_state = {}
    _task_state = {}
    _lock = threading.RLock()
    _ctx = threading.local()

    @classmethod
    def clear_task(cls, task_id):
        """
        Remove all cached state for the given task ID.

        :param task_id: Integer ID of the task to purge.
        """
        with cls._lock:
            cls._runner_state.pop(task_id, None)
            cls._task_state.pop(task_id, None)

    @classmethod
    def bind_runner_context(cls, task_id, plugin_id, runner):
        """
        Bind the current thread's runner context.

        Must be called before a plugin runner executes so that
        set_runner_value / get_runner_value know which (task_id, plugin_id, runner)
        to use.

        :param task_id: Integer ID of the task being processed.
        :param plugin_id: String identifier of the plugin.
        :param runner: String name of the runner function.
        """
        cls._ctx.task_id = task_id
        cls._ctx.plugin_id = plugin_id
        cls._ctx.runner = runner

    @classmethod
    def clear_context(cls):
        """
        Clear the current thread's runner context.

        Should be called after the plugin runner completes.
        """
        cls._ctx.task_id = None
        cls._ctx.plugin_id = None
        cls._ctx.runner = None

    @classmethod
    def set_runner_value(cls, key, value):
        """
        Store an immutable value under the bound (task_id, plugin_id, runner).

        :param key: String key to identify the data.
        :param value: Any JSON-serializable Python object to store.
        :return: True if stored successfully, False if that key already exists.
        :raises RuntimeError: if no runner context is bound.
        """
        tid = getattr(cls._ctx, 'task_id', None)
        pid = getattr(cls._ctx, 'plugin_id', None)
        run = getattr(cls._ctx, 'runner', None)
        if None in (tid, pid, run):
            raise RuntimeError("Runner context not bound")
        with cls._lock:
            task_map = dict(cls._runner_state.get(tid, {}))
            plugin_map = dict(task_map.get(pid, {}))
            runner_map = dict(plugin_map.get(run, {}))
            if key in runner_map:
                return False
            runner_map[key] = deepcopy(value)
            plugin_map[run] = runner_map
            task_map[pid] = plugin_map
            cls._runner_state[tid] = task_map
            return True

    @classmethod
    def get_runner_value(cls, key, default=None, *, plugin_id=None, runner=None):
        """
        Retrieve an immutable runner value by key.

        :param key: String key to retrieve.
        :param default: Value to return if key is not found.
        :param plugin_id: (optional) override plugin identifier.
        :param runner: (optional) override runner name.
        :return: The stored value or default.
        :raises RuntimeError: if context not bound and no override provided.
        """
        tid = getattr(cls._ctx, 'task_id', None)
        if tid is None:
            raise RuntimeError("Runner context not bound")

        pid = plugin_id if plugin_id is not None else getattr(cls._ctx, 'plugin_id', None)
        run = runner if runner is not None else getattr(cls._ctx, 'runner', None)
        if None in (pid, run):
            raise RuntimeError("Runner context not fully bound and no override provided")

        with cls._lock:
            return (
                cls._runner_state
                .get(tid, {})
                .get(pid, {})
                .get(run, {})
                .get(key, default)
            )

    @classmethod
    def set_task_state(cls, key, value, task_id=None):
        """
        Store or overwrite a mutable value for a task.

        :param key: Identifier for the state.
        :param value: JSON-serializable object.
        :param task_id: Optional task ID; if omitted, uses bound runner context.
        :raises: RuntimeError if no task_id provided and no context bound.
        """
        tid = task_id if task_id is not None else getattr(cls._ctx, 'task_id', None)
        if tid is None:
            raise RuntimeError("Task ID not provided or bound")
        with cls._lock:
            existing = cls._task_state.get(tid, {})
            new_t = dict(existing)
            new_t[key] = value
            cls._task_state[tid] = new_t

    @classmethod
    def get_task_state(cls, key, default=None, task_id=None):
        """
        Retrieve a mutable task value by key.

        :param key: Identifier to fetch.
        :param default: Returned if key missing.
        :param task_id: Optional task ID; if omitted, uses bound runner context.
        :raises: RuntimeError if no task_id provided and no context bound.
        :return: Stored value or default.
        """
        tid = task_id if task_id is not None else getattr(cls._ctx, 'task_id', None)
        if tid is None:
            raise RuntimeError("Task ID not provided or bound")
        with cls._lock:
            return cls._task_state.get(tid, {}).get(key, default)

    @classmethod
    def delete_task_state(cls, key, task_id=None):
        """
        Delete a mutable key for a given task.

        :param key: Identifier to remove.
        :param task_id: Optional task ID; if omitted, uses bound runner context.
        :raises: RuntimeError if no task_id provided and no context bound.
        """
        tid = task_id if task_id is not None else getattr(cls._ctx, 'task_id', None)
        if tid is None:
            raise RuntimeError("Task ID not provided or bound")
        with cls._lock:
            t = cls._task_state.get(tid, {})
            t.pop(key, None)
            if not t:
                cls._task_state.pop(tid, None)

    @classmethod
    def export_task_state(cls, task_id):
        """
        Export the mutable state for a specific task as a deep-copied dict.

        :param task_id: Integer ID of the task to export.
        :return: Dict of key→value for that task, or {} if none.
        """
        with cls._lock:
            return deepcopy(cls._task_state.get(task_id, {}))

    @classmethod
    def export_task_state_json(cls, task_id, **json_kwargs):
        """
        Export the mutable state for a specific task as JSON.

        :param task_id: Integer ID of the task to export.
        :param json_kwargs: Passed to json.dumps (e.g. indent=2).
        :return: JSON string.
        """
        state = cls.export_task_state(task_id)
        return json.dumps(state, **json_kwargs)

    @classmethod
    def import_task_state(cls, task_id, new_state):
        """
        Merge a dict of new_state into existing task_state for a task.

        Only adds or updates keys; existing keys not in new_state remain untouched.

        :param task_id: Integer ID of the task.
        :param new_state: Dict of key→value to merge in.
        """
        with cls._lock:
            t = cls._task_state.setdefault(task_id, {})
            for k, v in new_state.items():
                t[k] = v

    @classmethod
    def import_task_state_json(cls, task_id, json_data):
        """
        Parse a JSON string and import it into task_state for a given task,
        merging keys as in import_task_state.

        :param task_id: Integer ID of the task.
        :param json_data: JSON string produced by export_task_state_json.
        """
        parsed = json.loads(json_data)
        if not isinstance(parsed, dict):
            raise ValueError("Imported JSON must be an object/dict")
        cls.import_task_state(task_id, parsed)
