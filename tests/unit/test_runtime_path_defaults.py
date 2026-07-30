#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# Runtime path defaults must track runtimepaths, not a string literal.
#
# This shipped as a real bug in three places. PluginExecutor and the
# plugin CLI each defaulted their plugins directory to
# ~/.unmanic/plugins while the application installs plugins to
# ~/.trawlarr/plugins, and the CLI did the same for its dev cache and dev
# library directories. Ten call sites construct PluginExecutor() with no
# argument, so all of them looked in a directory the application never
# writes to. Nothing failed loudly -- the executor simply found no
# plugins.
#
# It survived the rename audits because the literals sit inside default
# arguments rather than in the config module, and it survived the test
# suite because tests either pass an explicit directory or run with no
# plugins installed, where both paths are equally empty.

import inspect
import os

import pytest

from trawlarr import config as config_module
from trawlarr.config import Config
from trawlarr.libs import runtimepaths
from trawlarr.libs.unplugins.executor import PluginExecutor


@pytest.mark.unittest
class TestPluginExecutorDefaultDirectory:

    def test_the_default_matches_where_the_application_installs_plugins(self, tmp_path, monkeypatch):
        """The bug this file exists for: these two disagreed.

        Config is a singleton, so an earlier test may already have built one
        against a different HOME. Drop it from the registry so this measures
        the patched environment, and drop it again afterwards so the instance
        built here does not leak into later tests.
        """
        monkeypatch.setenv('HOME', str(tmp_path))
        registry = type(config_module.Config)._instances
        registry.pop(config_module.Config, None)
        try:
            assert PluginExecutor().plugins_directory == Config().get_plugins_path()
        finally:
            registry.pop(config_module.Config, None)

    def test_the_default_is_under_the_current_app_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv('HOME', str(tmp_path))
        directory = PluginExecutor().plugins_directory
        assert runtimepaths.APP_DIR_NAME in directory.split(os.sep)
        assert runtimepaths.LEGACY_APP_DIR_NAME not in directory.split(os.sep)

    def test_an_explicit_directory_is_still_honoured(self, tmp_path):
        explicit = str(tmp_path / 'somewhere-else')
        assert PluginExecutor(plugins_directory=explicit).plugins_directory == explicit


@pytest.mark.unittest
class TestNoModuleHardcodesTheLegacyAppDirectory:
    """The generalisation. The per-directory marker file is a separate
    concern and deliberately keeps a legacy name for a different reason --
    see tests/unit/test_directory_marker_coexistence.py -- so it is not
    covered here."""

    @pytest.mark.parametrize('module_name', [
        'trawlarr.libs.unplugins.executor',
        'trawlarr.libs.unplugins.pluginscli',
        'trawlarr.config',
    ])
    def test_module_builds_paths_from_runtimepaths(self, module_name):
        import importlib
        module = importlib.import_module(module_name)
        source = inspect.getsource(module)
        offenders = [
            line.strip() for line in source.splitlines()
            if "'{}'".format(runtimepaths.LEGACY_APP_DIR_NAME) in line
            and not line.strip().startswith('#')
        ]
        assert offenders == [], (
            "{} hardcodes the legacy app directory in code: {}. Use "
            "runtimepaths.APP_DIR_NAME so the next rename cannot "
            "desynchronise them.".format(module_name, offenders)
        )
