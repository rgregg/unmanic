#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_api_error_contract.py

    The API v2 error contract (#23).

    Before this, a v2 handler could fail in several shapes. A schema
    validation failure was written by read_json_request() and then *returned*
    by the handler's except-block, so the write and the return lived in
    different files; a 404 came back without the `messages` key that every
    other error carried; nothing in any body said what kind of failure it was,
    so a frontend had only a three-digit status and an English sentence it was
    told not to parse.

    What is asserted here:

      1. TAXONOMY. Every handled failure carries an `error_code` from
         API_ERROR_CODES, and each code maps to exactly one HTTP status.
      2. SHAPE. error / error_code / messages are present on every error
         response - routing errors, validation errors, retired endpoints and
         internal errors alike.
      3. EXACTLY ONCE. A failure path writes one response. A second write is
         dropped and logged rather than raising, because the secondary
         response is what masked the real failure in #19.
      4. THE CALL SITES ARE WIRED. Every `except BaseApiError` in the v2 API
         delegates to handle_api_error(), and every broad `except Exception`
         to handle_unexpected_error(). Centralising the behaviour is worth
         nothing if a handler quietly keeps its own copy - that is the lesson
         of tests/unit/test_safety_mechanism_call_sites.py.

    The endpoint tests drive the real handler classes through the real Tornado
    routing and response machinery, the same way
    tests/unit/test_retired_central_api_surfaces.py does, so what is asserted
    is the bytes on the connection rather than a mock's call log.
"""
import ast
import asyncio
import inspect
import json
import pathlib
from unittest import mock

import pytest
import tornado.httputil
import tornado.web

from trawlarr.webserver.api_v2 import base_api_handler as base_module
from trawlarr.webserver.api_v2.base_api_handler import (
    API_ERROR_CODES,
    BaseApiError,
    BaseApiHandler,
    sanitise_reason,
)
from trawlarr.webserver.api_v2.filebrowser_api import ApiFilebrowserHandler
from trawlarr.webserver.api_v2.history_api import ApiHistoryHandler
from trawlarr.webserver.api_v2.plugins_api import ApiPluginsHandler
from trawlarr.webserver.api_v2.session_api import ApiSessionHandler

API_ROOT = '/trawlarr/api/v2'

API_V2_PACKAGE = pathlib.Path(base_module.__file__).parent

#: The keys every error response must carry, whatever went wrong.
REQUIRED_ERROR_FIELDS = ['error', 'error_code', 'messages']


# ---------------------------------------------------------------------------
# Request driver
# ---------------------------------------------------------------------------

class Response:
    """The captured HTTP response of a single handler invocation."""

    def __init__(self, code, reason, body, writes):
        self.code = code
        self.reason = reason
        self.body = body
        #: How many times the handler handed a complete response to Tornado.
        self.writes = writes

    def json(self):
        return json.loads(self.body)


def call_endpoint(handler_class, endpoint, method='POST', body=b'', initialize=None):
    """
    Route a request through a handler exactly as the web server would, and
    capture what it writes back on the connection.

    :param handler_class: the handler class under test
    :param endpoint: API endpoint below /trawlarr/api/v2, eg. 'filebrowser/list'
    :param method: HTTP method
    :param body: raw request body
    :param initialize: optional replacement for handler.initialize()
    :return: Response
    """
    uri = '{}/{}'.format(API_ROOT, endpoint.lstrip('/'))
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

    request = tornado.httputil.HTTPServerRequest(method=method, uri=uri, connection=connection, body=body)

    if initialize is not None:
        handler_class = type(
            '_Stubbed{}'.format(handler_class.__name__),
            (handler_class,),
            {'initialize': initialize},
        )

    handler = handler_class(application, request)
    # Normally populated by RequestHandler._execute().
    handler._transforms = []

    writes = {'count': 0}
    real_finish_once = handler.finish_once

    def counting_finish_once(response):
        wrote = real_finish_once(response)
        if wrote:
            writes['count'] += 1
        return wrote

    handler.finish_once = counting_finish_once

    async def _execute():
        result = getattr(handler, method.lower())(uri)
        if inspect.isawaitable(result):
            await result
        if not handler._finished:
            handler.finish()

    asyncio.run(_execute())

    return Response(captured['code'], captured['reason'], captured['body'], writes['count'])


def _inert_initialize(self, **kwargs):
    """
    Replacement for the handlers' initialize(), which otherwise builds the
    Session, config and data-queue singletons. None of the failure paths under
    test reach any of them.
    """
    self.session = mock.Mock()
    self.logger = mock.Mock()
    self.config = mock.Mock()
    self.params = None
    self.unmanic_data_queues = None


def _bare_handler():
    """A BaseApiHandler with just enough state to build an error envelope."""
    handler = BaseApiHandler.__new__(BaseApiHandler)
    handler.application = mock.Mock(settings={})
    handler.error_messages = {}
    return handler


def assert_error_envelope(response, status, error_code):
    """The whole contract, in one assertion helper."""
    body = response.json()
    assert response.code == status
    for field in REQUIRED_ERROR_FIELDS:
        assert field in body, "error response is missing '{}': {}".format(field, body)
    assert body['error_code'] == error_code
    assert API_ERROR_CODES[body['error_code']] == status
    assert body['error'].startswith('{}: '.format(status))
    assert isinstance(body['messages'], dict)
    # Exactly once.
    assert response.writes == 1


# ---------------------------------------------------------------------------
# 1. The taxonomy itself
# ---------------------------------------------------------------------------

class TestTheTaxonomy:

    def test_every_code_maps_to_exactly_one_status(self):
        for code, status in API_ERROR_CODES.items():
            assert isinstance(status, int)
            assert 400 <= status <= 599

    def test_codes_are_upper_snake_case_constants(self):
        for code in API_ERROR_CODES:
            assert code == code.upper()
            assert code.replace('_', '').isalpha()

    def test_the_four_statuses_the_handler_declares_are_all_reachable(self):
        """
        Every error status BaseApiHandler names has a code, or a caller could
        receive a status the taxonomy cannot classify.
        """
        declared = {
            BaseApiHandler.STATUS_ERROR_EXTERNAL,
            BaseApiHandler.STATUS_ERROR_ENDPOINT_NOT_FOUND,
            BaseApiHandler.STATUS_ERROR_METHOD_NOT_ALLOWED,
            BaseApiHandler.STATUS_ERROR_GONE,
            BaseApiHandler.STATUS_ERROR_INTERNAL,
        }
        assert declared <= set(API_ERROR_CODES.values())

    @pytest.mark.parametrize('status', [400, 404, 405, 410, 500])
    def test_a_bare_status_still_gets_a_code_of_the_right_status(self, status):
        handler = BaseApiHandler.__new__(BaseApiHandler)
        code = handler.error_code_for_status(status)
        assert API_ERROR_CODES[code] == status

    def test_an_unknown_error_code_is_rejected_at_the_raise(self):
        with pytest.raises(ValueError):
            BaseApiError("boom", error_code='NOT_A_REAL_CODE')


class TestBaseApiErrorCarriesTheTaxonomy:

    def test_it_defaults_to_an_internal_error(self):
        error = BaseApiError("boom")
        assert error.error_code == 'INTERNAL_ERROR'
        assert error.status_code == 500

    def test_it_does_not_write_a_response(self):
        """
        The load-bearing half of "returns exactly once": raising must not
        write. If the raise wrote and the catch wrote too, every validation
        failure would be two responses.
        """
        error = BaseApiError("boom")
        assert not hasattr(error, 'finish')
        source = inspect.getsource(BaseApiHandler.read_json_request)
        assert 'write_error' not in source
        assert 'set_status' not in source

    def test_the_reason_is_stripped_of_control_characters(self):
        error = BaseApiError("line one\r\nX-Injected: yes")
        assert '\r' not in error.reason
        assert '\n' not in error.reason

    def test_a_long_reason_is_truncated(self):
        error = BaseApiError('x' * 5000)
        assert len(error.reason) <= 400

    def test_an_empty_reason_is_never_empty_on_the_wire(self):
        assert sanitise_reason('   ') == 'Unknown'


# ---------------------------------------------------------------------------
# 2. Representative endpoints
# ---------------------------------------------------------------------------

class TestMalformedRequestBodies:
    """
    Named in #23: filebrowser, plugins, history and session. A body that is
    not JSON is the caller's fault (400) and is distinguishable from a body
    that is JSON but wrong.
    """

    CASES = [
        (ApiFilebrowserHandler, 'filebrowser/list'),
        (ApiPluginsHandler, 'plugins/installed'),
        (ApiHistoryHandler, 'history/tasks'),
    ]

    @pytest.mark.parametrize('handler_class,endpoint', CASES)
    def test_a_body_that_is_not_json_is_invalid_json(self, handler_class, endpoint):
        response = call_endpoint(handler_class, endpoint, body=b'{not json', initialize=_inert_initialize)
        assert_error_envelope(response, 400, 'INVALID_JSON')

    @pytest.mark.parametrize('handler_class,endpoint', CASES)
    def test_an_empty_body_is_invalid_json(self, handler_class, endpoint):
        response = call_endpoint(handler_class, endpoint, body=b'', initialize=_inert_initialize)
        assert_error_envelope(response, 400, 'INVALID_JSON')

    @pytest.mark.parametrize('handler_class,endpoint', CASES)
    def test_a_body_that_is_not_utf8_is_invalid_json(self, handler_class, endpoint):
        """
        json.loads() raises UnicodeDecodeError rather than JSONDecodeError for
        this. Before #23 that escaped the decode handler entirely and became a
        500 - a client error reported as a server fault.
        """
        response = call_endpoint(handler_class, endpoint, body=b'\xff\xfe\x00', initialize=_inert_initialize)
        assert_error_envelope(response, 400, 'INVALID_JSON')

    def test_a_body_that_fails_the_schema_is_a_validation_failure(self):
        response = call_endpoint(
            ApiFilebrowserHandler, 'filebrowser/list',
            body=json.dumps({'current_path': 12345}).encode(),
            initialize=_inert_initialize,
        )
        assert_error_envelope(response, 400, 'VALIDATION_FAILED')

    def test_a_validation_failure_reports_which_field_was_wrong(self):
        response = call_endpoint(
            ApiFilebrowserHandler, 'filebrowser/list',
            body=json.dumps({'current_path': 12345}).encode(),
            initialize=_inert_initialize,
        )
        assert 'current_path' in response.json()['messages']

    def test_invalid_json_and_a_schema_failure_are_told_apart(self):
        """
        Both are 400. A client that wants to say "we sent you nonsense" versus
        "you sent us the wrong shape" needs more than the status.
        """
        malformed = call_endpoint(
            ApiFilebrowserHandler, 'filebrowser/list', body=b'{not json', initialize=_inert_initialize)
        invalid = call_endpoint(
            ApiFilebrowserHandler, 'filebrowser/list',
            body=json.dumps({'current_path': 12345}).encode(), initialize=_inert_initialize)
        assert malformed.code == invalid.code == 400
        assert malformed.json()['error_code'] != invalid.json()['error_code']


class TestRoutingErrors:

    def test_an_unmatched_path_is_endpoint_not_found(self):
        response = call_endpoint(
            ApiFilebrowserHandler, 'filebrowser/no-such-thing', initialize=_inert_initialize)
        assert_error_envelope(response, 404, 'ENDPOINT_NOT_FOUND')

    def test_a_matched_path_with_the_wrong_method_is_method_not_allowed(self):
        response = call_endpoint(
            ApiFilebrowserHandler, 'filebrowser/list', method='GET', initialize=_inert_initialize)
        assert_error_envelope(response, 405, 'METHOD_NOT_ALLOWED')

    def test_routing_errors_carry_messages_like_every_other_error(self):
        """
        404 and 405 used to answer with `error` alone. A client parsing
        `messages` on every error branch got a KeyError on exactly the two
        cases it could not control.
        """
        for response in (
                call_endpoint(ApiFilebrowserHandler, 'filebrowser/nope', initialize=_inert_initialize),
                call_endpoint(ApiFilebrowserHandler, 'filebrowser/list', method='GET',
                              initialize=_inert_initialize),
        ):
            assert response.json()['messages'] == {}


class TestRetiredEndpointsKeepTheirContract:
    """
    #21 fixed the shape of a retired endpoint's answer. #23 adds a field to it
    and must not disturb the rest.
    """

    RETIRED = ['session/logout', 'session/get_app_auth_code', 'session/funding_proposals']

    @pytest.mark.parametrize('endpoint', RETIRED)
    def test_still_410_with_the_retired_flag(self, endpoint):
        response = call_endpoint(ApiSessionHandler, endpoint, method='GET', initialize=_inert_initialize)
        assert_error_envelope(response, 410, 'ENDPOINT_RETIRED')
        assert response.json()['retired'] is True


class TestInternalErrors:

    def test_a_handler_that_raises_answers_with_the_internal_error_envelope(self):
        with mock.patch(
                'trawlarr.webserver.api_v2.filebrowser_api.DirectoryListing',
                side_effect=RuntimeError("disk on fire"),
        ):
            response = call_endpoint(
                ApiFilebrowserHandler, 'filebrowser/list',
                body=json.dumps({'current_path': '/'}).encode(),
                initialize=_inert_initialize,
            )
        assert_error_envelope(response, 500, 'INTERNAL_ERROR')
        assert 'disk on fire' in response.json()['error']

    def test_an_exception_message_cannot_inject_a_response_header(self):
        """
        The reason phrase goes on the HTTP status line and is built from
        str(exception). A newline there is response splitting.
        """
        with mock.patch(
                'trawlarr.webserver.api_v2.filebrowser_api.DirectoryListing',
                side_effect=RuntimeError("boom\r\nX-Injected: yes"),
        ):
            response = call_endpoint(
                ApiFilebrowserHandler, 'filebrowser/list',
                body=json.dumps({'current_path': '/'}).encode(),
                initialize=_inert_initialize,
            )
        assert response.code == 500
        # The reason phrase is what lands on the status line.
        assert '\r' not in response.reason
        assert '\n' not in response.reason
        assert response.reason.startswith('boom')

    def test_a_session_that_was_never_created_is_a_500_not_a_success(self):
        def _uncreated_session(self, **kwargs):
            _inert_initialize(self)
            self.session.created = None

        response = call_endpoint(
            ApiSessionHandler, 'session/state', method='GET', initialize=_uncreated_session)
        assert_error_envelope(response, 500, 'INTERNAL_ERROR')


# ---------------------------------------------------------------------------
# 3. Exactly once
# ---------------------------------------------------------------------------

class _ProbeHandler(BaseApiHandler):
    """A handler whose routes do whatever the test asks of them."""

    routes = [
        {
            "path_pattern":      r"/probe",
            "supported_methods": ["POST"],
            "call_method":       "probe",
        },
        {
            # The other dispatch branch: a route that captures path arguments.
            "path_pattern":      r"/probe/(?P<thing>\w+)",
            "supported_methods": ["POST"],
            "call_method":       "probe_with_args",
        },
    ]

    behaviour = None

    def initialize(self, **kwargs):
        pass

    async def probe(self):
        await self.behaviour(self)

    async def probe_with_args(self, thing):
        await self.behaviour(self)


def _probe(behaviour, endpoint='probe'):
    handler_class = type('_Probe', (_ProbeHandler,), {'behaviour': staticmethod(behaviour)})
    return call_endpoint(handler_class, endpoint, body=b'{}')


class TestEveryFailurePathReturnsExactlyOnce:

    def test_a_handler_that_writes_twice_does_not_raise_and_the_first_wins(self):
        """
        #19: a handler wrote an error and fell through to write another. The
        second write became a generic 500 that hid the specific failure. The
        second response is now dropped; the first stands.
        """

        async def writes_twice(handler):
            handler.write_api_error(400, "the real problem", error_code='BAD_REQUEST')
            handler.write_api_error(500, "the masking problem", error_code='INTERNAL_ERROR')

        response = _probe(writes_twice)
        assert response.code == 400
        assert 'the real problem' in response.json()['error']
        assert response.writes == 1

    def test_a_success_followed_by_an_error_keeps_the_success(self):
        async def writes_then_fails(handler):
            handler.write_success({'success': True})
            raise RuntimeError("too late")

        response = _probe(writes_then_fails)
        assert response.code == 200
        assert response.json() == {'success': True}
        assert response.writes == 1

    def test_the_duplicate_write_is_logged_loudly(self):
        async def writes_twice(handler):
            handler.write_api_error(400, "first")
            handler.write_api_error(500, "second")

        with mock.patch.object(base_module.tornado.log.app_log, 'error') as logged:
            _probe(writes_twice)
        assert any('duplicate' in str(call).lower() for call in logged.call_args_list)

    def test_a_handler_with_no_error_handling_of_its_own_still_answers_json(self):
        """
        dispatch_route() is the backstop. Without it an un-caught exception
        reaches Tornado's generic HTML error page, breaking the JSON contract
        for whichever endpoint forgot its try/except.
        """

        async def unguarded(handler):
            raise RuntimeError("nobody caught me")

        response = _probe(unguarded)
        assert_error_envelope(response, 500, 'INTERNAL_ERROR')

    def test_the_backstop_covers_routes_that_take_path_arguments_too(self):
        """
        action_route() dispatches through two branches - with and without
        captured path arguments. Only one of them used to be exercised, so the
        other could quietly lose the backstop.
        """

        async def unguarded(handler):
            raise RuntimeError("nobody caught me either")

        response = _probe(unguarded, endpoint='probe/widget')
        assert_error_envelope(response, 500, 'INTERNAL_ERROR')

    def test_a_route_with_path_arguments_records_which_method_failed(self):
        """
        Error logs name self.route['call_method']. The parameterised branch
        used not to set self.route at all, so every such failure was logged
        against 'None'.
        """
        seen = {}

        async def records_route(handler):
            seen['call_method'] = handler.route.get('call_method')

        _probe(records_route, endpoint='probe/widget')
        assert seen['call_method'] == 'probe_with_args'

    def test_the_backstop_does_not_double_up_with_a_handler_that_did_catch(self):
        async def caught_it(handler):
            try:
                raise BaseApiError("bad input", error_code='BAD_REQUEST')
            except BaseApiError as bae:
                handler.handle_api_error(bae)
                return

        response = _probe(caught_it)
        assert_error_envelope(response, 400, 'BAD_REQUEST')

    def test_a_base_api_error_escaping_a_handler_is_still_classified(self):
        async def leaks(handler):
            raise BaseApiError("nope", error_code='VALIDATION_FAILED')

        response = _probe(leaks)
        assert_error_envelope(response, 400, 'VALIDATION_FAILED')


# ---------------------------------------------------------------------------
# 4. The call sites are wired to the centralised behaviour
# ---------------------------------------------------------------------------

def _v2_handler_modules():
    for path in sorted(API_V2_PACKAGE.glob('*_api.py')):
        yield path


def _outermost_except_handlers(tree, exception_name):
    """
    Yield every `except <exception_name>` clause of an endpoint method's
    outermost try block.

    Only the outermost try is the endpoint's error contract. Handlers also use
    narrow inner try/excepts - coercing a query argument to an int, say - which
    recover locally and are none of this contract's business.
    """
    for func in ast.walk(tree):
        if not isinstance(func, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for statement in func.body:
            if not isinstance(statement, ast.Try):
                continue
            for node in statement.handlers:
                if node.type is None:
                    continue
                names = []
                if isinstance(node.type, ast.Name):
                    names = [node.type.id]
                elif isinstance(node.type, ast.Tuple):
                    names = [e.id for e in node.type.elts if isinstance(e, ast.Name)]
                if exception_name in names:
                    yield func, node


def _calls_in(node):
    return {
        n.func.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }


class TestEveryCatchDelegatesToTheCentralHandler:
    """
    Centralising the behaviour is worth nothing if a handler keeps a private
    copy. These read the source: a module that grows a bespoke error path
    fails here, whatever its own tests say.
    """

    @pytest.mark.parametrize('path', list(_v2_handler_modules()), ids=lambda p: p.name)
    def test_base_api_error_is_always_routed_through_handle_api_error(self, path):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        seen = 0
        for func, handler in _outermost_except_handlers(tree, 'BaseApiError'):
            seen += 1
            assert 'handle_api_error' in _calls_in(handler), (
                "{}.{} catches BaseApiError without calling handle_api_error()".format(path.name, func.name))
        assert seen, "{} has no endpoint catching BaseApiError".format(path.name)

    @pytest.mark.parametrize('path', list(_v2_handler_modules()), ids=lambda p: p.name)
    def test_a_broad_except_is_always_routed_through_handle_unexpected_error(self, path):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        seen = 0
        for func, handler in _outermost_except_handlers(tree, 'Exception'):
            seen += 1
            assert 'handle_unexpected_error' in _calls_in(handler), (
                "{}.{} catches Exception without calling handle_unexpected_error()".format(path.name, func.name))
        assert seen, "{} has no endpoint catching Exception".format(path.name)

    @pytest.mark.parametrize('path', list(_v2_handler_modules()), ids=lambda p: p.name)
    def test_no_handler_module_finishes_a_request_behind_the_base_layers_back(self, path):
        """
        Every response is written by base_api_handler, so finish_once() can
        enforce "exactly once". A handler calling self.finish() directly
        escapes that.
        """
        source = path.read_text(encoding='utf-8')
        assert 'self.finish(' not in source, "{} finishes a response itself".format(path.name)

    def test_at_least_every_named_module_in_the_issue_is_covered(self):
        names = {p.name for p in _v2_handler_modules()}
        assert {'filebrowser_api.py', 'plugins_api.py', 'history_api.py', 'session_api.py'} <= names

    def test_finish_once_is_the_only_place_finish_is_called_in_the_base_layer(self):
        source = inspect.getsource(BaseApiHandler)
        assert source.count('self.finish(') == 1
        assert 'self.finish(' in inspect.getsource(BaseApiHandler.finish_once)


class TestTheDocumentedErrorContract:
    """
    trawlarr/webserver/api_v2/README.md is the contract a frontend author
    reads. Its taxonomy table is a behavioural claim; this pins it to the code.
    """

    @pytest.fixture(scope='class')
    def readme(self):
        return (API_V2_PACKAGE / 'README.md').read_text(encoding='utf-8')

    def test_every_code_in_the_taxonomy_is_documented_with_its_status(self, readme):
        for code, status in API_ERROR_CODES.items():
            assert '`{}`'.format(code) in readme, "{} is undocumented".format(code)
            row = [line for line in readme.splitlines() if '`{}`'.format(code) in line and line.startswith('|')]
            assert row, "{} has no row in the taxonomy table".format(code)
            assert any(str(status) in line for line in row), (
                "{} is documented with the wrong status".format(code))

    def test_the_readme_documents_no_code_the_code_does_not_define(self, readme):
        documented = {
            line.split('`')[1]
            for line in readme.splitlines()
            if line.startswith('| `') and line.count('`') >= 2
        }
        documented = {d for d in documented if d.isupper()}
        assert documented <= set(API_ERROR_CODES)

    def test_the_documented_envelope_fields_are_the_ones_written(self, readme):
        for field in REQUIRED_ERROR_FIELDS:
            assert '`{}`'.format(field) in readme
        envelope = _bare_handler().build_error_envelope(500, "boom")
        assert sorted(envelope) == sorted(REQUIRED_ERROR_FIELDS)

    def test_the_readme_example_block_matches_a_real_envelope(self, readme):
        """
        The README shows the body a 500 produces. If that block drifts from
        what the code writes, it is a false claim in the document a frontend
        author trusts most.
        """
        block = readme.split('```')[3]
        documented = json.loads(block)
        actual = _bare_handler().build_error_envelope(500, "Unable to read privacy policy.")
        assert documented == actual
