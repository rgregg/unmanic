#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_settings_api_write.py

    Regression coverage for the /settings/write contract, see #18.

    The original bug was a pattern, not a typo: write_settings() built a
    filtered copy of the requested settings, dropped the protected field from
    the copy, and then handed the *unfiltered* original to
    set_bulk_config_items(). The filter therefore protected nothing.

    The field it failed to protect (remote_installations) no longer exists —
    it went with the Link feature — so these tests pin the mechanism instead,
    against the protected keys that do exist today:

      - a request naming a protected key is refused with a 400 and *nothing*
        from that request reaches the config object;
      - the refusal is case-insensitive, because Config.set_config_item()
        lowercases keys before matching them;
      - ordinary settings are still persisted, and exactly what the caller
        asked for is what gets written;
      - unknown keys and values of the wrong type are refused with a
        field-level message, and nothing from that request is saved (#24).
        The UI used to post a library-scoped `enable_inotify` here, which
        Config dropped in silence; the field it read has not existed since
        #52, and the page no longer sends it.

    write_settings() is exercised directly on an un-initialised handler
    instance with the Tornado plumbing stubbed out. That is deliberate: a test
    of the pure partition helper alone would not have caught the original bug,
    which lived in what the handler did with the helper's output.
"""

import asyncio

import pytest

from trawlarr import config
from trawlarr.webserver.api_v2.settings_api import ApiSettingsHandler


class RecordingConfig:
    """Stands in for unmanic.config.Config, recording what it is asked to save"""

    def __init__(self):
        self.saved = []

    def set_bulk_config_items(self, items, save_settings=True):
        self.saved.append(dict(items))


def build_handler(requested_settings):
    """
    Build an ApiSettingsHandler that is callable without a Tornado application.

    Only the request/response plumbing is stubbed. The method under test,
    write_settings(), runs unmodified.
    """
    handler = ApiSettingsHandler.__new__(ApiSettingsHandler)
    handler.route = {'call_method': 'write_settings'}
    handler.error_messages = {}
    handler.config = RecordingConfig()
    handler.status = None
    handler.reason = None
    handler.errors_written = 0
    handler.successes_written = 0

    handler.read_json_request = lambda schema: {'settings': requested_settings}

    def set_status(status_code, reason=None):
        handler.status = status_code
        handler.reason = reason

    def write_error(status_code=None, **kwargs):
        handler.errors_written += 1

    def write_success(response=None):
        handler.successes_written += 1
        handler.status = 200

    handler.set_status = set_status
    handler.write_error = write_error
    handler.write_success = write_success
    return handler


def run_write_settings(requested_settings):
    handler = build_handler(requested_settings)
    asyncio.run(handler.write_settings())
    return handler


def test_protected_keys_are_not_empty():
    """A vacuous protected-key set would make every other test here pass for free"""
    assert config.API_PROTECTED_CONFIG_KEYS
    assert 'config_path' in config.API_PROTECTED_CONFIG_KEYS


@pytest.mark.parametrize('protected_key', sorted(config.API_PROTECTED_CONFIG_KEYS))
def test_protected_field_is_rejected_and_never_persisted(protected_key):
    handler = run_write_settings({protected_key: '/tmp/attacker-controlled'})

    assert handler.status == 400
    assert handler.errors_written == 1
    assert handler.successes_written == 0
    # The whole point of #18: nothing at all reached the config object
    assert handler.config.saved == []
    assert protected_key in handler.error_messages['settings'][0]


def test_protected_field_is_not_smuggled_in_alongside_valid_settings():
    """
    This is the exact shape of the original bug. The valid settings made the
    request look ordinary while the protected field rode along in the payload
    that was actually persisted.
    """
    handler = run_write_settings({
        'cache_path':  '/tmp/cache',
        'config_path': '/tmp/attacker-controlled',
        'debugging':   True,
    })

    assert handler.status == 400
    assert handler.config.saved == []


def test_protected_field_rejection_is_case_insensitive():
    """Config.set_config_item() lowercases keys, so the guard must too"""
    handler = run_write_settings({'Config_Path': '/tmp/attacker-controlled'})

    assert handler.status == 400
    assert handler.config.saved == []


def test_ordinary_settings_are_persisted_unchanged():
    requested = {
        'cache_path':             '/tmp/cache',
        'debugging':              True,
        'enable_library_scanner': False,
    }
    handler = run_write_settings(requested)

    assert handler.status == 200
    assert handler.successes_written == 1
    assert handler.errors_written == 0
    assert handler.config.saved == [requested]


def test_empty_request_persists_an_empty_dict():
    handler = run_write_settings({})

    assert handler.status == 200
    assert handler.config.saved == [{}]


# ---------------------------------------------------------------------------
# #24: a write that names nothing, or carries an unusable value, is refused
# ---------------------------------------------------------------------------

def test_an_unknown_key_is_refused_and_nothing_is_saved():
    handler = run_write_settings({'enable_libary_scanner': True})

    assert handler.status == 400
    assert handler.errors_written == 1
    assert handler.config.saved == []
    # Field-level, and it names the field the caller most likely meant
    message = handler.error_messages['enable_libary_scanner'][0]
    assert 'Unknown setting' in message
    assert 'enable_library_scanner' in message


def test_a_value_of_the_wrong_type_is_refused_and_nothing_is_saved():
    handler = run_write_settings({'worker_stall_timeout': 'as long as it takes'})

    assert handler.status == 400
    assert handler.config.saved == []
    assert 'worker_stall_timeout' in handler.error_messages


def test_a_value_outside_the_allowed_range_is_refused():
    handler = run_write_settings({'worker_stall_timeout': 5})

    assert handler.status == 400
    assert handler.config.saved == []
    assert str(config.MINIMUM_WORKER_STALL_TIMEOUT) in handler.error_messages['worker_stall_timeout'][0]


def test_one_bad_key_refuses_the_whole_request():
    """
    Same rule as the protected keys: no partial saves. A caller that got a
    400 must not have to work out which half of its request applied.
    """
    handler = run_write_settings({
        'library_path': '/library',
        'debugging':    'yes please',
    })

    assert handler.status == 400
    assert handler.config.saved == []
    assert 'debugging' in handler.error_messages
    assert 'library_path' not in handler.error_messages


def test_every_offending_field_is_reported_not_just_the_first():
    handler = run_write_settings({
        'debugging':          'yes please',
        'nonsense':           1,
        'ui_port':            999999,
        'enable_library_scanner': True,
    })

    assert handler.status == 400
    assert sorted(handler.error_messages) == ['debugging', 'nonsense', 'ui_port']


def test_a_valid_bulk_update_is_saved_with_its_values_typed():
    """
    The success path, including the conversion: a number sent as a string
    (an HTML number input, an older client) is stored as a number.
    """
    handler = run_write_settings({
        'library_path':               '/library',
        'enable_library_scanner':     True,
        'schedule_full_scan_minutes': '720',
        'concurrent_file_testers':    2,
        'worker_stall_timeout':       120,
    })

    assert handler.status == 200
    assert handler.errors_written == 0
    assert handler.config.saved == [{
        'library_path':               '/library',
        'enable_library_scanner':     True,
        'schedule_full_scan_minutes': 720,
        'concurrent_file_testers':    2,
        'worker_stall_timeout':       120,
    }]


def test_a_protected_key_is_still_refused_as_protected_not_as_unknown():
    """
    The two checks are separate and the protected one runs first. A read-only
    field is a real field; saying "unknown setting" about it would be a lie.
    """
    handler = run_write_settings({'config_path': '/tmp/attacker-controlled'})

    assert handler.status == 400
    assert handler.config.saved == []
    assert 'read-only' in handler.error_messages['settings'][0]
