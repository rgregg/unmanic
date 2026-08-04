#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    trawlarr.base_api_handler.py

    Written by:               Josh.5 <jsunnex@gmail.com>
    Date:                     26 Oct 2020, (12:15 PM)

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
import re
import sys
import traceback
from json import JSONDecodeError
from typing import (
    Any,
)

import tornado.web
import tornado.log
import tornado.routing
from marshmallow import Schema, exceptions
from tornado.ioloop import IOLoop

from tornado.web import RequestHandler


# The API v2 error taxonomy.
#
# Every handled failure of the v2 API is answered with one of these codes, in
# the ``error_code`` field of the error envelope. The code is the part a client
# is allowed to branch on: the ``error`` string is prose and may change, the
# HTTP status is coarse (three different 400s mean three different things to a
# caller), and the code is neither.
#
# The mapping is one-way and total: each code names exactly one HTTP status, so
# a client that understands only the status still gets the right status, and a
# client that understands the code gets more. Adding a code is a compatible
# change; changing the status a code maps to is not.
API_ERROR_CODES = {
    # 400 - the request itself is at fault, in three distinguishable ways
    'INVALID_JSON':       400,  # body was not decodable JSON
    'VALIDATION_FAILED':  400,  # body decoded, but failed the request schema
    'BAD_REQUEST':        400,  # rejected on its content by the endpoint
    # 404/405 - routing
    'ENDPOINT_NOT_FOUND': 404,
    'METHOD_NOT_ALLOWED': 405,
    # 410 - see write_retired() and #21
    'ENDPOINT_RETIRED':   410,
    # 500 - the server failed, the request may have been fine
    'INTERNAL_ERROR':     500,
}

# Used when only an HTTP status is known - an inline ``set_status()`` in a
# handler, or a status Tornado itself raised. Where a status has several codes
# (400 does), this names the least specific of them: guessing INVALID_JSON for
# a 400 nobody classified would be a lie, BAD_REQUEST is merely vague.
_DEFAULT_ERROR_CODE_BY_STATUS = {
    400: 'BAD_REQUEST',
    404: 'ENDPOINT_NOT_FOUND',
    405: 'METHOD_NOT_ALLOWED',
    410: 'ENDPOINT_RETIRED',
    500: 'INTERNAL_ERROR',
}

# HTTP reason phrases end up in the response start line. A newline there is a
# response-splitting bug, and reasons here are built from exception strings.
_UNSAFE_REASON_CHARS = re.compile(r'[\r\n\x00-\x1f\x7f]')
_MAX_REASON_LENGTH = 400


def sanitise_reason(reason):
    """
    Make an arbitrary string safe to use as an HTTP reason phrase.

    Control characters (notably CR and LF, which would let an exception
    message inject headers into the response) are replaced with spaces, and
    the result is truncated. The full, untruncated text still reaches the log
    and the JSON body's ``error`` field is built from the same sanitised text,
    so nothing meaningful is hidden - only made safe to put on a status line.

    :param reason: candidate reason phrase
    :return: str
    """
    text = _UNSAFE_REASON_CHARS.sub(' ', str(reason)).strip()
    if not text:
        text = 'Unknown'
    if len(text) > _MAX_REASON_LENGTH:
        text = text[:_MAX_REASON_LENGTH - 3] + '...'
    return text


class BaseApiError(Exception):
    """
    Manage errors handled by the BaseApiHandler.

    Carries the taxonomy with it. Raising one of these does not write a
    response - the handler that catches it calls ``handle_api_error()``, which
    is the only place a BaseApiError becomes an HTTP response. Keeping the
    raise and the write apart is what stops a failure path writing twice.
    """

    def __init__(self, errmsg, error_code='INTERNAL_ERROR', reason=None, messages=None):
        """
        :param errmsg: full message, for the log. May be long and multi-line.
        :param error_code: a key of API_ERROR_CODES; decides the HTTP status.
        :param reason: short single-line summary for the client. Defaults to
                       errmsg, sanitised.
        :param messages: per-field validation errors for the ``messages`` field.
        """
        Exception.__init__(self, errmsg)
        if error_code not in API_ERROR_CODES:
            raise ValueError("Unknown API error code '{}'".format(error_code))
        self.error_code = error_code
        self.status_code = API_ERROR_CODES[error_code]
        self.reason = sanitise_reason(errmsg if reason is None else reason)
        self.messages = messages or {}


