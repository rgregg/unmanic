#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_retired_central_api_surfaces.py

    Regression coverage for #21: the inherited upstream central account,
    authentication and funding endpoints are retired.

    Trawlarr has no central account service, no remote authentication and no
    funding portal. The routes that used to talk to them stay registered but
    permanently answer HTTP 410 Gone:

      - v2  GET /session/logout
      - v2  GET /session/get_app_auth_code
      - v2  GET /session/funding_proposals
      - v1  GET /api/v1/session/unmanic-sign-out-url
      - v1  GET /api/v1/session/unmanic-patreon-login-url
      - v1  GET /api/v1/session/unmanic-github-login-url
      - v1  GET /api/v1/session/unmanic-discord-login-url
      - v1  GET /api/v1/session/unmanic-patreon-page

    410 rather than 404 (which reads as "you got the URL wrong"), rather than
    200 with empty data (which reads as a half-working feature), and rather
    than the 500s some of these used to produce (which read as a broken
    install).

    Each test drives the real handler class through the real Tornado routing
    and response machinery, and asserts both halves of the contract:
      1. the documented 410 body is returned, and
      2. nothing outbound happens - neither the ``requests`` library nor the
         Session object is touched at all.
"""
import asyncio
import inspect
import json
from unittest import mock

import pytest
import requests
import tornado.httputil
import tornado.web

from trawlarr.libs.session import LOCAL_SESSION_LEVEL, Session
from trawlarr.webserver.api_v1.session_api import ApiSessionHandler as V1SessionHandler
from trawlarr.webserver.api_v2.base_api_handler import BaseApiHandler
from trawlarr.webserver.api_v2.session_api import ApiSessionHandler as V2SessionHandler

V2_RETIRED_ROUTES = [
    'session/logout',
    'session/get_app_auth_code',
    'session/funding_proposals',
]

V1_API_ROOT = '/trawlarr/api/v1'

V1_RETIRED_ROUTES = [
    'session/unmanic-sign-out-url',
    'session/unmanic-patreon-login-url',
    'session/unmanic-github-login-url',
    'session/unmanic-discord-login-url',
    'session/unmanic-patreon-page',
]


def _session_mock():
    """
    Stand-in for the Session singleton. Values match what a healthy local
    installation carries so the endpoints that are *not* retired still answer
    200. Any recorded call means a handler reached into the session.
    """
    session_mock = mock.Mock()
    session_mock.level = LOCAL_SESSION_LEVEL
    session_mock.picture_uri = ""
    session_mock.name = ""
    session_mock.email = ""
    session_mock.created = 1627793093.676484
    session_mock.uuid = "b429fcc7-9ce1-bcb3-2b8a-b094747f226e"
    session_mock.register_unmanic.return_value = True
    return session_mock


class _StubbedV2SessionHandler(V2SessionHandler):
    """
    The real v2 session handler with only ``initialize`` replaced, so the test
    needs no database, config or data queue singletons.
    """

    def initialize(self, **kwargs):
        self.session = kwargs.get('session_mock')
        self.logger = mock.Mock()
        self.params = None
        self.unmanic_data_queues = None


class _StubbedV1SessionHandler(V1SessionHandler):
    """The real v1 session handler with only ``initialize`` replaced."""

    def initialize(self, **kwargs):
        self.name = 'plugins_api'
        self.session = kwargs.get('session_mock')
        self.params = None
        self.unmanic_data_queues = None


class Response:
    """The captured HTTP response of a single handler invocation."""

    def __init__(self, code, reason, body, session_mock):
        self.code = code
        self.reason = reason
        self.body = body
        self.session_mock = session_mock

    def json(self):
        return json.loads(self.body)


def call_endpoint(handler_class, endpoint, method='GET', api_root='/trawlarr/api/v2'):
    """
    Route a request through the handler exactly as the web server would, and
    capture what it writes back on the connection.

    :param handler_class: handler class to route through
    :param endpoint: API endpoint below api_root, eg. 'session/logout'
    :param method: HTTP method
    :param api_root: the mount point of the API the handler belongs to
    :return: Response
    """
    uri = '{}/{}'.format(api_root.rstrip('/'), endpoint.lstrip('/'))
    session_mock = _session_mock()
    application = tornado.web.Application()
    connection = mock.Mock()
    captured = {'code': None, 'reason': None, 'body': b''}

    def _resolved_future():
        future = asyncio.get_event_loop().create_future()
        future.set_result(None)
        return future

    def _write_headers(start_line, headers, chunk=None):
        captured['code'] = start_line.code
        captured['reason'] = start_line.reason
        if chunk:
            captured['body'] += chunk
        return _resolved_future()

    def _write(chunk):
        captured['body'] += chunk
        return _resolved_future()

    connection.write_headers.side_effect = _write_headers
    connection.write.side_effect = _write

    request = tornado.httputil.HTTPServerRequest(
        method=method,
        uri=uri,
        connection=connection,
    )
    handler = handler_class(application, request, session_mock=session_mock)
    # Normally populated by RequestHandler._execute().
    handler._transforms = []

    async def _execute():
        result = getattr(handler, method.lower())(uri)
        if inspect.isawaitable(result):
            await result
        if not handler._finished:
            handler.finish()

    asyncio.run(_execute())

    return Response(captured['code'], captured['reason'], captured['body'], session_mock)


@pytest.fixture(autouse=True)
def no_outbound_http():
    """
    Booby-trap the only HTTP client the application uses to reach a remote
    API. Any retired endpoint that tries to phone home fails the test.
    """
    with mock.patch.object(
            requests.sessions.Session,
            'request',
            side_effect=AssertionError("A retired endpoint made an outbound HTTP request"),
    ):
        yield


def assert_retired_v2(response):
    assert response.code == 410
    body = response.json()
    assert body.get('retired') is True
    assert body.get('messages') == {}
    assert body.get('error', '').startswith('410: ')
    assert 'retired' in body.get('error', '')
    # The session must not be read from, or acted on, at all.
    assert response.session_mock.mock_calls == []


def assert_retired_v1(response):
    assert response.code == 410
    body = response.json()
    assert body.get('retired') is True
    # v1 keeps its own envelope. A retired endpoint is never a success.
    assert body.get('success') is False
    assert body.get('error', '').startswith('410: ')
    assert response.session_mock.mock_calls == []


class TestRetiredV2SessionRoutes:

    @pytest.mark.parametrize('endpoint', V2_RETIRED_ROUTES)
    def test_returns_the_documented_gone_contract(self, endpoint):
        assert_retired_v2(call_endpoint(_StubbedV2SessionHandler, endpoint))

    @pytest.mark.parametrize('endpoint', V2_RETIRED_ROUTES)
    def test_route_is_still_registered(self, endpoint):
        """
        The routes must stay registered. Deleting them would fall through to
        the generic 404 handler - "wrong URL" rather than "gone for good".
        """
        registered = {r.get('path_pattern') for r in V2SessionHandler.routes}
        assert '/{}'.format(endpoint) in registered


class TestRetiredV1SessionRoutes:

    @pytest.mark.parametrize('endpoint', V1_RETIRED_ROUTES)
    def test_returns_the_documented_gone_contract(self, endpoint):
        assert_retired_v1(call_endpoint(_StubbedV1SessionHandler, endpoint, api_root=V1_API_ROOT))

    def test_every_v1_session_route_is_retired(self):
        """
        Every route on the v1 session handler is a central account surface, so
        the whole handler is retired. Adding one back should be deliberate.
        """
        for route in V1SessionHandler.routes:
            endpoint = route.get('path_pattern').replace('/api/v1/', '')
            assert_retired_v1(call_endpoint(_StubbedV1SessionHandler, endpoint, api_root=V1_API_ROOT))


class TestLocalSessionRoutesSurvive:
    """
    The retirement must not take the local session state with it. /session/state
    and /session/reload describe the local installation and stay supported.
    """

    def test_session_state_still_returns_the_local_installation(self):
        response = call_endpoint(_StubbedV2SessionHandler, 'session/state')
        assert response.code == 200
        body = response.json()
        assert body.get('uuid') == "b429fcc7-9ce1-bcb3-2b8a-b094747f226e"
        assert body.get('level') == LOCAL_SESSION_LEVEL

    def test_session_reload_still_succeeds(self):
        response = call_endpoint(_StubbedV2SessionHandler, 'session/reload', method='POST')
        assert response.code == 200
        assert response.json().get('success') is True


class TestGoneStatusConstant:

    def test_status_error_gone_is_410(self):
        assert BaseApiHandler.STATUS_ERROR_GONE == 410


class TestSignOutKeepsPinnedLevel:
    """
    /session/logout is retired, but Session.sign_out survives for clearing
    inherited account data. It must not drop the locally pinned supporter
    level - that would lock the user out of their own features.
    """

    def test_reset_keeps_the_pinned_level(self):
        session = Session.__new__(Session)
        session.logger = mock.Mock()
        session.uuid = "test-uuid"
        session.level = LOCAL_SESSION_LEVEL
        session.name = "someone"
        session.email = "someone@example.com"
        session.picture_uri = "https://example.com/avatar.png"
        session.user_access_token = "token"
        session.application_token = "app-token"
        session.requests_session = mock.Mock()

        with mock.patch.object(session, "_Session__store_installation_data"), \
                mock.patch.object(session, "_Session__configure_log_forwarding"), \
                mock.patch.object(session, "_Session__clear_session_auth"), \
                mock.patch.object(session, "_Session__trigger_plugin_repo_refresh_for_level_change"):
            assert session.sign_out() is True

        # Account identity is cleared...
        assert session.name == ""
        assert session.email == ""
        assert session.user_access_token is None
        assert session.application_token is None
        # ...but the locally pinned level survives.
        assert session.level == LOCAL_SESSION_LEVEL
