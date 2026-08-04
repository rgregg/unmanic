#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_frontend_settings_payload.py

    /settings/write now refuses a request that names a setting this version
    does not have (#24). That turns a stale key in the UI from a harmless
    no-op into a save button that fails, and nothing else in either test
    suite would notice: the Python tests never read the .vue files, and the
    frontend tests never see the config object.

    This is exactly what had already happened. SettingsLibrary.vue posted
    `enable_inotify`, a global setting removed in #52, alongside the settings
    it did own. The endpoint dropped it in silence, the page read the value
    back as `undefined` for good, and nobody found out.

    So: every key the frontend posts in a `settings: {...}` payload must be a
    field the config object has. The scan is over the whole frontend source,
    not a list of files, so a new settings page is covered the day it is
    written.
"""
import os
import re

import pytest

from trawlarr import config

FRONTEND_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'trawlarr', 'webserver', 'frontend', 'src')

#: `settings: {` followed by the object literal that /settings/write receives.
_SETTINGS_BLOCK = re.compile(r'\bsettings:\s*\{')
#: A key at the top level of that literal: `library_path: this.libraryPath,`
_KEY = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:', re.MULTILINE)


def _object_literal(source, start):
    """Return the text between the brace at `start` and its match"""
    depth = 0
    for index in range(start, len(source)):
        if source[index] == '{':
            depth += 1
        elif source[index] == '}':
            depth -= 1
            if depth == 0:
                return source[start + 1:index]
    raise AssertionError("Unbalanced braces in a settings payload")


def _settings_payloads():
    """Yield (relative path, key) for every key posted in a settings payload"""
    for directory, _dirs, files in os.walk(FRONTEND_SRC):
        for name in sorted(files):
            if not name.endswith(('.vue', '.js')):
                continue
            path = os.path.join(directory, name)
            with open(path, encoding='utf-8') as source_file:
                source = source_file.read()
            for match in _SETTINGS_BLOCK.finditer(source):
                literal = _object_literal(source, match.end() - 1)
                for key in _KEY.findall(literal):
                    yield os.path.relpath(path, FRONTEND_SRC), key


def test_the_scan_finds_the_payloads_it_is_meant_to_check():
    """A scan that matched nothing would pass every assertion below"""
    found = list(_settings_payloads())

    assert len(found) > 10
    assert ('pages/SettingsLibrary.vue', 'library_path') in found
    assert ('pages/SettingsWorkers.vue', 'worker_stall_timeout') in found


@pytest.mark.parametrize('path, key', sorted(set(_settings_payloads())))
def test_every_setting_the_ui_posts_is_a_setting_that_exists(path, key):
    assert key in config.CONFIG_FIELD_SPECS, (
        "{} posts '{}' to /settings/write, which is not a configuration field. "
        "The endpoint refuses the whole request, so this save button is broken.".format(path, key))


@pytest.mark.parametrize('path, key', sorted(set(_settings_payloads())))
def test_the_ui_never_posts_a_read_only_setting(path, key):
    assert key not in config.API_PROTECTED_CONFIG_KEYS, (
        "{} posts the read-only setting '{}', which /settings/write refuses".format(path, key))
