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
      - unknown keys are passed through deliberately (the UI posts
        library-scoped keys such as enable_inotify alongside application ones,
        and Config drops them silently).

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


def test_unknown_keys_are_passed_through_rather_than_refused():
    """
    Documented contract: unknown keys are ignored, not rejected. The UI posts
    library-scoped keys (enable_inotify) to this endpoint; Config discards
    anything that is not one of its own fields.
    """
    handler = run_write_settings({'library_path': '/library', 'enable_inotify': True})

    assert handler.status == 200
    assert handler.config.saved == [{'library_path': '/library', 'enable_inotify': True}]


def test_empty_request_persists_an_empty_dict():
    handler = run_write_settings({})

    assert handler.status == 200
    assert handler.config.saved == [{}]
