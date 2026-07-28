#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_config_runtime_paths.py

    settings.json must not be able to move the application back into
    ~/.unmanic/.

    `Config` used to dump its entire __dict__ to settings.json, absolute
    `config_path`, `log_path`, `plugins_path` and `userdata_path` included,
    and read them straight back in through `set_bulk_config_items`. That
    turned a derived runtime path into a persisted setting, and it silently
    undid the .unmanic -> .trawlarr move:

      1. Operator migrates correctly. ~/.trawlarr holds everything.
      2. Trawlarr starts, reads ~/.trawlarr/config/settings.json, and finds
         config_path = ~/.unmanic/config written there by the last build.
      3. It recreates ~/.unmanic and runs entirely out of it. New empty
         database, new logs, new plugins directory. The migrated data is
         never touched.
      4. Next restart, both directories hold data, so the legacy-config
         guard is satisfied and never speaks again.

    Every step succeeds. Nothing is logged. The operator finds an empty
    install and no explanation.

    These tests drive a temporary HOME throughout. The real ~/.unmanic is
    never read or written.
"""
import json
import os

import pytest

from trawlarr import config


def _fresh_config(**kwargs):
    """
    Build a Config that actually re-reads its environment.

    Config is a singleton and other tests leave one cached against the real
    home directory. Reach through the class's own metaclass rather than an
    imported `SingletonType`: the compatibility shim can leave more than one
    copy of the metaclass alive, and clearing the wrong registry hands back
    the stale instance.
    """
    type(config.Config)._instances.pop(config.Config, None)
    return config.Config(**kwargs)


def _drop_cached_config():
    type(config.Config)._instances.pop(config.Config, None)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME_DIR', str(tmp_path))
    for key in config.DERIVED_PATH_CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield tmp_path
    _drop_cached_config()


def _write_settings(home_dir, **values):
    config_dir = home_dir / '.trawlarr' / 'config'
    config_dir.mkdir(parents=True, exist_ok=True)
    settings_file = config_dir / 'settings.json'
    settings_file.write_text(json.dumps(values), encoding='utf-8')
    return settings_file


def _legacy_paths(home_dir):
    legacy = home_dir / '.unmanic'
    return {
        'config_path':   str(legacy / 'config'),
        'log_path':      str(legacy / 'logs'),
        'plugins_path':  str(legacy / 'plugins'),
        'userdata_path': str(legacy / 'userdata'),
    }


class TestSettingsJsonCannotResurrectTheLegacyDirectory:
    """
    The scenario in full: a correctly migrated install whose settings.json
    still names the old absolute paths.
    """

    def test_the_legacy_paths_in_the_file_are_ignored(self, home):
        _write_settings(home, **_legacy_paths(home))

        settings = _fresh_config()

        assert settings.get_config_path() == str(home / '.trawlarr' / 'config')
        assert settings.get_log_path() == str(home / '.trawlarr' / 'logs')
        assert settings.get_plugins_path() == str(home / '.trawlarr' / 'plugins')
        assert settings.get_userdata_path() == str(home / '.trawlarr' / 'userdata')

    def test_the_legacy_directory_is_not_recreated(self, home):
        _write_settings(home, **_legacy_paths(home))

        _fresh_config()

        # Nothing under the old name, at all. Not the directory, not a
        # stray logs/ or plugins/ underneath it.
        assert not (home / '.unmanic').exists()

    def test_the_migrated_data_is_the_data_that_is_used(self, home):
        # A migrated install: the real database lives under .trawlarr.
        paths = _legacy_paths(home)
        paths['installation_name'] = 'migrated-install'
        _write_settings(home, **paths)
        database = home / '.trawlarr' / 'config' / 'trawlarr.db'
        database.write_bytes(b'SQLite format 3\x00migrated')

        settings = _fresh_config()

        assert os.path.join(settings.get_config_path(), 'trawlarr.db') == str(database)
        assert database.read_bytes() == b'SQLite format 3\x00migrated'
        # The rest of the file is still honoured - the fix drops four keys,
        # not the settings file.
        assert settings.get_installation_name() == 'migrated-install'

    def test_the_legacy_config_guard_stays_armed(self, home):
        # The compounding failure: once the application recreates .unmanic,
        # both directories hold data and the guard can never fire again.
        from trawlarr.libs import runtimepaths

        _write_settings(home, **_legacy_paths(home))

        _fresh_config()

        assert not runtimepaths._directory_holds_data(str(home / '.unmanic'))


class TestTheDerivedPathsAreNotPersisted:

    def test_they_are_absent_from_a_newly_written_settings_file(self, home):
        settings = _fresh_config()

        settings.set_config_item('installation_name', 'writes-the-file')

        written = json.loads(
            (home / '.trawlarr' / 'config' / 'settings.json').read_text(encoding='utf-8'))
        assert written['installation_name'] == 'writes-the-file'
        for key in sorted(config.DERIVED_PATH_CONFIG_KEYS):
            assert key not in written

    def test_writing_the_file_strips_paths_an_older_build_left_behind(self, home):
        _write_settings(home, installation_name='old', **_legacy_paths(home))

        settings = _fresh_config()
        settings.set_config_item('installation_name', 'new')

        written = json.loads(
            (home / '.trawlarr' / 'config' / 'settings.json').read_text(encoding='utf-8'))
        assert written['installation_name'] == 'new'
        assert not config.DERIVED_PATH_CONFIG_KEYS.intersection(written)

    def test_a_restart_reproduces_the_same_paths(self, home):
        first = _fresh_config()
        first.set_config_item('installation_name', 'restart-me')
        expected = (first.get_config_path(), first.get_log_path(),
                    first.get_plugins_path(), first.get_userdata_path())

        second = _fresh_config()

        assert (second.get_config_path(), second.get_log_path(),
                second.get_plugins_path(), second.get_userdata_path()) == expected


class TestOverridingThePathsStillWorks:
    """
    Not persisting them must not mean not being able to set them. The launch
    surface - environment and command line - is re-applied on every start, so
    it is the right place for an override to live.
    """

    def test_the_environment_still_overrides_the_config_path(self, home, monkeypatch):
        override = home / 'elsewhere' / 'config'
        monkeypatch.setenv('config_path', str(override))

        settings = _fresh_config()

        assert settings.get_config_path() == str(override)
        assert override.is_dir()

    def test_an_environment_override_survives_a_settings_file(self, home, monkeypatch):
        # The env is read before settings.json. Now that the file no longer
        # carries these keys, nothing downstream can shadow the override.
        override = home / 'elsewhere' / 'config'
        override.mkdir(parents=True)
        (override / 'settings.json').write_text(
            json.dumps(_legacy_paths(home)), encoding='utf-8')
        monkeypatch.setenv('config_path', str(override))

        settings = _fresh_config()

        assert settings.get_config_path() == str(override)
        assert not (home / '.unmanic').exists()

    def test_an_environment_override_is_not_written_into_settings_json(self, home, monkeypatch):
        override = home / 'elsewhere' / 'config'
        monkeypatch.setenv('config_path', str(override))

        settings = _fresh_config()
        settings.set_config_item('installation_name', 'env-override')

        written = json.loads((override / 'settings.json').read_text(encoding='utf-8'))
        assert 'config_path' not in written

    def test_the_command_line_still_overrides_the_paths(self, home):
        target = home / 'cli'

        settings = _fresh_config(unmanic_path=str(target))

        assert settings.get_config_path() == str(target / 'config')
        assert settings.get_plugins_path() == str(target / 'plugins')
        assert settings.get_userdata_path() == str(target / 'userdata')


class TestTheKeySetIsShared:

    def test_the_api_protected_keys_are_the_derived_paths(self):
        # The API refuses to write exactly the keys that are not persisted.
        # Two lists that must agree, kept as one.
        assert config.API_PROTECTED_CONFIG_KEYS == config.DERIVED_PATH_CONFIG_KEYS

    def test_it_names_all_four_runtime_paths(self):
        assert config.DERIVED_PATH_CONFIG_KEYS == frozenset({
            'config_path', 'log_path', 'plugins_path', 'userdata_path',
        })
