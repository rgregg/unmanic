#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    unmanic.session_api.py

    Written by:               Josh.5 <jsunnex@gmail.com>
    Date:                     10 Mar 2021, (7:14 PM)

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

from unmanic.libs import session
from unmanic.libs.logs import UnmanicLogging
from unmanic.libs.uiserver import UnmanicDataQueues
from unmanic.webserver.api_v2.base_api_handler import BaseApiHandler, BaseApiError
from unmanic.webserver.api_v2.schema.schemas import SessionStateSuccessSchema


class ApiSessionHandler(BaseApiHandler):
    STATUS_ERROR_NOT_SUPPORTED = 410
    CENTRAL_SERVICES_DISABLED_REASON = (
        "Upstream central account and funding services are not supported."
    )
    session = None
    config = None
    logger = None
    params = None
    unmanic_data_queues = None

    routes = [
        {
            "path_pattern":      r"/session/state",
            "supported_methods": ["GET"],
            "call_method":       "get_session_state",
        },
        {
            "path_pattern":      r"/session/reload",
            "supported_methods": ["POST"],
            "call_method":       "session_reload",
        },
        {
            "path_pattern":      r"/session/logout",
            "supported_methods": ["GET"],
            "call_method":       "session_logout",
        },
        {
            "path_pattern":      r"/session/get_app_auth_code",
            "supported_methods": ["GET"],
            "call_method":       "get_app_auth_code",
        },
        {
            "path_pattern":      r"/session/funding_proposals",
            "supported_methods": ["GET"],
            "call_method":       "get_funding_proposals",
        },
    ]

    def initialize(self, **kwargs):
        self.session = session.Session()
        self.logger = UnmanicLogging.get_logger(name=__class__.__name__)
        self.params = kwargs.get("params")
        udq = UnmanicDataQueues()
        self.unmanic_data_queues = udq.get_unmanic_data_queues()

    async def get_session_state(self):
        """
        Session - state
        ---
        description: Returns the application session state.
        responses:
            200:
                description: 'Sample response: Returns the application session state.'
                content:
                    application/json:
                        schema:
                            SessionStateSuccessSchema
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
            if not self.session.created:
                self.set_status(self.STATUS_ERROR_INTERNAL, reason="Session has not yet been created.")
                self.write_error()
                return
            else:
                response = self.build_response(
                    SessionStateSuccessSchema(),
                    {
                        "level":       self.session.level,
                        "picture_uri": self.session.picture_uri,
                        "name":        self.session.name,
                        "email":       self.session.email,
                        "created":     self.session.created,
                        "uuid":        self.session.uuid,
                    }
                )
                self.write_success(response)
                return
        except BaseApiError:
            raise
        except Exception as e:
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()

    async def session_reload(self):
        """
        Session - reload
        ---
        description: Reload the current session.
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
            if not self.session.register_unmanic(force=True):
                self.set_status(self.STATUS_ERROR_INTERNAL, reason="Failed to reload session")
                self.write_error()
                return
            else:
                self.write_success()
                return
        except BaseApiError:
            raise
        except Exception as e:
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()

    async def session_logout(self):
        """
        Session - disabled central account logout
        ---
        description: The upstream central account service is not supported by this distribution.
        responses:
            410:
                description: Central account logout is not supported.
                content:
                    application/json:
                        schema:
                            BaseErrorSchema
        """
        self._write_central_service_disabled()

    async def get_app_auth_code(self):
        """
        Session - disabled application authentication
        ---
        description: The upstream central account service is not supported by this distribution.
        responses:
            410:
                description: Central account authentication is not supported.
                content:
                    application/json:
                        schema:
                            BaseErrorSchema
        """
        self._write_central_service_disabled()

    async def get_funding_proposals(self):
        """
        Session - funding proposals
        ---
        description: The upstream central funding service is not supported by this distribution.
        responses:
            410:
                description: Central funding proposals are not supported.
                content:
                    application/json:
                        schema:
                            BaseErrorSchema
        """
        self._write_central_service_disabled()

    def _write_central_service_disabled(self):
        """Return BaseApiHandler's stable JSON error shape with HTTP 410."""
        self.set_status(
            self.STATUS_ERROR_NOT_SUPPORTED,
            reason=self.CENTRAL_SERVICES_DISABLED_REASON,
        )
        self.write_error()
