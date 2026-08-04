#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_config_validation.py

    Configuration writes used to succeed no matter what they contained.

    A key that named nothing was dropped without a word, so a typed
    `enable_libary_scanner` returned 200, wrote nothing, and left the
    operator looking at a scanner that would not turn on. A value of the
    wrong type was stored exactly as sent, so `worker_stall_timeout: "abc"`
    saved cleanly and then quietly became 300 seconds inside a getter, and
    `schedule_full_scan_minutes: "1440"` saved a string that every later
    reader had to cope with. Both are the shape #24 asks for: a write that
    reports success and does not do what it says.

    Two policies, deliberately different (see #82: prefer loud over silent,
    but a check that fires on healthy configuration is worse than no check):

      * an API write is a deliberate act by a client that is present to be
        told - unknown keys and bad values are refused, whole request, with
        a field-level message. That half is pinned in
        tests/unit/test_settings_api_write.py.
      * reading settings.json is not. A file written by an older build may
        legitimately name settings removed in #49/#52, and refusing to start
        over one would be a far worse failure than the one being fixed. So
        the reader warns and carries on, keeping the default for anything it
        cannot use.

    The table `CONFIG_FIELD_SPECS` and the `Config` object are checked
    against each other here, because a field missing from the table opts
    itself out of validation silently, which is the bug being fixed wearing
    a different hat.
"""
import json
import logging

import pytest

from trawlarr import config
from trawlarr.config import ConfigFieldSpec, validate_config_items


# ---------------------------------------------------------------------------
# The specification table must cover the config object, and nothing else
# ---------------------------------------------------------------------------

def _drop_cached_config():
    type(config.Config)._instances.pop(config.Config, None)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A Config rooted in a temporary HOME, as in test_config_runtime_paths"""
    monkeypatch.setenv('HOME_DIR', str(tmp_path))
    for key in config.DERIVED_PATH_CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)
    _drop_cached_config()
    yield tmp_path
    _drop_cached_config()


@pytest.fixture
def fresh_config(home):
    _drop_cached_config()
    return config.Config()


def test_every_config_field_has_a_spec(fresh_config):
    missing = sorted(set(fresh_config.get_config_keys()) - set(config.CONFIG_FIELD_SPECS))
    assert not missing, "config fields with no entry in CONFIG_FIELD_SPECS: {}".format(missing)


def test_the_spec_table_names_no_field_that_does_not_exist(fresh_config):
    extra = sorted(set(config.CONFIG_FIELD_SPECS) - set(fresh_config.get_config_keys()))
    assert not extra, "CONFIG_FIELD_SPECS names fields Config does not have: {}".format(extra)


def test_the_default_of_every_field_satisfies_its_own_spec(fresh_config):
    """
    The strongest cheap check there is on the table: the values the
    application ships with must be values it would accept. A bound that
    rejects the default is a bound that fires on healthy configuration.
    """
    defaults = dict(fresh_config.get_config_as_dict())
    values, errors = validate_config_items(defaults)
    assert errors == {}
    # ...and validating them changes none of them.
    assert values == defaults


# ---------------------------------------------------------------------------
# Unknown keys
# ---------------------------------------------------------------------------

def test_an_unknown_key_is_an_error_rather_than_a_silent_drop():
    values, errors = validate_config_items({'not_a_setting': True})

    assert values == {}
    assert list(errors) == ['not_a_setting']
    assert 'Unknown setting' in errors['not_a_setting']


def test_a_near_miss_is_told_what_it_probably_meant():
    """The failure being fixed is a typo, so naming the intended field is the point"""
    _, errors = validate_config_items({'enable_libary_scanner': True})

    assert "enable_library_scanner" in errors['enable_libary_scanner']


def test_an_unknown_key_does_not_stop_the_other_keys_being_reported():
    values, errors = validate_config_items({
        'nonsense':    1,
        'debugging':   'not a boolean',
        'library_path': '/library',
    })

    assert sorted(errors) == ['debugging', 'nonsense']
    assert values == {'library_path': '/library'}


