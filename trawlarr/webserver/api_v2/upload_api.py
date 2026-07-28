#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    trawlarr.upload_api.py

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
import os
import time

import tornado.log
import tornado.web

from trawlarr import config
from trawlarr.libs import session
from trawlarr.webserver.api_v2.base_api_handler import BaseApiHandler, BaseApiError

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

    bytes_read = None
    meta = None
    receiver = None

    cache_directory = None

    routes = [
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

    def prepare(self):
        self.bytes_read = 0
        self.meta = dict()
        self.receiver = self.get_receiver()

        # If max_body_size is not set, you cannot upload files > 100MB
        self.request.connection.set_max_body_size(MAX_STREAMED_SIZE)

        # Set the output path to the cache directory
        out_folder = "unmanic_remote_pending_library-{}".format(time.time())
        if not self.cache_directory:
            self.cache_directory = os.path.join(self.config.get_cache_path(), 'remote_library', out_folder)
            if not os.path.exists(self.cache_directory):
                os.makedirs(self.cache_directory)

    def data_received(self, chunk):
        self.receiver(chunk)

    def get_receiver(self):
        index = 0

        def receiver(chunk):
            nonlocal index
            if index == 0:
                index += 1
                split_chunk = chunk.split(SEPARATOR)

                self.meta['boundary'] = SEPARATOR + split_chunk[0] + b'--' + SEPARATOR
                self.meta['header'] = SEPARATOR.join(split_chunk[0:3])
                self.meta['header'] += SEPARATOR * 2
                self.meta['filename'] = split_chunk[1].split(b'=')[-1].replace(b'"', b'').decode()

                chunk = chunk[len(self.meta['header']):]
                self.fp = open(os.path.join(self.cache_directory, self.meta['filename']), "wb")
                self.fp.write(chunk)
            else:
                self.fp.write(chunk)

        return receiver

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
            self.meta['content_length'] = int(self.request.headers.get('Content-Length')) - \
                                          len(self.meta['header']) - \
                                          len(self.meta['boundary'])

            self.fp.seek(self.meta['content_length'], 0)
            self.fp.truncate()
            self.fp.close()

            # Create task entry for the file
            upload_path = os.path.join(self.cache_directory, self.meta['filename'])

            # Install plugin from zip
            from trawlarr.libs.plugins import PluginsHandler
            plugins = PluginsHandler()
            if not plugins.install_plugin_from_path_on_disk(upload_path):
                self.set_status(self.STATUS_ERROR_INTERNAL, reason="Failed to upload and install/update plugin")
                self.write_error()
                return

            self.write_success()
            return
        except BaseApiError as bae:
            tornado.log.app_log.error("BaseApiError.{}: {}".format(self.route.get('call_method'), str(bae)))
            return
        except Exception as e:
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()
