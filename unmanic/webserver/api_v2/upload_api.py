#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    unmanic.upload_api.py

    Written by:               Josh.5 <jsunnex@gmail.com>
    Date:                     01 Oct 2021, (12:55 AM)

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
import ntpath
import os
import re
import stat
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - remote uploads are served on POSIX hosts
    fcntl = None

import tornado.log
import tornado.web

from unmanic import config
from unmanic.libs import common, session
from unmanic.libs.frontend_push_messages import FrontendPushMessages
from unmanic.libs.task import Task, TaskCreationOwnershipUncertain
from unmanic.webserver.api_v2.base_api_handler import (
    ApiErrorCode,
    BaseApiError,
    BaseApiHandler,
)
from unmanic.webserver.api_v2.schema.schemas import PendingTasksTableResultsSchema
from unmanic.webserver.helpers import pending_tasks

# CONST
MB = 1024 * 1024
GB = 1024 * MB
TB = 1024 * GB
MAX_STREAMED_SIZE = 100 * TB
SEPARATOR = b'\r\n'


@tornado.web.stream_request_body
class ApiUploadHandler(BaseApiHandler):
    session = None
    params = None
    config = None
    frontend_messages = None

    bytes_read = None
    meta = None
    receiver = None

    cache_directory = None

    routes = [
        {
            "path_pattern":      r"/upload/pending/file",
            "supported_methods": ["POST"],
            "call_method":       "upload_file_to_pending_tasks",
        },
        {
            "path_pattern":      r"/upload/plugin/file",
            "supported_methods": ["POST"],
            "call_method":       "upload_and_install_plugin",
        },
    ]

    def initialize(self, **kwargs):
        self.session = session.Session()
        self.params = kwargs.get("params")
        self.config = config.Config()
        self.frontend_messages = FrontendPushMessages()

    def prepare(self):
        self.bytes_read = 0
        self.meta = dict()
        self.fp = None
        self._upload_error = None
        self._owns_cache_directory = False
        self._cache_directory_identity = None
        self._active_upload_marker_fd = None
        self._request_cleanup_complete = False
        self._preserve_pending_upload = False
        upload_type = "pending"
        if "upload/plugin/file" in self.request.uri:
            upload_type = "plugin"

        # If max_body_size is not set, you cannot upload files > 100MB
        self.request.connection.set_max_body_size(MAX_STREAMED_SIZE)

        self._create_owned_cache_directory()
        try:
            self._create_active_upload_marker()
        except Exception:
            self._cleanup_failed_pending_upload(None)
            raise
        self.receiver = self.get_receiver(upload_type)

    def data_received(self, chunk):
        if self._upload_error is not None:
            return
        try:
            self.receiver(chunk)
            marker_fd = getattr(self, '_active_upload_marker_fd', None)
            if marker_fd is not None:
                os.utime(marker_fd, None)
        except BaseApiError as error:
            self._upload_error = error
            self._close_pending_upload(self.meta.get('pathname'))
        except Exception:
            tornado.log.app_log.exception("Unable to receive streamed upload")
            self._upload_error = BaseApiError(
                "Failed to receive upload",
                status_code=self.STATUS_ERROR_INTERNAL,
                error_code=ApiErrorCode.INTERNAL_ERROR,
            )
            self._close_pending_upload(self.meta.get('pathname'))

    @staticmethod
    def _filename_error(reason):
        return BaseApiError(
            "Invalid upload filename",
            status_code=BaseApiHandler.STATUS_ERROR_EXTERNAL,
            error_code=ApiErrorCode.VALIDATION_ERROR,
            messages={'filename': [reason]},
        )

    @classmethod
    def _validate_upload_filename(cls, filename):
        if not isinstance(filename, str) or not filename:
            raise cls._filename_error("A filename is required.")
        if '\x00' in filename:
            raise cls._filename_error("NUL bytes are not allowed.")
        # Both slash styles are rejected on every host, so a filename accepted
        # on POSIX cannot become a path when handled by a Windows peer.
        separators = {'/', '\\', os.sep}
        if os.altsep:
            separators.add(os.altsep)
        if any(separator in filename for separator in separators):
            raise cls._filename_error(
                "Directory separators are not allowed.")
        if filename in ('.', '..'):
            raise cls._filename_error("Dot path components are not allowed.")
        if filename == Task.REMOTE_METADATA_DIRECTORY:
            raise cls._filename_error(
                "This filename is reserved for remote task metadata.")
        if os.path.isabs(filename) or ntpath.isabs(filename):
            raise cls._filename_error("Absolute paths are not allowed.")
        if ntpath.splitdrive(filename)[0]:
            raise cls._filename_error(
                "Drive-qualified filenames are not allowed.")
        return filename

    def _create_owned_cache_directory(self):
        cache_root = os.path.realpath(
            os.path.abspath(self.config.get_cache_path()))
        remote_root = os.path.join(cache_root, Task.REMOTE_UPLOAD_DIRECTORY)
        os.makedirs(remote_root, mode=0o700, exist_ok=True)
        if (os.path.islink(remote_root)
                or os.path.dirname(os.path.realpath(remote_root)) != cache_root):
            raise RuntimeError("Remote upload cache is not a direct cache child")

        for _attempt in range(10):
            out_folder = "{}{}".format(
                Task.REMOTE_UPLOAD_PREFIX, uuid.uuid4().hex)
            cache_directory = os.path.join(remote_root, out_folder)
            try:
                os.mkdir(cache_directory, mode=0o700)
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError("Unable to allocate a unique upload directory")

        resolved_directory = os.path.realpath(cache_directory)
        if (os.path.dirname(resolved_directory) != os.path.realpath(remote_root)
                or resolved_directory != os.path.abspath(cache_directory)):
            os.rmdir(cache_directory)
            raise RuntimeError("Unsafe remote upload cache path")

        directory_stat = os.stat(cache_directory, follow_symlinks=False)
        self.cache_directory = cache_directory
        self._owned_cache_directory = resolved_directory
        self._cache_directory_identity = (
            directory_stat.st_dev, directory_stat.st_ino)
        self._owns_cache_directory = True

    def _open_owned_cache_directory(self):
        if not getattr(self, '_owns_cache_directory', False):
            return None
        cache_directory = getattr(self, 'cache_directory', None)
        owned_directory = getattr(self, '_owned_cache_directory', None)
        if (not cache_directory or not owned_directory
                or os.path.abspath(cache_directory) != owned_directory):
            return None
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        try:
            directory_fd = os.open(cache_directory, flags)
        except OSError:
            return None
        directory_stat = os.fstat(directory_fd)
        if ((directory_stat.st_dev, directory_stat.st_ino)
                != getattr(self, '_cache_directory_identity', None)):
            os.close(directory_fd)
            return None
        return directory_fd

    def _create_active_upload_marker(self):
        directory_fd = self._open_owned_cache_directory()
        if directory_fd is None:
            raise RuntimeError("Upload directory ownership changed")
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        try:
            marker_fd = os.open(
                Task.REMOTE_UPLOAD_ACTIVE_MARKER,
                flags,
                0o600,
                dir_fd=directory_fd,
            )
        finally:
            os.close(directory_fd)
        if fcntl is None:
            os.close(marker_fd)
            raise RuntimeError("Cross-process upload locks are unavailable")
        fcntl.flock(marker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(marker_fd, str(os.getpid()).encode('ascii'))
        self._active_upload_marker_fd = marker_fd

    def _release_active_upload_marker(self):
        marker_fd = getattr(self, '_active_upload_marker_fd', None)
        directory_fd = self._open_owned_cache_directory()
        if marker_fd is not None:
            try:
                fcntl.flock(marker_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(marker_fd)
            except OSError:
                pass
            self._active_upload_marker_fd = None
        if directory_fd is None:
            return
        try:
            os.unlink(Task.REMOTE_UPLOAD_ACTIVE_MARKER, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError as error:
            tornado.log.app_log.error(
                "Unable to remove remote upload activity marker. %s", error)
        finally:
            os.close(directory_fd)

    def _safe_upload_path(self, filename):
        filename = self._validate_upload_filename(filename)
        owned_directory = getattr(self, '_owned_cache_directory', None)
        if not owned_directory:
            raise RuntimeError("Upload directory ownership is unavailable")
        intended_path = os.path.join(owned_directory, filename)
        resolved_path = os.path.realpath(intended_path)
        if (os.path.dirname(resolved_path) != owned_directory
                or resolved_path != intended_path):
            raise self._filename_error(
                "The filename does not resolve to a direct upload child.")
        return intended_path

    def _open_upload_file(self, filename):
        pathname = self._safe_upload_path(filename)
        directory_fd = self._open_owned_cache_directory()
        if directory_fd is None:
            raise RuntimeError("Upload directory ownership changed")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        try:
            file_fd = os.open(filename, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError as error:
            raise self._filename_error(
                "A file with this name already exists.") from error
        except OSError as error:
            if error.errno in (errno.EEXIST, errno.ELOOP):
                raise self._filename_error(
                    "A file with this name already exists.") from error
            raise
        finally:
            os.close(directory_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(file_fd)
            raise self._filename_error(
                "The upload destination is not a regular file.")
        self.meta['pathname'] = pathname
        self.fp = os.fdopen(file_fd, 'wb')

    def _close_pending_upload(self, pathname):
        fp = getattr(self, 'fp', None)
        try:
            if fp is not None and not getattr(fp, 'closed', False):
                fp.close()
        except Exception as e:
            tornado.log.app_log.error("Unable to close failed remote upload '%s'. %s", pathname, str(e))

    def _cleanup_failed_pending_upload(self, pathname):
        self._release_active_upload_marker()
        directory_fd = self._open_owned_cache_directory()
        if directory_fd is None:
            tornado.log.app_log.error(
                "Refusing to clean an upload directory not owned by this request")
            return
        try:
            if pathname:
                pathname = os.path.abspath(pathname)
                if os.path.dirname(pathname) != self._owned_cache_directory:
                    tornado.log.app_log.error(
                        "Refusing to clean failed upload outside its request cache: '%s'",
                        pathname,
                    )
                    return
                try:
                    os.unlink(os.path.basename(pathname), dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    tornado.log.app_log.error(
                        "Unable to remove failed remote upload '%s'. %s",
                        pathname, error)
        finally:
            os.close(directory_fd)

        try:
            os.rmdir(self.cache_directory)
        except FileNotFoundError:
            pass
        except OSError as error:
            tornado.log.app_log.error(
                "Unable to remove failed remote upload cache '%s'. %s",
                self.cache_directory,
                error,
            )
        self._request_cleanup_complete = True

    def _cleanup_unregistered_pending_upload(self, pathname):
        self._close_pending_upload(pathname)
        self._cleanup_failed_pending_upload(pathname)

    def _mark_pending_upload_ownership_uncertain(self, pathname):
        self._close_pending_upload(pathname)
        self._preserve_pending_upload = True
        self._release_active_upload_marker()
        try:
            # Start the orphan grace period when queue ownership becomes
            # ambiguous, not when a potentially long upload began.
            os.utime(self.cache_directory, None)
        except OSError as e:
            tornado.log.app_log.error(
                "Unable to mark remote upload '%s' for reconciliation. %s",
                pathname,
                str(e),
            )

    def _raise_streaming_upload_error(self):
        error = getattr(self, '_upload_error', None)
        if error is not None:
            raise error
        if not self.meta.get('pathname') or getattr(self, 'fp', None) is None:
            raise self._filename_error(
                "A valid multipart file field is required.")

    def on_finish(self):
        if getattr(self, '_request_cleanup_complete', False):
            return
        pathname = getattr(self, 'meta', {}).get('pathname')
        self._close_pending_upload(pathname)
        if getattr(self, '_preserve_pending_upload', False):
            self._release_active_upload_marker()
            self._request_cleanup_complete = True
        else:
            self._cleanup_failed_pending_upload(pathname)

    def on_connection_close(self):
        self.on_finish()
        super().on_connection_close()

    def get_receiver(self, upload_type):
        index = 0
        frontend_messages = self.frontend_messages

        def receiver(chunk):
            nonlocal index
            if index == 0:
                index += 1
                header_end = chunk.find(SEPARATOR * 2)
                if header_end < 0:
                    raise self._filename_error(
                        "Malformed multipart file headers.")
                header = chunk[:header_end]
                header_lines = header.split(SEPARATOR)
                if not header_lines:
                    raise self._filename_error(
                        "Malformed multipart file headers.")
                filename_match = re.search(
                    br'(?:^|;)\s*filename="([^"]*)"', header,
                    flags=re.IGNORECASE)
                if not filename_match:
                    raise self._filename_error(
                        "A multipart filename is required.")
                try:
                    filename = filename_match.group(1).decode('utf-8')
                except UnicodeDecodeError as error:
                    raise self._filename_error(
                        "The filename must be valid UTF-8.") from error
                self._validate_upload_filename(filename)

                self.meta['boundary'] = (
                    SEPARATOR + header_lines[0] + b'--' + SEPARATOR)
                self.meta['header'] = chunk[:header_end + len(SEPARATOR * 2)]
                self.meta['filename'] = filename
                self._open_upload_file(filename)

                if frontend_messages:
                    if upload_type == 'pending':
                        frontend_messages.update(
                            {
                                'id':      'receivingRemoteFile',
                                'type':    'status',
                                'code':    'receivingRemoteFile',
                                'message': self.meta['filename'],
                                'timeout': 0
                            }
                        )

                chunk = chunk[len(self.meta['header']):]
                self.fp.write(chunk)
            else:
                self.fp.write(chunk)

        return receiver

    async def upload_file_to_pending_tasks(self):
        """
        Upload - upload a new pending task
        ---
        description: Uploads a file to the pending tasks list
        requestBody:
            description: Uploads a file to the pending tasks list
            required: True
            content:
                multipart/form-data:
                    schema:
                        type: object
                        properties:
                            fileName:
                                type: string
                                format: binary
        responses:
            200:
                description: 'Successful request; Returns data for the generated task'
                content:
                    application/json:
                        schema:
                            PendingTasksTableResultsSchema
            400:
                description: Bad request; Check `messages` for any validation errors
                content:
                    application/json:
                        schema:
                            BadRequestSchema
            404:
                description: Bad request; Requested endpoint not found
                content:
                    application/json:
                        schema:
                            BadEndpointSchema
            405:
                description: Bad request; Requested method is not allowed
                content:
                    application/json:
                        schema:
                            BadMethodSchema
            500:
                description: Internal error; Check `error` for exception
                content:
                    application/json:
                        schema:
                            InternalErrorSchema
        """
        pathname = None
        task_registered = False
        try:
            self._raise_streaming_upload_error()
            pathname = self.meta['pathname']
            # TODO: Add POST endpoint to receive metadata or a recipe pertaining to this uploaded file (for future when plugins can be sent with the file).
            self.meta['content_length'] = int(self.request.headers.get('Content-Length')) - \
                                          len(self.meta['header']) - \
                                          len(self.meta['boundary'])

            if self.frontend_messages:
                self.frontend_messages.update(
                    {
                        'id':      'receivingRemoteFile',
                        'type':    'status',
                        'code':    'receivingRemoteFile',
                        'message': '',
                        'timeout': 0
                    }
                )

            self.fp.seek(self.meta['content_length'], 0)
            self.fp.truncate()
            self.fp.close()

            # Remove frontend status message
            if self.frontend_messages:
                self.frontend_messages.remove_item('receivingRemoteFile')

            # Create task entry for the file
            task_info = pending_tasks.add_remote_tasks(pathname)
            if not task_info:
                self._cleanup_unregistered_pending_upload(pathname)
                self.set_status(
                    self.STATUS_ERROR_INTERNAL,
                    reason="Failed to add uploaded file to pending tasks",
                )
                self.write_error()
                return
            task_registered = True
            self._preserve_pending_upload = True

            # TODO: Make this optional
            checksum = common.get_file_checksum(task_info.get('abspath'))

            # Return the details of the generated task
            response = self.build_response(
                PendingTasksTableResultsSchema(),
                {
                    "id":       task_info.get('id'),
                    "abspath":  task_info.get('abspath'),
                    "priority": task_info.get('priority'),
                    "type":     task_info.get('type'),
                    "status":   task_info.get('status'),
                    "checksum": checksum
                }
            )
            self.write_success(response)
            return
        except TaskCreationOwnershipUncertain as e:
            self._mark_pending_upload_ownership_uncertain(pathname)
            tornado.log.app_log.error(
                "Task ownership is indeterminate for remote upload '%s'; preserving it for recovery. %s",
                pathname,
                str(e),
            )
            if self.frontend_messages:
                self.frontend_messages.remove_item('receivingRemoteFile')
            self.set_status(
                self.STATUS_ERROR_INTERNAL,
                reason="Failed to add uploaded file to pending tasks",
            )
            self.write_error()
            return
        except BaseApiError:
            if not task_registered:
                self._cleanup_unregistered_pending_upload(pathname)
            if self.frontend_messages:
                self.frontend_messages.remove_item('receivingRemoteFile')
            raise
        except Exception as e:
            if not task_registered:
                self._cleanup_unregistered_pending_upload(pathname)
            if self.frontend_messages:
                self.frontend_messages.remove_item('receivingRemoteFile')
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()

    async def upload_and_install_plugin(self):
        """
        Upload - upload a plugin and install it
        ---
        description: Uploads a plugin ZIP file and installs it
        requestBody:
            description: Uploads a plugin ZIP file and installs it
            required: True
            content:
                multipart/form-data:
                    schema:
                        type: object
                        properties:
                            fileName:
                                type: string
                                format: binary
        responses:
            200:
                description: 'Successful request; Returns success status'
                content:
                    application/json:
                        schema:
                            BaseSuccessSchema
            400:
                description: Bad request; Check `messages` for any validation errors
                content:
                    application/json:
                        schema:
                            BadRequestSchema
            404:
                description: Bad request; Requested endpoint not found
                content:
                    application/json:
                        schema:
                            BadEndpointSchema
            405:
                description: Bad request; Requested method is not allowed
                content:
                    application/json:
                        schema:
                            BadMethodSchema
            500:
                description: Internal error; Check `error` for exception
                content:
                    application/json:
                        schema:
                            InternalErrorSchema
        """
        try:
            self._raise_streaming_upload_error()
            self.meta['content_length'] = int(self.request.headers.get('Content-Length')) - \
                                          len(self.meta['header']) - \
                                          len(self.meta['boundary'])

            self.fp.seek(self.meta['content_length'], 0)
            self.fp.truncate()
            self.fp.close()

            # Create task entry for the file
            upload_path = self.meta['pathname']

            # Install plugin from zip
            from unmanic.libs.plugins import PluginsHandler
            plugins = PluginsHandler()
            if not plugins.install_plugin_from_path_on_disk(upload_path):
                self.set_status(self.STATUS_ERROR_INTERNAL, reason="Failed to upload and install/update plugin")
                self.write_error()
                return

            self.write_success()
            return
        except BaseApiError:
            if self.frontend_messages:
                self.frontend_messages.remove_item('receivingRemoteFile')
            raise
        except Exception as e:
            if self.frontend_messages:
                self.frontend_messages.remove_item('receivingRemoteFile')
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()