def test_a_key_in_the_wrong_case_is_not_quietly_accepted():
    """
    `Config.set_bulk_config_items` matches field names exactly, so 'Debugging'
    would never have been applied. Say so instead of returning success.
    """
    values, errors = validate_config_items({'Debugging': True})

    assert values == {}
    assert 'debugging' in errors['Debugging']


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('key, value', [
    ('worker_stall_timeout', 'abc'),
    ('worker_stall_timeout', 12.5),
    ('worker_stall_timeout', True),
    ('worker_stall_timeout', [60]),
    ('schedule_full_scan_minutes', None),
    ('debugging', 3),
    ('debugging', 'maybe'),
    ('debugging', None),
    ('library_path', 42),
    ('library_path', None),
    ('sanity_check_size_growth_ratio', 'big'),
    ('installation_name', ['a']),
])
def test_values_of_the_wrong_type_are_refused(key, value):
    values, errors = validate_config_items({key: value})

    assert values == {}
    assert key in errors


def test_a_boolean_is_not_a_number():
    """
    `True` is an int in Python. Accepting it for worker_stall_timeout would
    arm a one-second stall detector on healthy transcodes.
    """
    _, errors = validate_config_items({'worker_stall_timeout': True})

    assert 'Expected an integer' in errors['worker_stall_timeout']


@pytest.mark.parametrize('sent, stored', [
    ('1440', 1440),
    (1440.0, 1440),
    (1440, 1440),
])
def test_numeric_strings_are_stored_as_numbers(sent, stored):
    """
    HTML number inputs and environment variables have no way to say 1440
    other than "1440". Store the number, so the string does not have to be
    coped with everywhere it is later read.
    """
    values, errors = validate_config_items({'schedule_full_scan_minutes': sent})

    assert errors == {}
    assert values == {'schedule_full_scan_minutes': stored}
    assert isinstance(values['schedule_full_scan_minutes'], int)


@pytest.mark.parametrize('sent, stored', [
    ('true', True),
    ('False', False),
    ('on', True),
    ('0', False),
    (True, True),
])
def test_boolean_spellings_are_stored_as_booleans(sent, stored):
    values, errors = validate_config_items({'debugging': sent})

    assert errors == {}
    assert values['debugging'] is stored


def test_nullable_fields_accept_null():
    values, errors = validate_config_items({
        'ssl_certfilepath':     None,
        'release_notes_viewed': None,
        'trial_welcome_viewed': None,
    })

    assert errors == {}
    assert values == {
        'ssl_certfilepath':     None,
        'release_notes_viewed': None,
        'trial_welcome_viewed': None,
    }


def test_the_deprecated_worker_fields_are_passed_through_untouched():
    """
    number_of_workers and worker_event_schedules exist to be read once by the
    worker-group migration and then set to None. Judging their shape here
    could discard a migration; there is nothing to protect.
    """
    values, errors = validate_config_items({
        'number_of_workers':      3,
        'worker_event_schedules': [{'repetition': 'daily'}],
    })

    assert errors == {}
    assert values == {'number_of_workers': 3, 'worker_event_schedules': [{'repetition': 'daily'}]}


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('key, value', [
    ('worker_stall_timeout', 30),
    ('ui_port', 0),
    ('ui_port', 70000),
    ('concurrent_file_testers', 0),
    ('max_age_of_completed_tasks', 0),
    ('max_consecutive_task_failures', 0),
    ('log_buffer_retention', -1),
    ('sanity_check_size_growth_ratio', 0.5),
    ('sanity_check_growth_repeats', 0),
    ('schedule_full_scan_minutes', 0),
])
def test_values_outside_a_field_s_range_are_refused(key, value):
    values, errors = validate_config_items({key: value})

    assert values == {}
    assert key in errors


def test_the_stall_timeout_floor_is_the_one_the_detector_uses():
    """A range invented here, rather than taken from the mechanism, would drift"""
    _, errors = validate_config_items({'worker_stall_timeout': config.MINIMUM_WORKER_STALL_TIMEOUT - 1})
    assert errors

    _, errors = validate_config_items({'worker_stall_timeout': config.MINIMUM_WORKER_STALL_TIMEOUT})
    assert errors == {}


