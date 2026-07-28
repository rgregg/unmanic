#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_env_vars.py

    The environment-variable rename (issue #49 step 4).

    Two properties are under test, and they are opposites of each other:

    1. `TRAWLARR_*` names take effect.
    2. `UNMANIC_*` names do NOT take effect -- but never silently. Every one
       of them produces a startup warning that names its replacement.

    Property 2 is the one worth having tests for. A fallback that quietly
    stops working is indistinguishable from a fallback that never existed,
    and the only evidence an operator gets is the warning.
"""
import importlib
import importlib.util
import os
from unittest import mock

import pytest

from trawlarr import metadata, service
from trawlarr.libs import envvars
from trawlarr.libs.plugins import PluginsHandler


def _fresh_session_module():
    """Load a second, private copy of trawlarr.libs.session.

    `LOCAL_SESSION_LEVEL` is bound once, at import time, so testing it
    requires a fresh execution of the module under a modified environment.
    `importlib.reload` would do that, but it rebinds the names in the live
    module -- every other test module that did `from trawlarr.libs.session
    import Session` would then be holding a class that is no longer the one
    the module exposes. This copy is never registered in `sys.modules`, so
    nothing else can see it.
    """
    live = importlib.import_module('trawlarr.libs.session')
    spec = importlib.util.spec_from_file_location('_test_session_copy', live.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestDistributionName:
    """The distribution and console script names, which packaging derives
    from `metadata.__name` via `versioninfo.name()`."""

    def test_metadata_name_is_trawlarr(self):
        # `__name` is a module-level global, so it is not name-mangled;
        # getattr avoids the mangling that `metadata.__name` would suffer
        # inside this class body. This is how versioninfo.py reads it.
        assert getattr(metadata, '__name') == 'trawlarr'

    def test_versioninfo_reports_trawlarr(self):
        versioninfo = importlib.import_module('versioninfo')
        assert versioninfo.name() == 'trawlarr'


class TestNewEnvVarsTakeEffect:

    def test_plugin_repo_url_override_applies(self, monkeypatch):
        monkeypatch.setenv(envvars.DEFAULT_PLUGIN_REPO_URL_ENV_VAR,
                           'https://mirror.example.com/repo.json')
        assert PluginsHandler._resolve_direct_repo_url('default') == \
            'https://mirror.example.com/repo.json'

    def test_remote_logging_endpoint_override_applies(self, monkeypatch):
        from trawlarr.libs.session import Session
        monkeypatch.setenv(envvars.REMOTE_LOGGING_ENDPOINT_ENV_VAR,
                           'http://sink.example.com/loki/api/v1/push')
        s = Session.__new__(Session)
        s.logger = mock.Mock()
        s.uuid = 'test-uuid'
        with mock.patch('trawlarr.libs.session.config.Config') as cfg, \
                mock.patch('trawlarr.libs.session.TrawlarrLogging') as logs:
            cfg.return_value.get_log_buffer_retention.return_value = 10
            s._Session__configure_log_forwarding(session_valid=True)
        logs.enable_remote_logging.assert_called_once_with(
            'http://sink.example.com/loki/api/v1/push', 'test-uuid', 10)

    def test_local_session_level_read_from_new_name(self, monkeypatch):
        monkeypatch.setenv(envvars.LOCAL_SESSION_LEVEL_ENV_VAR, '3')
        assert _fresh_session_module().LOCAL_SESSION_LEVEL == 3


class TestLegacyEnvVarsAreIgnored:
    """No fallback. Setting the old name changes nothing."""

    def test_legacy_plugin_repo_url_is_not_read(self, monkeypatch):
        monkeypatch.delenv(envvars.DEFAULT_PLUGIN_REPO_URL_ENV_VAR, raising=False)
        monkeypatch.setenv('UNMANIC_DEFAULT_PLUGIN_REPO_URL',
                           'https://mirror.example.com/repo.json')
        assert PluginsHandler._resolve_direct_repo_url('default') == (
            'https://raw.githubusercontent.com/Unmanic/unmanic-plugins/repo/repo.json')

    def test_legacy_remote_logging_endpoint_is_not_read(self, monkeypatch):
        from trawlarr.libs.session import Session
        monkeypatch.delenv(envvars.REMOTE_LOGGING_ENDPOINT_ENV_VAR, raising=False)
        monkeypatch.setenv('UNMANIC_REMOTE_LOGGING_ENDPOINT',
                           'http://sink.example.com/loki/api/v1/push')
        s = Session.__new__(Session)
        s.logger = mock.Mock()
        s.uuid = 'test-uuid'
        with mock.patch('trawlarr.libs.session.config.Config') as cfg, \
                mock.patch('trawlarr.libs.session.TrawlarrLogging') as logs:
            cfg.return_value.get_log_buffer_retention.return_value = 10
            s._Session__configure_log_forwarding(session_valid=True)
        logs.enable_remote_logging.assert_not_called()
        logs.disable_remote_logging.assert_called_once_with(10)

    def test_legacy_local_session_level_is_not_read(self, monkeypatch):
        monkeypatch.delenv(envvars.LOCAL_SESSION_LEVEL_ENV_VAR, raising=False)
        monkeypatch.setenv('UNMANIC_LOCAL_SESSION_LEVEL', '3')
        assert _fresh_session_module().LOCAL_SESSION_LEVEL == 7


class TestLegacyEnvVarDetection:

    def test_clean_environment_produces_no_message(self):
        assert envvars.check_for_legacy_env_vars({'PATH': '/usr/bin'}) is None

    def test_trawlarr_names_are_not_flagged(self):
        env = {envvars.DEFAULT_PLUGIN_REPO_URL_ENV_VAR: 'https://x/repo.json'}
        assert envvars.check_for_legacy_env_vars(env) is None

    @pytest.mark.parametrize('legacy,replacement', sorted(envvars.RENAMED_ENV_VARS.items()))
    def test_every_documented_rename_is_reported(self, legacy, replacement):
        message = envvars.check_for_legacy_env_vars({legacy: 'value'})
        assert message is not None
        assert legacy in message
        assert replacement in message

    def test_unknown_legacy_prefixed_var_still_reported(self):
        """The scan is by prefix, so a variable this module has never heard
        of is reported with the mechanical name swap rather than ignored."""
        message = envvars.check_for_legacy_env_vars({'UNMANIC_SOMETHING_NEW': '1'})
        assert message is not None
        assert 'UNMANIC_SOMETHING_NEW  ->  TRAWLARR_SOMETHING_NEW' in message

    def test_profile_unmanic_has_no_prefix_and_is_still_reported(self):
        """PROFILE_UNMANIC does not start with UNMANIC_, so it is only ever
        found because RENAMED_ENV_VARS names it explicitly."""
        assert not 'PROFILE_UNMANIC'.startswith(envvars.LEGACY_ENV_VAR_PREFIX)
        message = envvars.check_for_legacy_env_vars({'PROFILE_UNMANIC': 'true'})
        assert 'PROFILE_TRAWLARR' in message

    def test_message_says_the_variable_has_no_effect(self):
        message = envvars.check_for_legacy_env_vars({'UNMANIC_DB_PATH': '/x'})
        assert 'NO effect' in message

    def test_all_legacy_names_reported_together(self):
        env = {legacy: 'v' for legacy in envvars.RENAMED_ENV_VARS}
        message = envvars.check_for_legacy_env_vars(env)
        for legacy, replacement in envvars.RENAMED_ENV_VARS.items():
            assert legacy in message
            assert replacement in message

    def test_replacement_name_for_unrelated_variable_is_none(self):
        assert envvars.replacement_env_var_name('PATH') is None


class TestStartupWarning:

    def test_warning_is_printed_to_stderr(self, monkeypatch, capsys):
        monkeypatch.setenv('UNMANIC_DEFAULT_PLUGIN_REPO_URL', 'https://x/repo.json')
        service.warn_about_legacy_env_vars()
        captured = capsys.readouterr()
        assert 'UNMANIC_DEFAULT_PLUGIN_REPO_URL' in captured.err
        assert 'TRAWLARR_DEFAULT_PLUGIN_REPO_URL' in captured.err
        assert captured.out == ''

    def test_warning_does_not_stop_startup(self, monkeypatch):
        """A stray environment variable is a warning, not a refusal. The
        config-directory guard raises SystemExit; this deliberately does
        not."""
        monkeypatch.setenv('UNMANIC_DEFAULT_PLUGIN_REPO_URL', 'https://x/repo.json')
        assert service.warn_about_legacy_env_vars() is None

    def test_nothing_printed_when_environment_is_clean(self, monkeypatch, capsys):
        for legacy in envvars.RENAMED_ENV_VARS:
            monkeypatch.delenv(legacy, raising=False)
        for key in list(os.environ):
            if key.startswith(envvars.LEGACY_ENV_VAR_PREFIX):
                monkeypatch.delenv(key, raising=False)
        service.warn_about_legacy_env_vars()
        captured = capsys.readouterr()
        assert captured.err == ''
