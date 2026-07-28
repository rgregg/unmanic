import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest

from unmanic.webserver.api_v1.session_api import ApiSessionHandler as V1SessionHandler
from unmanic.webserver.api_v2.session_api import ApiSessionHandler as V2SessionHandler


def _v2_handler():
    handler = V2SessionHandler.__new__(V2SessionHandler)
    handler.application = SimpleNamespace(settings={})
    handler.session = mock.Mock()
    handler.finish = mock.Mock()
    handler.get_status = mock.Mock(return_value=handler.STATUS_ERROR_NOT_SUPPORTED)

    def set_status(status, reason=None):
        handler._reason = reason

    handler.set_status = mock.Mock(side_effect=set_status)
    return handler


@pytest.mark.parametrize(
    "method_name",
    ["session_logout", "get_app_auth_code", "get_funding_proposals"],
)
def test_v2_central_service_endpoints_return_stable_410_without_session_calls(method_name):
    handler = _v2_handler()

    asyncio.run(getattr(handler, method_name)())

    handler.set_status.assert_called_once_with(
        410,
        reason="Upstream central account and funding services are not supported.",
    )
    handler.finish.assert_called_once_with({
        "error": "410: Upstream central account and funding services are not supported.",
        "error_code": "not_supported",
        "messages": {},
    })
    assert handler.session.mock_calls == []


@pytest.mark.parametrize("route", V1SessionHandler.routes)
def test_v1_central_service_routes_return_stable_410_without_session_calls(route):
    handler = V1SessionHandler.__new__(V1SessionHandler)
    handler.session = mock.Mock()
    handler.set_status = mock.Mock()
    handler.finish = mock.Mock()

    getattr(handler, route["call_method"])()

    handler.set_status.assert_called_once_with(
        410,
        reason="Upstream central account and funding services are not supported.",
    )
    handler.finish.assert_called_once_with({
        "error": "410: Upstream central account and funding services are not supported.",
        "messages": {},
    })
    assert handler.session.mock_calls == []
