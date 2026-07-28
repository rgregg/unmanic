import asyncio
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from unmanic.config import Config
from unmanic.webserver.api_v2.settings_api import ApiSettingsHandler


def _routed_handler(body):
    handler = ApiSettingsHandler.__new__(ApiSettingsHandler)
    handler.application = SimpleNamespace(settings={})
    handler.config = mock.Mock()
    handler.request = SimpleNamespace(
        body=json.dumps(body).encode(),
        method="POST",
        path="/unmanic/api/v2/settings/write",
        uri="/unmanic/api/v2/settings/write",
    )
    handler.finish = mock.Mock()
    handler._status = 200

    def set_status(status, reason=None):
        handler._status = status
        handler._reason = reason

    handler.set_status = mock.Mock(side_effect=set_status)
    handler.get_status = mock.Mock(side_effect=lambda: handler._status)
    return handler


@pytest.mark.parametrize(
    ("settings", "expected_messages"),
    [
        (
            {"debuging": True},
            {"settings": {"debuging": ["Unknown field."]}},
        ),
        (
            {"debugging": "true", "ui_port": "8888"},
            {
                "settings": {
                    "debugging": ["Not a valid boolean."],
                    "ui_port": ["Not a valid integer."],
                },
            },
        ),
        (
            {"ui_port": 70000, "concurrent_file_testers": 0},
            {
                "settings": {
                    "concurrent_file_testers": ["Must be greater than or equal to 1."],
                    "ui_port": ["Must be greater than or equal to 1 and less than or equal to 65535."],
                },
            },
        ),
    ],
)
def test_write_settings_returns_field_errors_without_persisting(settings, expected_messages):
    handler = _routed_handler({"settings": settings})

    asyncio.run(handler.action_route())

    handler.config.set_bulk_config_items.assert_not_called()
    handler.finish.assert_called_once_with({
        "error": "400: Failed request schema validation",
        "error_code": "validation_error",
        "messages": expected_messages,
    })


def test_write_settings_validates_and_persists_bulk_update():
    handler = _routed_handler({
        "settings": {
            "ui_port": 9999,
            "debugging": True,
            "follow_symlinks": False,
        },
    })

    asyncio.run(handler.action_route())

    handler.config.set_bulk_config_items.assert_called_once_with({
        "ui_port": 9999,
        "debugging": True,
        "follow_symlinks": False,
    })
    handler.finish.assert_called_once_with({"success": True})


def test_write_settings_persists_only_filtered_settings():
    handler = _routed_handler({
        'settings': {
            'remote_installations': {'protected': True},
            'ui_port': 9999,
            'debugging': True,
        },
    })

    asyncio.run(handler.action_route())

    handler.config.set_bulk_config_items.assert_called_once_with({
        'ui_port': 9999,
        'debugging': True,
    })
    handler.finish.assert_called_once_with({"success": True})


def test_write_settings_filters_empty_protected_setting():
    handler = ApiSettingsHandler.__new__(ApiSettingsHandler)
    handler.config = mock.Mock()
    handler.route = {'call_method': 'write_settings'}
    handler.read_json_request = mock.Mock(return_value={
        'settings': {
            'remote_installations': {},
            'follow_symlinks': False,
        },
    })
    handler.write_success = mock.Mock()

    asyncio.run(handler.write_settings())

    handler.config.set_bulk_config_items.assert_called_once_with({
        'follow_symlinks': False,
    })


@pytest.mark.parametrize(
    ("setting", "value", "current_value", "expected"),
    [
        ("ui_port", "9999", 8888, 9999),
        ("ssl_enabled", "true", False, True),
        ("debugging", "0", True, False),
        ("remote_installations", "[]", [], []),
        ("number_of_workers", "4", None, 4),
        ("worker_event_schedules", '[{"start": "01:00"}]', None,
         [{"start": "01:00"}]),
    ],
)
def test_environment_settings_preserve_runtime_types(
        setting, value, current_value, expected):
    assert Config._coerce_environment_value(
        setting, value, current_value) == expected


def test_invalid_environment_setting_retains_typed_default():
    assert Config._coerce_environment_value(
        "ui_port", "not-a-port", 8888) == 8888
    assert Config._coerce_environment_value(
        "worker_event_schedules", "[1]", None) is None
    assert Config._coerce_environment_value(
        "remote_installations", "[1]", []) == []
