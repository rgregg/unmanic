import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest
from marshmallow import fields

from unmanic.webserver.api_v2.base_api_handler import (
    ApiErrorCode,
    BaseApiError,
    BaseApiHandler,
)
from unmanic.webserver.api_v2.filebrowser_api import ApiFilebrowserHandler
from unmanic.webserver.api_v2.history_api import ApiHistoryHandler
from unmanic.webserver.api_v2.plugins_api import ApiPluginsHandler
from unmanic.webserver.api_v2.schema.schemas import BaseSchema
from unmanic.webserver.api_v2.session_api import ApiSessionHandler


class RequiredNameSchema(BaseSchema):
    name = fields.Str(required=True)


def _handler(handler_class, method_name):
    handler = handler_class.__new__(handler_class)
    handler.application = SimpleNamespace(settings={})
    handler.request = SimpleNamespace(
        method="POST",
        path="/unmanic/api/v2/test",
        uri="/unmanic/api/v2/test",
    )
    handler.routes = [{
        "path_pattern": "/test",
        "supported_methods": ["POST"],
        "call_method": method_name,
    }]
    handler.finish = mock.Mock()
    handler._status = 200

    def set_status(status, reason=None):
        handler._status = status
        handler._reason = reason

    handler.set_status = mock.Mock(side_effect=set_status)
    handler.get_status = mock.Mock(side_effect=lambda: handler._status)
    return handler


def test_read_json_request_raises_malformed_json_without_writing_response():
    handler = _handler(BaseApiHandler, "unused")
    handler.request.body = b"{"

    with pytest.raises(BaseApiError) as raised:
        handler.read_json_request(RequiredNameSchema())

    assert raised.value.status_code == 400
    assert raised.value.error_code == ApiErrorCode.MALFORMED_JSON
    handler.finish.assert_not_called()
    handler.set_status.assert_not_called()


def test_read_json_request_rejects_invalid_utf8_as_malformed_json():
    handler = _handler(BaseApiHandler, "unused")
    handler.request.body = b'{"name":"\xff"}'

    with pytest.raises(BaseApiError) as raised:
        handler.read_json_request(RequiredNameSchema())

    assert raised.value.status_code == 400
    assert raised.value.error_code == ApiErrorCode.MALFORMED_JSON
    handler.finish.assert_not_called()
    handler.set_status.assert_not_called()


@pytest.mark.parametrize(
    ("handler_class", "method_name"),
    [
        (ApiFilebrowserHandler, "fetch_directory_listing"),
        (ApiPluginsHandler, "get_installed_plugins"),
        (ApiHistoryHandler, "get_completed_tasks"),
        (ApiSessionHandler, "get_session_state"),
    ],
)
def test_representative_endpoints_return_one_validation_error_response(
        handler_class, method_name):
    handler = _handler(handler_class, method_name)
    error = BaseApiError(
        "Failed request schema validation",
        status_code=400,
        error_code=ApiErrorCode.VALIDATION_ERROR,
        messages={"name": ["Missing data for required field."]},
    )
    if handler_class is ApiSessionHandler:
        handler.session = SimpleNamespace(
            created=True,
            level=0,
            picture_uri="",
            name="",
            email="",
            uuid="",
        )
        handler.build_response = mock.Mock(side_effect=error)
    else:
        handler.read_json_request = mock.Mock(side_effect=error)

    asyncio.run(handler.action_route())

    handler.set_status.assert_called_once_with(
        400,
        reason="Failed request schema validation",
    )
    handler.finish.assert_called_once_with({
        "error": "400: Failed request schema validation",
        "error_code": "validation_error",
        "messages": {"name": ["Missing data for required field."]},
    })


@pytest.mark.parametrize(
    ("method", "request_method", "status", "error_code", "message"),
    [
        (
            "handle_endpoint_not_found",
            "GET",
            404,
            "endpoint_not_found",
            "Endpoint not found",
        ),
        (
            "handle_method_not_allowed",
            "PATCH",
            405,
            "method_not_allowed",
            "Method 'PATCH' not allowed",
        ),
    ],
)
def test_router_errors_use_common_contract(
        method, request_method, status, error_code, message):
    handler = _handler(BaseApiHandler, "unused")
    handler.request.method = request_method

    getattr(handler, method)()

    handler.finish.assert_called_once_with({
        "error": "{}: {}".format(status, message),
        "error_code": error_code,
        "messages": {},
    })
