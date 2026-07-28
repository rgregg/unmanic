#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# End-to-end wiring for the per-library extension allow-list (issue #33).
#
# The other tests for this feature exercise the pieces in isolation:
# test_native_done_state.py builds FileTest via __new__ and assigns
# file_extension_allowlist by hand, and test_library_allowlist_upgrade.py
# asserts on the raw database column. Neither runs FileTest.__init__, and
# neither goes through the settings helper -- so the chain that actually
# connects a user's setting to a queueing decision was covered by nothing.
#
# The review of #73 demonstrated the consequence: four separate mutations,
# each of which disables the feature completely end to end, all left the
# suite green at 450/450 --
#   * Library.set_file_extension_allowlist() made a no-op
#   * Library.get_file_extension_allowlist() returning [] always
#   * deleting the set_file_extension_allowlist call in the settings helper
#   * FileTest.__read_library_extension_allowlist() returning () always
#
# The wiring was verified correct at the time by hand, so this is a
# regression guard rather than a bug fix. It walks the whole path: save
# through save_library_config(), read back through a real FileTest built
# from a library id, and assert on the actual scope decision.

import inspect as _inspect
import os
import sys

import pytest
from peewee import Model, SqliteDatabase

from trawlarr.libs.db_migrate import Migrations


@pytest.fixture
def library_db(tmp_path):
    """A real migrated database with the models bound to it.

    peewee-migrate rebinds every model's Meta.database as a side effect of
    creating tables, so this runs inside a bind_ctx that restores the
    original bindings and keeps the rest of the suite unaffected.
    """
    db_file = str(tmp_path / 'trawlarr.db')
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(
        sys.modules['trawlarr.libs.db_migrate'].__file__)))
    settings = {
        "TYPE":                       "SQLITE",
        "FILE":                       db_file,
        "MIGRATIONS_DIR":             os.path.join(app_dir, "migrations_v1"),
        "MIGRATIONS_HISTORY_VERSION": "v1",
    }
    discovered = _inspect.getmembers(sys.modules["trawlarr.libs.unmodels"], _inspect.isclass)
    all_models = [model for _name, model in discovered if issubclass(model, Model)]

    scratch_db = SqliteDatabase(db_file)
    with scratch_db.bind_ctx(all_models):
        Migrations(settings).update_schema()
        yield db_file
    scratch_db.close()


def _new_library(tmp_path, name='Films'):
    from trawlarr.libs.library import Library
    return Library.create({'name': name, 'path': str(tmp_path / name)})


@pytest.mark.unittest
class TestExtensionAllowlistWiring:

    def test_a_saved_allowlist_reaches_the_queueing_decision(self, library_db, tmp_path):
        """The whole chain: settings helper -> database -> FileTest -> scope.

        This is the test whose absence let four feature-disabling mutations
        pass. It must fail if ANY link is broken.
        """
        from trawlarr.libs.filetest import FileTest
        from trawlarr.webserver.helpers.settings import save_library_config

        library = _new_library(tmp_path)
        save_library_config(library.get_id(), {'file_extension_allowlist': ['.MKV', 'mp4']})

        file_test = FileTest(library.get_id())
        assert file_test.file_extension_is_in_library_scope(str(tmp_path / 'ep.mkv')) is True
        assert file_test.file_extension_is_in_library_scope(str(tmp_path / 'poster.jpg')) is False

    def test_an_empty_allowlist_means_unrestricted_not_nothing(self, library_db, tmp_path):
        """The upgrade default, checked through the real read path.

        An allow-list that defaulted to 'match nothing' would silently stop
        all processing on every existing library. Empty must mean no
        restriction.
        """
        from trawlarr.libs.filetest import FileTest

        library = _new_library(tmp_path, name='Shows')
        file_test = FileTest(library.get_id())
        assert file_test.file_extension_allowlist == ()
        assert file_test.file_extension_is_in_library_scope(str(tmp_path / 'anything.xyz')) is True

    def test_clearing_the_allowlist_restores_unrestricted_scope(self, library_db, tmp_path):
        from trawlarr.libs.filetest import FileTest
        from trawlarr.webserver.helpers.settings import save_library_config

        library = _new_library(tmp_path, name='Docs')
        save_library_config(library.get_id(), {'file_extension_allowlist': ['mkv']})
        assert FileTest(library.get_id()).file_extension_is_in_library_scope(
            str(tmp_path / 'a.avi')) is False

        save_library_config(library.get_id(), {'file_extension_allowlist': []})
        assert FileTest(library.get_id()).file_extension_is_in_library_scope(
            str(tmp_path / 'a.avi')) is True

    def test_an_unrelated_settings_save_does_not_wipe_the_allowlist(self, library_db, tmp_path):
        """save_library_config() applies a whole config dict. A caller that
        omits the allow-list must not clear it as a side effect."""
        from trawlarr.libs.filetest import FileTest
        from trawlarr.webserver.helpers.settings import save_library_config

        library = _new_library(tmp_path, name='Kids')
        save_library_config(library.get_id(), {'file_extension_allowlist': ['mkv']})
        save_library_config(library.get_id(), {'priority_score': 5})

        assert FileTest(library.get_id()).file_extension_allowlist == ('mkv',)
