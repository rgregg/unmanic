#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# Tests for the `trawlarr` -> `unmanic` namespace alias (issue #49, step 1).
#
# The property under test throughout is object *identity*, not merely that
# the import succeeds. Two module objects for the same source file would
# mean two copies of every module-level singleton in it -- loggers, caches,
# the settings object -- which fails silently and at a distance.

import os
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run_in_fresh_interpreter(source):
    """Run a snippet in a clean interpreter rooted at the project."""
    return subprocess.run(
        [sys.executable, '-c', source],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


@pytest.mark.unittest
class TestNamespaceAliasShim(object):

    def test_top_level_alias_is_the_same_module_object(self):
        import trawlarr
        import unmanic

        assert trawlarr is unmanic

    def test_plain_import_of_submodule_returns_canonical_module(self):
        import trawlarr.libs.filetest
        import unmanic.libs.filetest

        assert trawlarr.libs.filetest is unmanic.libs.filetest
        assert trawlarr.libs is unmanic.libs

    def test_from_import_of_module_returns_canonical_module(self):
        from trawlarr.libs import filetest as aliased
        from unmanic.libs import filetest as canonical

        assert aliased is canonical

    def test_from_import_of_attribute_returns_canonical_object(self):
        from trawlarr.libs.filetest import FileTest as AliasedFileTest
        from unmanic.libs.filetest import FileTest as CanonicalFileTest

        assert AliasedFileTest is CanonicalFileTest

    def test_plugin_style_import_path_resolves_through_the_shim(self):
        """
        The acceptance criterion from issue #49: a community plugin's own
        import line, written against `unmanic`, must reach the same object
        when written against `trawlarr`.
        """
        from trawlarr.libs.unplugins.plugin_types.plugin_type_base import PluginType as AliasedPluginType
        from unmanic.libs.unplugins.plugin_types.plugin_type_base import PluginType as CanonicalPluginType

        assert AliasedPluginType is CanonicalPluginType

    @pytest.mark.parametrize('dotted_path', [
        'trawlarr.libs',
        'trawlarr.libs.filetest',
        'trawlarr.libs.directoryinfo',
        'trawlarr.libs.unplugins',
        'trawlarr.libs.unplugins.plugin_types',
        'trawlarr.libs.unplugins.plugin_types.plugin_type_base',
        'trawlarr.webserver.api_v2.schema.swagger',
    ])
    def test_deep_submodules_resolve_by_importlib_to_the_canonical_module(self, dotted_path):
        import importlib

        canonical_path = dotted_path.replace('trawlarr', 'unmanic', 1)
        assert importlib.import_module(dotted_path) is importlib.import_module(canonical_path)

    def test_lazily_imported_submodule_is_not_duplicated(self):
        """
        A submodule imported for the first time *through the alias* must
        still be the one and only module object, and must be reachable
        afterwards under the canonical name.
        """
        import importlib
        import unmanic

        # A module unlikely to have been imported by anything else here.
        dotted_path = 'unmanic.libs.singleton'
        for name in (dotted_path, dotted_path.replace('unmanic', 'trawlarr', 1)):
            sys.modules.pop(name, None)

        aliased = importlib.import_module('trawlarr.libs.singleton')
        canonical = importlib.import_module(dotted_path)

        assert aliased is canonical
        assert sys.modules['trawlarr.libs.singleton'] is sys.modules[dotted_path]
        assert unmanic.libs.singleton is aliased

    def test_aliased_module_keeps_its_canonical_identity_attributes(self):
        """
        The import machinery rewrites dunders on a module handed back by a
        loader. The shim restores them, so an aliased module still reports
        itself under the real name -- tracebacks, logging and pickling all
        read `__name__`.
        """
        import trawlarr.libs.filetest

        assert trawlarr.libs.filetest.__name__ == 'unmanic.libs.filetest'
        assert trawlarr.libs.filetest.__spec__.name == 'unmanic.libs.filetest'
        assert trawlarr.libs.__name__ == 'unmanic.libs'

    def test_module_level_state_is_shared_not_copied(self):
        import trawlarr.libs.filetest
        import unmanic.libs.filetest

        sentinel = object()
        unmanic.libs.filetest._alias_shim_sentinel = sentinel
        try:
            assert trawlarr.libs.filetest._alias_shim_sentinel is sentinel
        finally:
            del unmanic.libs.filetest._alias_shim_sentinel

    def test_alias_is_live_when_only_the_real_package_was_imported(self):
        """
        Nothing has to import a magic module first: importing `unmanic`
        installs the alias.
        """
        result = _run_in_fresh_interpreter(
            'import unmanic\n'
            'import trawlarr.libs.filetest\n'
            'import unmanic.libs.filetest\n'
            'assert trawlarr.libs.filetest is unmanic.libs.filetest\n'
            'print("ok")\n'
        )

        assert result.returncode == 0, result.stderr
        assert 'ok' in result.stdout

    def test_alias_is_live_when_it_is_imported_first(self):
        """
        A process that reaches for `trawlarr` before `unmanic` has been
        imported at all still gets the real modules.
        """
        result = _run_in_fresh_interpreter(
            'import trawlarr\n'
            'from trawlarr.libs.unplugins.plugin_types.plugin_type_base import PluginType\n'
            'import unmanic\n'
            'from unmanic.libs.unplugins.plugin_types.plugin_type_base import PluginType as Canonical\n'
            'assert trawlarr is unmanic\n'
            'assert PluginType is Canonical\n'
            'print("ok")\n'
        )

        assert result.returncode == 0, result.stderr
        assert 'ok' in result.stdout

    def test_plugins_importing_the_real_package_are_unaffected(self):
        """
        Plugins are executed with importlib inside the running process and
        import `unmanic.*` directly. The shim must not disturb that path.
        """
        result = _run_in_fresh_interpreter(
            'import importlib.util, sys\n'
            'src = "from unmanic.libs.unplugins.plugin_types.plugin_type_base import PluginType\\n"\n'
            'spec = importlib.util.spec_from_loader("fake_plugin.plugin", loader=None)\n'
            'module = importlib.util.module_from_spec(spec)\n'
            'exec(compile(src, "plugin.py", "exec"), module.__dict__)\n'
            'from unmanic.libs.unplugins.plugin_types.plugin_type_base import PluginType\n'
            'assert module.PluginType is PluginType\n'
            'print("ok")\n'
        )

        assert result.returncode == 0, result.stderr
        assert 'ok' in result.stdout

    def test_unrelated_module_names_are_not_claimed_by_the_finder(self):
        """
        The finder must decline everything outside the aliased namespace,
        including names that merely start with the same characters.
        """
        from unmanic.namespace_shim import AliasFinder

        finder = AliasFinder()
        for name in ('unmanic', 'unmanic.libs', 'json', 'trawlarrbogus', 'trawlarrbogus.thing'):
            assert finder.find_spec(name) is None

    def test_installing_the_alias_twice_adds_only_one_finder(self):
        from unmanic.namespace_shim import AliasFinder, install

        install()
        install()

        finders = [finder for finder in sys.meta_path if isinstance(finder, AliasFinder)]
        assert len(finders) == 1