def test_ranges_can_be_waived_while_types_are_still_checked():
    """
    The settings.json reader waives them: an out-of-range value was accepted
    by whichever build wrote it, and `get_worker_stall_timeout()` clamps it.
    A value of the wrong type is a different matter and is still caught.
    """
    values, errors = validate_config_items({'worker_stall_timeout': 30}, enforce_ranges=False)
    assert errors == {}
    assert values == {'worker_stall_timeout': 30}

    values, errors = validate_config_items({'worker_stall_timeout': 'abc'}, enforce_ranges=False)
    assert values == {}
    assert 'worker_stall_timeout' in errors


def test_an_unrecognised_kind_would_not_pass_silently():
    """
    A typo in the table must not become a field that validates everything.
    ConfigFieldSpec has one deliberate pass-through kind, and it is spelled.
    """
    spec = ConfigFieldSpec('intger')
    with pytest.raises(AttributeError):
        spec.coerce(5)


# ---------------------------------------------------------------------------
# Reading settings.json: warn, do not refuse
# ---------------------------------------------------------------------------

def _write_settings(home_dir, **values):
    config_dir = home_dir / '.trawlarr' / 'config'
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / 'settings.json').write_text(json.dumps(values), encoding='utf-8')


class TestReadingASettingsFileThatThisVersionCannotFullyUse:

    def test_a_retired_key_does_not_stop_the_application_starting(self, home, caplog):
        # 'enable_inotify' and 'remote_installations' really were config
        # fields; #49 and #52 removed them. An install carrying them is a
        # healthy install that has been upgraded.
        _write_settings(home, enable_inotify=True, installation_name='upgraded')

        with caplog.at_level(logging.WARNING):
            settings = config.Config()

        assert settings.get_installation_name() == 'upgraded'
        assert not hasattr(settings, 'enable_inotify')
        assert any('enable_inotify' in record.getMessage() for record in caplog.records)

    def test_a_value_of_the_wrong_type_is_reported_and_the_default_kept(self, home, caplog):
        _write_settings(home, worker_stall_timeout='not a number')

        with caplog.at_level(logging.WARNING):
            settings = config.Config()

        assert settings.worker_stall_timeout == config.DEFAULT_WORKER_STALL_TIMEOUT
        assert any('worker_stall_timeout' in record.getMessage() for record in caplog.records)

    def test_a_numeric_string_in_the_file_is_loaded_as_a_number(self, home):
        _write_settings(home, schedule_full_scan_minutes='720')

        settings = config.Config()

        assert settings.schedule_full_scan_minutes == 720

    def test_a_healthy_settings_file_produces_no_warning(self, home, caplog):
        """The check that matters most: it must be silent on a good install"""
        _write_settings(
            home,
            installation_name='healthy',
            debugging=True,
            schedule_full_scan_minutes=1440,
            worker_stall_timeout=300,
            sanity_check_size_growth_ratio=1.05,
            ssl_certfilepath=None,
        )

        with caplog.at_level(logging.WARNING):
            settings = config.Config()

        assert settings.get_installation_name() == 'healthy'
        assert [record.getMessage() for record in caplog.records if 'Ignoring setting' in record.getMessage()] == []

    def test_an_out_of_range_value_written_by_an_older_build_is_still_loaded(self, home):
        """
        Ranges are not enforced on load. This value is below the stall
        detector's floor; the getter clamps it, and the file is not treated
        as broken.
        """
        _write_settings(home, worker_stall_timeout=30)

        settings = config.Config()

        assert settings.worker_stall_timeout == 30
        assert settings.get_worker_stall_timeout() == config.MINIMUM_WORKER_STALL_TIMEOUT


class TestSetConfigItemUsesTheFieldItMatched:

    def test_a_differently_cased_key_sets_the_real_field(self, fresh_config):
        """
        The key check lowercases; the assignment used not to. 'Debugging'
        passed the check and then created a second attribute of that name,
        which nothing reads and which was written to settings.json.
        """
        fresh_config.set_config_item('Installation_Name', 'cased', save_settings=False)

        assert fresh_config.get_installation_name() == 'cased'
        assert 'Installation_Name' not in fresh_config.get_config_as_dict()
