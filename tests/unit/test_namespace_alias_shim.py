#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# Tests for the legacy `unmanic` -> `trawlarr` namespace alias (issue #49).
#
# Step 2 inverted the direction: `trawlarr` is now the real package and
# `unmanic` is the compatibility alias. That is no longer a preparatory
# nicety -- it is the path every existing community plugin's imports take,
# so these tests guard a live contract rather than a future one.
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

        assert unmanic is trawlarr

    def test_plain_import_of_submodule_returns_canonical_module(self):
        import trawlarr.libs.filetest
        import unmanic.libs.filetest

        assert unmanic.libs.filetest is trawlarr.libs.filetest
        assert unmanic.libs is trawlarr.libs

    def test_from_import_of_module_returns_canonical_module(self):
        from trawlarr.libs import filetest as canonical
        from unmanic.libs import filetest as aliased

        assert aliased is canonical

    def test_from_import_of_attribute_returns_canonical_object(self):
        from trawlarr.libs.filetest import FileTest as CanonicalFileTest
        from unmanic.libs.filetest import FileTest as AliasedFileTest

        assert AliasedFileTest is CanonicalFileTest

    def test_plugin_style_import_path_resolves_through_the_shim(self):
        """
        The acceptance criterion from issue #49: a community plugin's own
        import line, written against `unmanic`, must reach the same object
        as the canonical `trawlarr` path.
        """
        from trawlarr.libs.unplugins.plugin_types.plugin_type_base import PluginType as CanonicalPluginType
        from unmanic.libs.unplugins.plugin_types.plugin_type_base import PluginType as AliasedPluginType

        assert AliasedPluginType is CanonicalPluginType

    @pytest.mark.parametrize('dotted_path', [
        'unmanic.libs',
        'unmanic.libs.filetest',
        'unmanic.libs.directoryinfo',
        'unmanic.libs.unplugins',
        'unmanic.libs.unplugins.plugin_types',
        'unmanic.libs.unplugins.plugin_types.plugin_type_base',
        'unmanic.webserver.api_v2.schema.swagger',
    ])
    def test_deep_submodules_resolve_by_importlib_to_the_canonical_module(self, dotted_path):
        import importlib

        canonical_path = dotted_path.replace('unmanic', 'trawlarr', 1)
        assert importlib.import_module(dotted_path) is importlib.import_module(canonical_path)

    def test_lazily_imported_submodule_is_not_duplicated(self):
        """
        A submodule imported for the first time *through the alias* must
        still be the one and only module object, and must be reachable
        afterwards under the canonical name.
        """
        import importlib
        import trawlarr

        # A module unlikely to have been imported by anything else here.
        dotted_path = 'trawlarr.libs.singleton'
        for name in (dotted_path, dotted_path.replace('trawlarr', 'unmanic', 1)):
            sys.modules.pop(name, None)

        aliased = importlib.import_module('unmanic.libs.singleton')
        canonical = importlib.import_module(dotted_path)

        assert aliased is canonical
        assert sys.modules['unmanic.libs.singleton'] is sys.modules[dotted_path]
        assert trawlarr.libs.singleton is aliased

    def test_aliased_module_keeps_its_canonical_identity_attributes(self):
        """
        The import machinery rewrites dunders on a module handed back by a
        loader. The shim restores them, so an aliased module still reports
        itself under the real name -- tracebacks, logging and pickling all
        read `__name__`.
        """
        import unmanic.libs.filetest

        assert unmanic.libs.filetest.__name__ == 'trawlarr.libs.filetest'
        assert unmanic.libs.filetest.__spec__.name == 'trawlarr.libs.filetest'
        assert unmanic.libs.__name__ == 'trawlarr.libs'

    def test_module_level_state_is_shared_not_copied(self):
        import trawlarr.libs.filetest
        import unmanic.libs.filetest

        sentinel = object()
        trawlarr.libs.filetest._alias_shim_sentinel = sentinel
        try:
            assert unmanic.libs.filetest._alias_shim_sentinel is sentinel
        finally:
            del trawlarr.libs.filetest._alias_shim_sentinel

    def test_alias_is_live_when_only_the_real_package_was_imported(self):
        """
        Nothing has to import a magic module first: importing `trawlarr`
        installs the alias.
        """
        result = _run_in_fresh_interpreter(
            'import trawlarr\n'
            'import unmanic.libs.filetest\n'
            'import trawlarr.libs.filetest\n'
            'assert unmanic.libs.filetest is trawlarr.libs.filetest\n'
            'print("ok")\n'
        )

        assert result.returncode == 0, result.stderr
        assert 'ok' in result.stdout

    def test_alias_is_live_when_it_is_imported_first(self):
        """
        A process that reaches for `unmanic` before `trawlarr` has been
        imported at all still gets the real modules. This is the path the
        legacy `unmanic` console script takes.
        """
        result = _run_in_fresh_interpreter(
            'import unmanic\n'
            'from unmanic.libs.unplugins.plugin_types.plugin_type_base import PluginType\n'
            'import trawlarr\n'
            'from trawlarr.libs.unplugins.plugin_types.plugin_type_base import PluginType as Canonical\n'
            'assert unmanic is trawlarr\n'
            'assert PluginType is Canonical\n'
            'print("ok")\n'
        )

        assert result.returncode == 0, result.stderr
        assert 'ok' in result.stdout

    def test_plugins_importing_the_legacy_package_get_the_real_modules(self):
        """
        Plugins are executed with importlib inside the running process and
        import `unmanic.*` directly. That source is not ours to change, so
        the objects it binds must be the very ones the service uses.
        """
        result = _run_in_fresh_interpreter(
            'import importlib.util, sys\n'
            'src = "from unmanic.libs.unplugins.plugin_types.plugin_type_base import PluginType\\n"\n'
            'spec = importlib.util.spec_from_loader("fake_plugin.plugin", loader=None)\n'
            'module = importlib.util.module_from_spec(spec)\n'
            'exec(compile(src, "plugin.py", "exec"), module.__dict__)\n'
            'from trawlarr.libs.unplugins.plugin_types.plugin_type_base import PluginType\n'
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
        from trawlarr.namespace_shim import AliasFinder

        finder = AliasFinder()
        for name in ('trawlarr', 'trawlarr.libs', 'json', 'unmanicbogus', 'unmanicbogus.thing'):
            assert finder.find_spec(name) is None

    def test_installing_the_alias_twice_adds_only_one_finder(self):
        from trawlarr.namespace_shim import AliasFinder, install

        install()
        install()

        finders = [finder for finder in sys.meta_path if isinstance(finder, AliasFinder)]
        assert len(finders) == 1


@pytest.mark.unittest
class TestFinderHonoursTheMetaPathFinderContract(object):
    """
    A MetaPathFinder must return None for names it cannot supply. The
    step 1 implementation claimed the entire aliased namespace without
    checking, which made `find_spec` lie about modules that do not exist.
    Unreachable then -- nothing imported the alias -- but the alias is now
    the path every plugin takes.
    """

    def test_find_spec_declines_a_module_that_does_not_exist(self):
        from trawlarr.namespace_shim import AliasFinder

        finder = AliasFinder()

        assert finder.find_spec('unmanic.libs.definitely_not_a_module') is None
        assert finder.find_spec('unmanic.not_a_subpackage.at_all') is None

    def test_importlib_find_spec_reports_missing_aliased_modules_as_missing(self):
        import importlib.util

        assert importlib.util.find_spec('unmanic.libs.definitely_not_a_module') is None

    def test_find_spec_still_claims_modules_that_do_exist(self):
        from trawlarr.namespace_shim import AliasFinder

        finder = AliasFinder()
        spec = finder.find_spec('unmanic.libs.filetest')

        assert spec is not None
        assert spec.name == 'unmanic.libs.filetest'

    def test_optional_import_guard_in_plugin_code_behaves(self):
        """
        `try: import unmanic.libs.x except ImportError:` is how a plugin
        feature-detects against an older host. It must take the except
        branch, not receive a half-built module.
        """
        try:
            import unmanic.libs.definitely_not_a_module  # noqa: F401
        except ImportError:
            pass
        else:
            pytest.fail('importing a non-existent aliased module should raise ImportError')

    def test_missing_module_error_names_what_the_caller_asked_for(self):
        """
        A plugin author who writes `unmanic.libs.nope` should not be told
        that `trawlarr.libs.nope` is missing. They never wrote `trawlarr`.
        """
        with pytest.raises(ModuleNotFoundError) as excinfo:
            import unmanic.libs.definitely_not_a_module  # noqa: F401

        assert 'unmanic.libs.definitely_not_a_module' in str(excinfo.value)
        assert 'trawlarr' not in str(excinfo.value)
        assert excinfo.value.name == 'unmanic.libs.definitely_not_a_module'

    def test_an_import_error_from_inside_a_real_module_is_not_rewritten(self):
        """
        The rename of the failing module must only happen when it is the
        aliased module itself that is missing. An ImportError raised by
        code *inside* a real module has to propagate untouched, or a
        genuine dependency problem gets reported as a missing plugin API.
        """
        import importlib

        from trawlarr.namespace_shim import AliasLoader

        loader = AliasLoader()

        class FakeSpec(object):
            name = 'unmanic.libs.filetest'

        real_import_module = importlib.import_module

        def exploding_import(name, *args, **kwargs):
            if name == 'trawlarr.libs.filetest':
                raise ModuleNotFoundError(
                    "No module named 'some_third_party_dep'", name='some_third_party_dep'
                )
            return real_import_module(name, *args, **kwargs)

        importlib.import_module = exploding_import
        try:
            with pytest.raises(ModuleNotFoundError) as excinfo:
                loader.create_module(FakeSpec())
        finally:
            importlib.import_module = real_import_module

        assert excinfo.value.name == 'some_third_party_dep'


@pytest.mark.unittest
class TestLegacyPluginFacingClassNames(object):
    """
    #49 renames the public `Unmanic*` classes plugins touch. The old names
    stay as aliases -- the *same object*, so `is` and `isinstance` checks
    written against either name agree.
    """

    @pytest.mark.parametrize('module_path, legacy_name, canonical_name', [
        ('unmanic.libs.logs', 'UnmanicLogging', 'TrawlarrLogging'),
        ('unmanic.libs.metadata', 'UnmanicFileMetadata', 'TrawlarrFileMetadata'),
        ('unmanic.libs.directoryinfo', 'UnmanicDirectoryInfo', 'TrawlarrDirectoryInfo'),
        ('unmanic.libs.directoryinfo', 'UnmanicDirectoryInfoException', 'TrawlarrDirectoryInfoException'),
        ('unmanic.libs.uiserver', 'UnmanicDataQueues', 'TrawlarrDataQueues'),
        ('unmanic.libs.uiserver', 'UnmanicRunningTreads', 'TrawlarrRunningTreads'),
    ])
    def test_legacy_class_name_is_the_canonical_class(self, module_path, legacy_name, canonical_name):
        import importlib

        legacy_module = importlib.import_module(module_path)
        canonical_module = importlib.import_module(module_path.replace('unmanic', 'trawlarr', 1))

        assert getattr(legacy_module, legacy_name) is getattr(canonical_module, canonical_name)

    def test_a_plugins_verbatim_import_block_still_works(self):
        """
        Import lines copied out of a real community plugin, unmodified.
        """
        result = _run_in_fresh_interpreter(
            'from unmanic.libs.unplugins.settings import PluginSettings\n'
            'from unmanic.libs.directoryinfo import UnmanicDirectoryInfo\n'
            'from unmanic.libs.logs import UnmanicLogging\n'
            'logger = UnmanicLogging.get_logger(name="Plugin.test")\n'
            'from trawlarr.libs.logs import TrawlarrLogging\n'
            'assert UnmanicLogging is TrawlarrLogging\n'
            'print("ok")\n'
        )

        assert result.returncode == 0, result.stderr
        assert 'ok' in result.stdout
