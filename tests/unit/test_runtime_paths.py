#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_runtime_paths.py

    Trawlarr keeps its configuration in ~/.trawlarr/ where Unmanic kept
    ~/.unmanic/, and serves itself under /trawlarr/ where Unmanic served
    /unmanic/. Both are clean breaks: no migration, no fallback, no alias.

    A clean break on a URL announces itself with a 404. A clean break on a
    config directory announces nothing at all — the application would just
    create an empty one and come up looking like a fresh install, with every
    library, plugin, setting and completed task apparently gone. Nothing
    would actually have been deleted, but the only way to establish that is
    to go looking, at whatever hour the container restarted.

    So the directory rename carries a guard, and these tests hold it to all
    three states an upgrade can be in:

      1. legacy directory populated, new one absent   -> refuse to start
      2. neither present (a genuinely fresh install)  -> start normally
      3. both present                                 -> start normally,
                                                         against the new one

    Every test drives a temporary HOME. The real ~/.unmanic is never read.
"""
import os

import pytest

from trawlarr.libs import runtimepaths


def _fresh_config():
    """
    Build a Config that actually re-reads the environment.

    Config is a singleton, and by the time this module runs some other test
    has usually built one against the real home directory. Reach through the
    class's own metaclass rather than an imported `SingletonType` symbol: the
    compatibility shim can leave more than one copy of the metaclass alive,
    and clearing the wrong registry silently hands back the cached instance.
    """
    from trawlarr import config

    registry = type(config.Config)._instances
    registry.pop(config.Config, None)
    return config.Config()


def _drop_cached_config():
    from trawlarr import config

    type(config.Config)._instances.pop(config.Config, None)


def _populate(directory, *relpaths):
    """Create a directory tree with a file in it, standing in for an install"""
    os.makedirs(directory, exist_ok=True)
    for relpath in relpaths:
        target = os.path.join(directory, relpath)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w', encoding='utf-8') as handle:
            handle.write('{}')


@pytest.fixture(autouse=True)
def _no_ignore_override(monkeypatch):
    """The guard has an env escape hatch; keep it out of the way by default"""
    monkeypatch.delenv(runtimepaths.IGNORE_LEGACY_CONFIG_ENV_VAR, raising=False)


class TestTheGuardCatchesAnUnmigratedInstall:
    """
    State 1: the operator pulled the new image without moving their data.
    """

    def test_it_refuses_to_start(self, tmp_path):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')

        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is not None

    def test_the_message_names_both_directories_in_full(self, tmp_path):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')

        message = runtimepaths.check_for_legacy_config_directory(str(tmp_path))

        # An operator reading this at 2am needs the two absolute paths, not
        # the concept of a rename.
        assert str(tmp_path / '.unmanic') in message
        assert str(tmp_path / '.trawlarr') in message

    def test_the_message_names_both_database_filenames(self, tmp_path):
        _populate(str(tmp_path / '.unmanic'), 'config/unmanic.db')

        message = runtimepaths.check_for_legacy_config_directory(str(tmp_path))

        assert 'unmanic.db' in message
        assert 'trawlarr.db' in message

    def test_the_message_says_nothing_was_deleted(self, tmp_path):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')

        message = runtimepaths.check_for_legacy_config_directory(str(tmp_path))

        assert 'Nothing has been moved or deleted' in message

    def test_an_empty_new_directory_does_not_defeat_the_guard(self, tmp_path):
        # The Docker entrypoint runs `mkdir -p /config/.trawlarr` before the
        # application starts. If the guard tested existence rather than
        # contents, that mkdir alone would wave an unmigrated install
        # straight through — the exact failure this exists to prevent.
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')
        os.makedirs(str(tmp_path / '.trawlarr'))

        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is not None

    def test_the_guard_never_touches_the_legacy_directory(self, tmp_path):
        legacy = tmp_path / '.unmanic'
        _populate(str(legacy), 'config/settings.json', 'plugins/marker')

        runtimepaths.check_for_legacy_config_directory(str(tmp_path))

        assert (legacy / 'config' / 'settings.json').is_file()
        assert (legacy / 'plugins' / 'marker').is_file()
        assert not (tmp_path / '.trawlarr').exists()


class TestAFreshInstallStartsNormally:
    """
    State 2: no Unmanic, no Trawlarr. Nothing to warn anyone about.
    """

    def test_it_does_not_refuse_to_start(self, tmp_path):
        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is None

    def test_the_config_object_creates_the_new_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv('HOME_DIR', str(tmp_path))

        try:
            settings = _fresh_config()

            assert settings.get_config_path() == str(tmp_path / '.trawlarr' / 'config')
            assert (tmp_path / '.trawlarr' / 'config').is_dir()
            assert not (tmp_path / '.unmanic').exists()
        finally:
            _drop_cached_config()

    def test_an_empty_legacy_directory_is_not_an_install(self, tmp_path):
        # A leftover empty directory is not data. Refusing on it would be a
        # false alarm the operator cannot clear without deleting something.
        os.makedirs(str(tmp_path / '.unmanic'))

        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is None


class TestBothPresentStartsAgainstTheNewDirectory:
    """
    State 3: the operator moved (or copied) their data. Get out of the way.
    """

    def test_it_does_not_refuse_to_start(self, tmp_path):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')
        _populate(str(tmp_path / '.trawlarr'), 'config/settings.json')

        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is None

    def test_the_new_directory_is_the_one_used(self, tmp_path, monkeypatch):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')
        _populate(str(tmp_path / '.trawlarr'), 'config/settings.json')
        monkeypatch.setenv('HOME_DIR', str(tmp_path))

        try:
            settings = _fresh_config()

            assert settings.get_config_path() == str(tmp_path / '.trawlarr' / 'config')
            assert '.trawlarr' in settings.get_config_path()
            assert '.unmanic' not in settings.get_config_path()
            assert '.unmanic' not in settings.get_plugins_path()
            assert '.unmanic' not in settings.get_userdata_path()
        finally:
            _drop_cached_config()


class TestTheEscapeHatch:
    """
    An operator who knows the legacy directory is stale must be able to say
    so without deleting it first.
    """

    @pytest.mark.parametrize('value', ['1', 'true', 'TRUE', 'yes', 'on'])
    def test_the_env_var_suppresses_the_guard(self, tmp_path, monkeypatch, value):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')
        monkeypatch.setenv(runtimepaths.IGNORE_LEGACY_CONFIG_ENV_VAR, value)

        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is None

    @pytest.mark.parametrize('value', ['', '0', 'false', 'no'])
    def test_a_falsey_value_leaves_the_guard_armed(self, tmp_path, monkeypatch, value):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')
        monkeypatch.setenv(runtimepaths.IGNORE_LEGACY_CONFIG_ENV_VAR, value)

        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is not None

    def test_the_message_advertises_the_escape_hatch(self, tmp_path):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')

        message = runtimepaths.check_for_legacy_config_directory(str(tmp_path))

        assert runtimepaths.IGNORE_LEGACY_CONFIG_ENV_VAR in message


class TestTheServiceEntrypointHonoursTheGuard:
    """
    The guard is only worth having if `main()` actually stops for it.
    """

    def test_it_exits_non_zero_and_prints_the_message(self, tmp_path, monkeypatch, capsys):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')
        monkeypatch.setenv('HOME_DIR', str(tmp_path))

        from trawlarr import service

        with pytest.raises(SystemExit) as exit_info:
            service.guard_against_legacy_config_directory()

        assert exit_info.value.code == 1

        # stderr, not stdout: this is a failure, and operators pipe logs.
        stderr = capsys.readouterr().err
        assert str(tmp_path / '.unmanic') in stderr
        assert str(tmp_path / '.trawlarr') in stderr

    def test_it_returns_quietly_on_a_fresh_install(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv('HOME_DIR', str(tmp_path))

        from trawlarr import service

        assert service.guard_against_legacy_config_directory() is None
        assert capsys.readouterr().err == ''


class TestTheDatabaseFilenameMoved:

    def test_init_db_names_the_trawlarr_database(self, tmp_path, monkeypatch):
        from trawlarr import service

        recorded = {}

        class _StubMigrations:
            def __init__(self, settings):
                recorded['settings'] = settings

            def update_schema(self):
                pass

        class _StubDatabase:
            @staticmethod
            def select_database(settings):
                return 'db-connection'

        import trawlarr.libs.unmodels.lib as unmodels_lib

        monkeypatch.setattr(service, 'Migrations', _StubMigrations)
        monkeypatch.setattr(unmodels_lib, 'Database', _StubDatabase)

        config_path = str(tmp_path / 'config')
        assert service.init_db(config_path) == 'db-connection'

        assert recorded['settings']['FILE'] == os.path.join(config_path, 'trawlarr.db')


class TestTheApiPrefixMoved:

    def test_the_url_constants_carry_the_new_name(self):
        assert runtimepaths.URL_PREFIX == '/trawlarr'
        assert runtimepaths.API_URL_PREFIX == '/trawlarr/api'

    def test_no_route_is_still_mounted_under_the_old_prefix(self):
        # A clean break means the old prefix is not served at all, by
        # anything - not the API, not the UI, not the static assets.
        from trawlarr.libs import uiserver

        source = open(uiserver.__file__, encoding='utf-8').read()

        assert '"/unmanic/' not in source
        assert "'/unmanic/" not in source

    def test_the_swagger_contract_advertises_the_new_server_url(self):
        from trawlarr.webserver.api_v2.schema.swagger import build_swagger_spec

        spec, _errors = build_swagger_spec()

        assert spec.to_dict()['servers'] == [
            {'url': '/trawlarr/api/v2/', 'description': 'Current environment'},
        ]
