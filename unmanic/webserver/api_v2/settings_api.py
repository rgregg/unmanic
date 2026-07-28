#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    unmanic.settings_api.py

    Written by:               Josh.5 <jsunnex@gmail.com>
    Date:                     20 Aug 2021, (2:30 PM)

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

import tornado.log

from unmanic import config
from unmanic.libs.library import Library
from unmanic.libs.uiserver import UnmanicDataQueues
from unmanic.libs.worker_group import WorkerGroup
from unmanic.webserver.api_v2.base_api_handler import BaseApiError, BaseApiHandler
from unmanic.webserver.api_v2.schema.schemas import RequestDatabaseItemByIdSchema, RequestLibraryByIdSchema, \
    SettingsLibrariesListSchema, SettingsLibraryConfigReadAndWriteSchema, \
    SettingsLibraryPluginConfigExportSchema, \
    SettingsLibraryPluginConfigImportSchema, SettingsReadAndWriteSchema, \
    SettingsSystemConfigSchema, SettingsWorkerGroupConfigSchema, WorkerGroupsListSchema
from unmanic.webserver.helpers import plugins


class ApiSettingsHandler(BaseApiHandler):
    config = None
    params = None
    unmanic_data_queues = None

    routes = [
        {
            "path_pattern":      r"/settings/read",
            "supported_methods": ["GET"],
            "call_method":       "get_all_settings",
        },
        {
            "path_pattern":      r"/settings/write",
            "supported_methods": ["POST"],
            "call_method":       "write_settings",
        },
        {
            "path_pattern":      r"/settings/configuration",
            "supported_methods": ["GET"],
            "call_method":       "get_system_configuration",
        },
        {
            "path_pattern":      r"/settings/worker_groups",
            "supported_methods": ["GET"],
            "call_method":       "get_all_worker_groups",
        },
        {
            "path_pattern":      r"/settings/worker_group/read",
            "supported_methods": ["POST"],
            "call_method":       "read_worker_group_config",
        },
        {
            "path_pattern":      r"/settings/worker_group/write",
            "supported_methods": ["POST"],
            "call_method":       "write_worker_group_config",
        },
        {
            "path_pattern":      r"/settings/worker_group/remove",
            "supported_methods": ["DELETE"],
            "call_method":       "remove_worker_group",
        },
        {
            "path_pattern":      r"/settings/libraries",
            "supported_methods": ["GET"],
            "call_method":       "get_all_libraries",
        },
        {
            "path_pattern":      r"/settings/library/read",
            "supported_methods": ["POST"],
            "call_method":       "read_library_config",
        },
        {
            "path_pattern":      r"/settings/library/write",
            "supported_methods": ["POST"],
            "call_method":       "write_library_config",
        },
        {
            "path_pattern":      r"/settings/library/remove",
            "supported_methods": ["DELETE"],
            "call_method":       "remove_library",
        },
        {
            "path_pattern":      r"/settings/library/export",
            "supported_methods": ["POST"],
            "call_method":       "export_library_plugin_config",
        },
        {
            "path_pattern":      r"/settings/library/import",
            "supported_methods": ["POST"],
            "call_method":       "import_library_plugin_config",
        },
    ]

    def initialize(self, **kwargs):
        self.params = kwargs.get("params")
        udq = UnmanicDataQueues()
        self.unmanic_data_queues = udq.get_unmanic_data_queues()
        self.config = config.Config()

    async def get_all_settings(self):
        """
        Settings - read
        ---
        description: Returns the application settings.
        responses:
            200:
                description: 'Sample response: Returns the application settings.'
                content:
                    application/json:
                        schema:
                            SettingsReadAndWriteSchema
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
            settings = self.config.get_config_as_dict()
            response = self.build_response(
                SettingsReadAndWriteSchema(),
                {
                    "settings": settings,
                }
            )
            self.write_success(response)
            return
        except BaseApiError as bae:
            tornado.log.app_log.error("BaseApiError.{}: {}".format(self.route.get('call_method'), str(bae)))
            return
        except Exception as e:
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()

    @staticmethod
    def partition_requested_settings(requested_settings):
        """
        Split a requested settings dictionary into the items that may be persisted
        and the names of any protected items that must not be.

        Key comparison is case-insensitive because `Config.set_config_item()`
        lowercases the key before matching it against the config fields — so
        "CONFIG_PATH" would otherwise slip past the check and still be written.

        :param requested_settings:
        :return: tuple of (writable settings dict, sorted list of protected keys)
        """
        protected_keys = []
        writable_settings = {}
        for key, value in requested_settings.items():
            if key.lower() in config.API_PROTECTED_CONFIG_KEYS:
                protected_keys.append(key)
                continue
            writable_settings[key] = value
        return writable_settings, sorted(protected_keys)

    async def write_settings(self):
        """
        Settings - save a dictionary of settings
        ---
        description: >
            Save a given dictionary of settings.

            Keys that do not name a configuration field are deliberately ignored
            rather than refused. The UI posts library-scoped keys alongside
            application ones, and older clients may post fields that no longer
            exist.

            Keys naming a protected, read-only field (config_path, log_path,
            plugins_path, userdata_path) are refused with a 400 and *nothing*
            from the request is saved. These are resolved at startup from the
            command line and environment; changing them over the API would only
            desynchronise the running process from its own files.
        requestBody:
            description: Requested a dictionary of settings to save.
            required: True
            content:
                application/json:
                    schema:
                        SettingsReadAndWriteSchema
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
            json_request = self.read_json_request(SettingsReadAndWriteSchema())

            writable_settings, protected_keys = self.partition_requested_settings(json_request.get('settings', {}))
            if protected_keys:
                # Refuse the request outright rather than saving the remainder. A partial
                # save would leave the caller unable to tell what was actually applied.
                self.error_messages = {
                    'settings': ["Refused to write read-only settings: {}".format(', '.join(protected_keys))],
                }
                self.set_status(self.STATUS_ERROR_EXTERNAL, reason="Request contained read-only settings")
                self.write_error()
                return

            # Save settings - writing to file.
            # Throws exception if settings fail to save.
            # NOTE: Persist the filtered dictionary. Filtering a copy and then persisting the
            # original request payload is how this protection was silently defeated before (#18).
            self.config.set_bulk_config_items(writable_settings)

            self.write_success()
            return
        except BaseApiError as bae:
            tornado.log.app_log.error("BaseApiError.{}: {}".format(self.route.get('call_method'), str(bae)))
            return
        except Exception as e:
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()

    async def get_system_configuration(self):
        """
        Settings - read the system configuration
        ---
        description: Returns the system configuration.
        responses:
            200:
                description: 'Sample response: Returns the system configuration.'
                content:
                    application/json:
                        schema:
                            SettingsSystemConfigSchema
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
            from unmanic.libs.system import System
            system = System()
            system_info = system.info()
            response = self.build_response(
                SettingsSystemConfigSchema(),
                {
                    "configuration": system_info,
                }
            )
            self.write_success(response)
            return
        except BaseApiError as bae:
            tornado.log.app_log.error("BaseApiError.{}: {}".format(self.route.get('call_method'), str(bae)))
            return
        except Exception as e:
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()

    async def get_all_worker_groups(self):
        """
        Settings - get list of all worker groups
        ---
        description: Returns a list of all worker groups.
        responses:
            200:
                description: 'Sample response: Returns a list of all worker groups.'
                content:
                    application/json:
                        schema:
                            WorkerGroupsListSchema
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
            worker_groups = WorkerGroup.get_all_worker_groups()
            response = self.build_response(
                WorkerGroupsListSchema(),
                {
                    "worker_groups": worker_groups,
                }
            )
            self.write_success(response)
            return
        except BaseApiError as bae:
            tornado.log.app_log.error("BaseApiError.{}: {}".format(self.route.get('call_method'), str(bae)))
            return
        except Exception as e:
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()

    async def read_worker_group_config(self):
        """
        Settings - read the configuration of a worker group
        ---
        description: Read the configuration of a worker group
        requestBody:
            description: The ID of the worker group
            required: True
            content:
                application/json:
                    schema:
                        RequestDatabaseItemByIdSchema
        responses:
            200:
                description: 'Sample response: Returns the worker group configuration.'
                content:
                    application/json:
                        schema:
                            SettingsWorkerGroupConfigSchema
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
            json_request = self.read_json_request(RequestDatabaseItemByIdSchema())

            # Fetch all data for this worker group
            worker_group = WorkerGroup(json_request.get('id'))
            if not worker_group:
                self.set_status(self.STATUS_ERROR_INTERNAL, reason="Unable to find worker group config by its ID")
                self.write_error()
                return

            response = self.build_response(
                SettingsWorkerGroupConfigSchema(),
                {
                    "id":                     worker_group.get_id(),
                    "locked":                 worker_group.get_locked(),
                    "name":                   worker_group.get_name(),
                    "number_of_workers":      worker_group.get_number_of_workers(),
                    "worker_event_schedules": worker_group.get_worker_event_schedules(),
                    "tags":                   worker_group.get_tags(),
                }
            )
            self.write_success(response)
            return
        except BaseApiError as bae:
            tornado.log.app_log.error("BaseApiError.{}: {}".format(self.route.get('call_method'), str(bae)))
            return
        except Exception as e:
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()

    async def write_worker_group_config(self):
        """
        Settings - write the configuration of a worker group
        ---
        description: Write the configuration of a worker group
        requestBody:
            description: The config of a worker group that is to be saved
            required: True
            content:
                application/json:
                    schema:
                        SettingsWorkerGroupConfigSchema
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
            json_request = self.read_json_request(SettingsWorkerGroupConfigSchema())

            # Write config for this worker group
            from unmanic.webserver.helpers import settings
            settings.save_worker_group_config(json_request)

            self.write_success()
            return
        except BaseApiError as bae:
            tornado.log.app_log.error("BaseApiError.{}: {}".format(self.route.get('call_method'), str(bae)))
            return
        except Exception as e:
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()

    async def remove_worker_group(self):
        """
        Settings - remove a worker group
        ---
        description: Remove a worker group
        requestBody:
            description: Requested a worker group to remove.
            required: True
            content:
                application/json:
                    schema:
                        RequestDatabaseItemByIdSchema
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
            json_request = self.read_json_request(RequestDatabaseItemByIdSchema())

            # Fetch existing worker group by ID
            worker_group = WorkerGroup(json_request.get('id'))

            # Delete the worker group
            if not worker_group.delete():
                self.set_status(self.STATUS_ERROR_INTERNAL, reason="Failed to remove worker group by its ID")
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

    async def get_all_libraries(self):
        """
        Settings - get list of all libraries
        ---
        description: Returns a list of all libraries.
        responses:
            200:
                description: 'Sample response: Returns a list of all libraries.'
                content:
                    application/json:
                        schema:
                            SettingsLibrariesListSchema
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
            libraries = Library.get_all_libraries()
            response = self.build_response(
                SettingsLibrariesListSchema(),
                {
                    "libraries": libraries,
                }
            )
            self.write_success(response)
            return
        except BaseApiError as bae:
            tornado.log.app_log.error("BaseApiError.{}: {}".format(self.route.get('call_method'), str(bae)))
            return
        except Exception as e:
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()

    async def read_library_config(self):
        """
        Settings - read the configuration of one library
        ---
        description: Read the configuration of one library
        requestBody:
            description: The ID of the library
            required: True
            content:
                application/json:
                    schema:
                        RequestLibraryByIdSchema
        responses:
            200:
                description: 'Sample response: Returns the library configuration.'
                content:
                    application/json:
                        schema:
                            SettingsLibraryConfigReadAndWriteSchema
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
            json_request = self.read_json_request(RequestLibraryByIdSchema())

            library_settings = {
                "library_config": {
                    "id":             0,
                    "name":           '',
                    "path":           '/',
                    "enable_scanner": False,
                    "enable_inotify": False,
                    "priority_score": 0,
                },
                "plugins":        {
                    "enabled_plugins": [],
                }
            }
            if json_request.get('id'):
                # Read the library
                library_config = Library(json_request.get('id'))
                library_settings = {
                    "library_config": {
                        "id":             library_config.get_id(),
                        "name":           library_config.get_name(),
                        "path":           library_config.get_path(),
                        "locked":         library_config.get_locked(),
                        "enable_scanner": library_config.get_enable_scanner(),
                        "enable_inotify": library_config.get_enable_inotify(),
                        "priority_score": library_config.get_priority_score(),
                        "tags":           library_config.get_tags(),
                    },
                    "plugins":        {
                        "enabled_plugins": library_config.get_enabled_plugins(),
                    }
                }

            response = self.build_response(
                SettingsLibraryConfigReadAndWriteSchema(),
                library_settings
            )

            self.write_success(response)
            return
        except BaseApiError as bae:
            tornado.log.app_log.error("BaseApiError.{}: {}".format(self.route.get('call_method'), str(bae)))
            return
        except Exception as e:
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()

    async def write_library_config(self):
        """
        Settings - write the configuration of one library
        ---
        description: Write the configuration of one library
        requestBody:
            description: Requested a dictionary of settings to save.
            required: True
            content:
                application/json:
                    schema:
                        SettingsLibraryConfigReadAndWriteSchema
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
            json_request = self.read_json_request(SettingsLibraryConfigReadAndWriteSchema())

            # Save settings
            from unmanic.webserver.helpers import settings
            library_config = json_request['library_config']
            plugin_config = json_request.get('plugins', {})
            library_id = library_config.get('id', 0)
            if not settings.save_library_config(library_id, library_config=library_config, plugin_config=plugin_config):
                self.set_status(self.STATUS_ERROR_INTERNAL, reason="Failed to write library config")
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

    async def remove_library(self):
        """
        Settings - remove a library
        ---
        description: Remove a library
        requestBody:
            description: Requested a library to remove.
            required: True
            content:
                application/json:
                    schema:
                        RequestLibraryByIdSchema
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
            json_request = self.read_json_request(RequestLibraryByIdSchema())

            # Fetch existing library by ID
            library = Library(json_request.get('id'))

            # Delete the library
            if not library.delete():
                self.set_status(self.STATUS_ERROR_INTERNAL, reason="Failed to remove library by its ID")
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

    async def export_library_plugin_config(self):
        """
        Settings - export the plugin configuration of one library
        ---
        description: Export the plugin configuration of one library
        requestBody:
            description: The ID of the library
            required: True
            content:
                application/json:
                    schema:
                        RequestLibraryByIdSchema
        responses:
            200:
                description: 'Sample response: Returns the library plugin configuration.'
                content:
                    application/json:
                        schema:
                            SettingsLibraryPluginConfigExportSchema
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
            json_request = self.read_json_request(RequestLibraryByIdSchema())

            # Fetch library config
            library_config = Library.export(json_request.get('id'))

            response = self.build_response(
                SettingsLibraryPluginConfigExportSchema(),
                library_config
            )

            self.write_success(response)
            return
        except BaseApiError as bae:
            tornado.log.app_log.error("BaseApiError.{}: {}".format(self.route.get('call_method'), str(bae)))
            return
        except Exception as e:
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()

    async def import_library_plugin_config(self):
        """
        Settings - import the plugin configuration of one library
        ---
        description: Import the configuration of one library
        requestBody:
            description: Requested a dictionary of settings to save.
            required: True
            content:
                application/json:
                    schema:
                        SettingsLibraryPluginConfigImportSchema
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
            json_request = self.read_json_request(SettingsLibraryPluginConfigImportSchema())

            # Save settings
            from unmanic.webserver.helpers import settings
            library_config = json_request.get('library_config')
            plugin_config = json_request.get('plugins', {})
            library_id = json_request.get('library_id')
            if not settings.save_library_config(library_id, library_config=library_config, plugin_config=plugin_config):
                self.set_status(self.STATUS_ERROR_INTERNAL, reason="Failed to import library config")
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