class BaseApiHandler(RequestHandler):
    api_version = 2
    routes = []
    route = {}
    error_messages = {}

    """
    Valid API return status codes:
    """
    STATUS_SUCCESS = 200
    STATUS_ERROR_EXTERNAL = 400
    STATUS_ERROR_ENDPOINT_NOT_FOUND = 404
    STATUS_ERROR_METHOD_NOT_ALLOWED = 405
    STATUS_ERROR_GONE = 410
    STATUS_ERROR_INTERNAL = 500

    def set_default_headers(self):
        """
        Set the default response header to be JSON.
        This overwrites the RequestHandler method.

        :return:
        """
        self.set_header("Content-Type", 'application/json; charset="utf-8"')

    def read_json_request(self, schema: Schema):
        """

        :param schema:
        :type schema: Schema descendant
        :return:
        """
        # Ensure body can be JSON decoded
        try:
            json_data = json.loads(self.request.body)
        except (JSONDecodeError, UnicodeDecodeError) as e:
            raise BaseApiError(
                "Expected request body to be JSON. Received '{}'".format(self.request.body),
                error_code='INVALID_JSON',
                reason=str(e),
            )

        request_validation_errors = schema.validate(json_data)
        if request_validation_errors:
            raise BaseApiError(
                "Failed schema validation: {}".format(str(request_validation_errors)),
                error_code='VALIDATION_FAILED',
                reason="Failed request schema validation",
                messages=request_validation_errors,
            )

        return schema.dump(schema.load(json_data))

    def build_response(self, schema: Schema, response):
        """
        Validate the given response against a given Schema.
        Return the response data as a serialized object according to the given Schema's fields.

        :param schema:
        :param response:
        :return:
        """
        # Validate that schema.
        # This is not normally done with responses, but I want to be strict about ensuring the schema is up-to-date
        validation_errors = schema.validate(response)

        if validation_errors:
            # Throw an exception here with all the errors.
            # This will be caught and handled by the 500 internal error
            raise exceptions.ValidationError(validation_errors)

        # Build schema object from response
        data = schema.dump(response)
        return data

    def finish_once(self, response):
        """
        Finish the request with ``response`` - unless it is already finished.

        Every response this API writes goes through here. Tornado's ``finish()``
        raises ``RuntimeError`` when called twice, and a handler that writes an
        error and then falls through to write again turns a real, specific
        failure into a generic secondary 500 that hides it (#19). Neither is
        useful to a caller. The first response written is the one that stands;
        a second attempt is dropped and logged loudly, because it means a
        handler has a missing ``return``.

        :param response: JSON-serialisable body
        :return: True if this call wrote the response, False if it was dropped
        """
        if self._finished:
            tornado.log.app_log.error(
                "Discarded a duplicate API response for %s %s. The first response stands. "
                "This is a bug in the handler - a failure path is missing a return.",
                self.request.method, self.request.uri,
            )
            return False
        self.finish(response)
        return True

    def error_code_for_status(self, status_code):
        """
        The taxonomy code to report when only an HTTP status is known.

        :param status_code:
        :return: str
        """
        return _DEFAULT_ERROR_CODE_BY_STATUS.get(status_code, 'INTERNAL_ERROR')

    def build_error_envelope(self, status_code, reason, error_code=None, messages=None, exc_info=None):
        """
        Build the one JSON shape every v2 error is returned in.

        Keys, always present:
          error       - "<status>: <reason>". Prose. Not for parsing.
          error_code  - a key of API_ERROR_CODES. This is the stable part.
          messages    - dict of per-field validation errors; {} when there are none.
        Key, present only when the server is running with serve_traceback:
          traceback   - list of formatted traceback lines.

        Individual error responses may add fields on top (``retired`` on a 410
        - see #21), never remove these.

        :param status_code:
        :param reason: single-line human readable summary
        :param error_code: taxonomy code; derived from status_code when omitted
        :param messages: per-field validation errors
        :param exc_info: exception triple for the traceback field
        :return: dict
        """
        if error_code is None:
            error_code = self.error_code_for_status(status_code)
        if messages is None:
            messages = self.error_messages or {}
        response = {
            'error':      "%(code)d: %(message)s" % {"code": status_code, "message": reason},
            'error_code': error_code,
            'messages':   messages,
        }
        if self.settings.get("serve_traceback"):
            if not exc_info:
                exc_info = sys.exc_info()
            # in debug mode, try to send a traceback
            traceback_lines = []
            if exc_info and exc_info[0]:
                for line in traceback.format_exception(*exc_info):
                    traceback_lines.append(line)
            response['traceback'] = traceback_lines
        return response

    def write_api_error(self, status_code, reason, error_code=None, messages=None, extra=None):
        """
        Write a handled error response. Finishes the request.

        Handlers should reach it through ``handle_api_error()`` or
        ``handle_unexpected_error()``. The inline ``set_status()`` +
        ``write_error()`` pairs that predate this method take the other route
        into ``build_error_envelope()``; both produce the same envelope, and
        both finish through ``finish_once()``.

        :param status_code:
        :param reason: single-line human readable summary
        :param error_code: taxonomy code; derived from status_code when omitted
        :param messages: per-field validation errors
        :param extra: additional top-level fields to merge into the envelope
        :return: True if this call wrote the response, False if it was dropped
        """
        reason = sanitise_reason(reason)
        self.set_status(status_code, reason=reason)
        response = self.build_error_envelope(status_code, reason, error_code=error_code, messages=messages)
        if extra:
            response.update(extra)
        return self.finish_once(response)

    def handle_api_error(self, error):
        """
        Turn a caught ``BaseApiError`` into exactly one HTTP response.

        Every ``except BaseApiError`` in the v2 API calls this and nothing
        else. The exception carries the status, the taxonomy code and any
        validation messages, so the shape of the response does not depend on
        which handler caught it.

        :param error: the caught BaseApiError
        :return: True if this call wrote the response, False if it was dropped
        """
        tornado.log.app_log.error("BaseApiError.%s: %s", self.route.get('call_method'), str(error))
        return self.write_api_error(
            error.status_code,
            error.reason,
            error_code=error.error_code,
            messages=error.messages,
        )

    def handle_unexpected_error(self, error):
        """
        Turn an unhandled exception into exactly one HTTP 500 response.

        Every ``except Exception`` in the v2 API calls this and nothing else.

        :param error: the caught exception
        :return: True if this call wrote the response, False if it was dropped
        """
        tornado.log.app_log.error(
            "Unhandled exception in %s.%s: %s",
            self.__class__.__name__, self.route.get('call_method'), str(error), exc_info=True,
        )
        return self.write_api_error(self.STATUS_ERROR_INTERNAL, str(error), error_code='INTERNAL_ERROR')

    def write_success(self, response=None):
        """
        Write data out as HTTP code 200
        Finishes this response, ending the HTTP request.

        :param response:
        :return:
        """
        if response is None:
            response = {'success': True}
        self.set_status(self.STATUS_SUCCESS)
        return self.finish_once(response)

    def write_retired(self, message):
        """
        Write data out as HTTP code 410 Gone.
        Finishes this response, ending the HTTP request.

        Used for endpoints that Trawlarr has permanently retired because the
        service behind them (the upstream central account, authentication and
        funding APIs) is not part of this fork. 410 is chosen over 404 so that
        clients can tell "this endpoint is gone for good" apart from "you got
        the URL wrong", and over 200-with-empty-data so that no caller is
        tricked into rendering a half-working feature.

        The response body carries the standard error envelope - with the
        ``ENDPOINT_RETIRED`` taxonomy code - plus the machine-readable
        ``retired`` flag that #21 established.

        :param message: Human readable explanation of why the endpoint is gone
        :return:
        """
        return self.write_api_error(
            self.STATUS_ERROR_GONE,
            message,
            error_code='ENDPOINT_RETIRED',
            messages={},
            extra={'retired': True},
        )

    def write_error(self, status_code=None, **kwargs: Any) -> None:
        """
        Set the default error message.
        This overwrites the RequestHandler method.

        ``write_error`` may call `write`, `render`, `set_header`, etc
        to produce output as usual.

        If this error was caused by an uncaught exception (including
        HTTPError), an ``exc_info`` triple will be available as
        ``kwargs["exc_info"]``.  Note that this exception may not be
        the "current" exception for purposes of methods like
        ``sys.exc_info()`` or ``traceback.format_exc``.

        This is also Tornado's own exit for an uncaught exception, so it has to
        keep working when nothing in this module raised. It emits the same
        envelope as every other error path.

        :param status_code:
        :param kwargs:
        :return:
        """
        if status_code is None:
            status_code = self.get_status()
        response = self.build_error_envelope(
            status_code,
            self._reason,
            exc_info=kwargs.get('exc_info'),
        )
        return self.finish_once(response)

    def handle_endpoint_not_found(self):
        """
        Return a JSON 404 error message.
        Finishes this response, ending the HTTP request.

        :return:
        """
        return self.write_api_error(
            self.STATUS_ERROR_ENDPOINT_NOT_FOUND,
            "Endpoint not found",
            error_code='ENDPOINT_NOT_FOUND',
            messages={},
        )

    def handle_method_not_allowed(self):
        """
        Return a JSON 405 error message.
        Finishes this response, ending the HTTP request.

        :return:
        """
        return self.write_api_error(
            self.STATUS_ERROR_METHOD_NOT_ALLOWED,
            "Method '%(method)s' not allowed" % {"method": self.request.method},
            error_code='METHOD_NOT_ALLOWED',
            messages={},
        )

    async def action_route(self):
        """
        Determine the handler method for the route.
        Execute that handler method.
        If not method if found to handle this route,
        return 404 by exec 'handle_missing_endpoint()' method.

        :return:
        """
        request_api_base = self.request.uri.split('api/v2')[0] + 'api/v2'
        # request_api_endpoint = re.sub('^/(unmanic/)*api/v\d', '', self.request.uri)
        matched_route_with_unsupported_method = False
        for route in self.routes:
            # Get supported methods
            supported_methods = route.get("supported_methods", [])

            # Fetch the path match from this route's path pattern
            path_pattern = request_api_base + route.get("path_pattern")
            path_match = tornado.routing.PathMatches(path_pattern)
            if path_match.regex.match(self.request.path):
                # Check if this endpoint supports the request HTTP method
                if self.request.method not in supported_methods:
                    # The request's method is not supported by this route.
                    # Mark as having found a matching route, but with an un-supported HTTP method
                    matched_route_with_unsupported_method = True
                    continue

                # Check if the path matches, and get any params from a match
                params = path_match.match(self.request)

                # If we have a match and were returned some params, load that method
                if params:
                    tornado.log.app_log.debug(
                        "Routing API to {}.{}(*args={}, **kwargs={})".format(self.__class__.__name__,
                                                                             route.get("call_method"), params["path_args"],
                                                                             params["path_kwargs"]), exc_info=True)

                    # Record the route before dispatching so error logs name the
                    # method that failed rather than reporting 'None'.
                    self.route = route
                    await self.dispatch_route(route, *params["path_args"], **params["path_kwargs"])
                    return

                # This route matches the current request URI and does not have any params.
                # Set this route and call the configured method.
                tornado.log.app_log.debug("Routing API to {}.{}()".format(self.__class__.__name__, route.get("call_method")),
                                          exc_info=True)
                self.route = route
                await self.dispatch_route(route)
                return

        if matched_route_with_unsupported_method:
            tornado.log.app_log.warning("Method not allowed for API route: {}".format(self.request.uri), exc_info=True)
            self.handle_method_not_allowed()
        else:
            tornado.log.app_log.warning("No match found for API route: {}".format(self.request.uri), exc_info=True)
            self.handle_endpoint_not_found()

    async def dispatch_route(self, route, *args, **kwargs):
        """
        Call the handler method for a matched route, and guarantee a response.

        Handlers catch their own errors and route them through
        ``handle_api_error()``/``handle_unexpected_error()``. This is the
        backstop for the case where one does not: without it, a handler that
        forgets its try/except leaks the exception to Tornado's generic HTML
        error page, which is not the JSON contract this API promises. With it,
        every v2 route answers with the v2 error envelope.

        Because both this and the handler's own except-blocks end at
        ``finish_once()``, the belt and the braces cannot both write.

        :param route: the matched route definition
        :return:
        """
        try:
            await getattr(self, route.get("call_method"))(*args, **kwargs)
        except BaseApiError as bae:
            self.handle_api_error(bae)
        except Exception as e:
            self.handle_unexpected_error(e)

    async def delete(self, path):
        """
        Route all DELETE requests to the 'action_route()' method

        :param path:
        :return:
        """
        await self.action_route()

    async def get(self, path):
        """
        Route all GET requests to the 'action_route()' method

        :param path:
        :return:
        """
        await self.action_route()

    async def post(self, path):
        """
        Route all POST requests to the 'action_route()' method

        :param path:
        :return:
        """
        await self.action_route()

    async def put(self, path):
        """
        Route all PUT requests to the 'action_route()' method

        :param path:
        :return:
        """
        await self.action_route()
