#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    trawlarr.session_api.py

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
import json
import tornado.log

from trawlarr.libs import session
from trawlarr.libs.uiserver import UnmanicDataQueues
from trawlarr.webserver.api_v1.base_api_handler import BaseApiHandler

# Every route on this handler is an inherited upstream central account
# surface: OAuth sign-in links, the sign-out URL and the sponsor page. None of
# them have a service behind them in Trawlarr, so they all answer 410 Gone
# rather than reporting a generic failure that reads as a broken install.
RETIRED_ACCOUNT_MESSAGE = "This endpoint has been retired. Trawlarr has no central account service."
RETIRED_LOGIN_MESSAGE = "This endpoint has been retired. Trawlarr does not support central account sign-in."
RETIRED_FUNDING_MESSAGE = "This endpoint has been retired. Trawlarr has no funding portal."


class ApiSessionHandler(BaseApiHandler):
    name = None
    session = None
    config = None
    params = None
    unmanic_data_queues = None

    routes = [
        {
            "supported_methods": ["GET"],
            "call_method":       "get_sign_out_url",
            "path_pattern":      r"/api/v1/session/unmanic-sign-out-url",
        },
        {
            "supported_methods": ["GET"],
            "call_method":       "get_patreon_login_url",
            "path_pattern":      r"/api/v1/session/unmanic-patreon-login-url",
        },
        {
            "supported_methods": ["GET"],
            "call_method":       "get_github_login_url",
            "path_pattern":      r"/api/v1/session/unmanic-github-login-url",
        },
        {
            "supported_methods": ["GET"],
            "call_method":       "get_discord_login_url",
            "path_pattern":      r"/api/v1/session/unmanic-discord-login-url",
        },
        {
            "supported_methods": ["GET"],
            "call_method":       "get_patreon_page",
            "path_pattern":      r"/api/v1/session/unmanic-patreon-page",
        },
    ]

    def initialize(self, **kwargs):
        self.name = 'plugins_api'
        self.session = session.Session()
        self.params = kwargs.get("params")
        udq = UnmanicDataQueues()
        self.unmanic_data_queues = udq.get_unmanic_data_queues()

    def set_default_headers(self):
        """Set the default response header to be JSON."""
        self.set_header("Content-Type", 'application/json; charset="utf-8"')

    def get(self, path):
        self.action_route()

    def post(self, path):
        self.action_route()

    def write_retired(self, message):
        """
        Answer a retired endpoint with HTTP 410 Gone.

        The v1 API predates the v2 error envelope, so the body keeps the v1
        ``success`` field (always False here) and adds the same
        machine-readable ``retired`` marker the v2 API uses.

        :param message: Human readable explanation of why the endpoint is gone
        :return:
        """
        self.set_status(410, reason=message)
        self.write(json.dumps({
            "success": False,
            "retired": True,
            "error":   "410: {}".format(message),
        }))

    def get_sign_out_url(self, *args, **kwargs):
        self.write_retired(RETIRED_ACCOUNT_MESSAGE)

    def get_patreon_login_url(self, *args, **kwargs):
        self.write_retired(RETIRED_LOGIN_MESSAGE)

    def get_github_login_url(self, *args, **kwargs):
        self.write_retired(RETIRED_LOGIN_MESSAGE)

    def get_discord_login_url(self, *args, **kwargs):
        self.write_retired(RETIRED_LOGIN_MESSAGE)

    def get_patreon_page(self, *args, **kwargs):
        self.write_retired(RETIRED_FUNDING_MESSAGE)
