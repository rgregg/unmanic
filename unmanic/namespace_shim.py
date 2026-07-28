#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# Namespace alias shim: make `trawlarr.*` resolve to the *same module
# objects* as `unmanic.*`.
#
# This is step 1 of the rename tracked in issue #49. Today `unmanic` is
# still the real package and `trawlarr` is the alias. When the tree moves
# (step 2) the two names swap, and only the two constants below change --
# the mechanism is direction-agnostic on purpose.
#
# Why a meta path finder rather than `sys.modules['trawlarr'] = unmanic`:
#
#   The naive sys.modules entry only covers the top-level name. On
#   `import trawlarr.libs.filetest`, the import machinery resolves the
#   parent (`trawlarr` -> the unmanic module object), then goes looking for
#   a *child* named `trawlarr.libs` using the parent's `__path__`. It finds
#   unmanic/libs/ on disk and happily builds a second, independent module
#   object for it. `trawlarr.libs.filetest is not unmanic.libs.filetest`,
#   and every module-level singleton in that tree -- logger handles, the
#   settings object, plugin caches -- silently exists twice. A meta path
#   finder is consulted *before* the path-based finder for every name at
#   every depth, so it intercepts the whole subtree and hands back the
#   already-imported real module instead of loading a copy.

import importlib
import importlib.abc
import importlib.util
import sys

# The name callers may use.
ALIAS_NAME = 'trawlarr'
# The name that actually exists on disk.
TARGET_NAME = 'unmanic'

# Attributes that the import machinery re-initialises on a module handed
# back by create_module(). `__spec__` is overwritten unconditionally by
# importlib._bootstrap._init_module_attrs; the rest are only set when
# absent, which for an already-imported module they are not. We restore
# all of them anyway so the real module is never left describing itself
# under the alias name.
_RESTORED_ATTRIBUTES = ('__name__', '__loader__', '__package__', '__spec__')


def _is_aliased(fullname):
    """Is this module name inside the aliased namespace?"""
    return fullname == ALIAS_NAME or fullname.startswith(ALIAS_NAME + '.')


def _target_for(fullname):
    """Map an alias module name onto the real module name."""
    return TARGET_NAME + fullname[len(ALIAS_NAME):]


class AliasLoader(importlib.abc.Loader):
    """
    Loader that "loads" an aliased module by importing the real one and
    returning that exact object. No source is read, nothing is executed
    twice, and no second module object is ever created.
    """

    def __init__(self):
        # Attributes saved between create_module() and exec_module(),
        # keyed by id() of the module object. The two calls are adjacent
        # within a single import, and distinct alias names always resolve
        # to distinct target modules, so the key is unambiguous.
        self._saved_attributes = {}

    def create_module(self, spec):
        module = importlib.import_module(_target_for(spec.name))
        self._saved_attributes[id(module)] = {
            name: getattr(module, name) for name in _RESTORED_ATTRIBUTES if hasattr(module, name)
        }
        # Registered here as well as by the import machinery so that a
        # partially-completed import still resolves to the real module.
        sys.modules[spec.name] = module
        return module

    def exec_module(self, module):
        # The real module was executed when it was first imported. All
        # that is left to do is undo the dunder rewrite the machinery
        # performed on our behalf on the way in.
        saved = self._saved_attributes.pop(id(module), None)
        if not saved:
            return
        for name, value in saved.items():
            try:
                setattr(module, name, value)
            except AttributeError:
                pass


class AliasFinder(importlib.abc.MetaPathFinder):
    """
    Meta path finder claiming every name in the aliased namespace, at any
    depth, before the normal path-based finder gets a chance to load a
    duplicate from disk.
    """

    def __init__(self):
        self._loader = AliasLoader()

    def find_spec(self, fullname, path=None, target=None):
        if not _is_aliased(fullname):
            return None
        if fullname == ALIAS_NAME and TARGET_NAME not in sys.modules:
            # The alias package is being imported before the real one --
            # e.g. `python -c "import trawlarr"`. Let the small bootstrap
            # package on disk import the real package, which installs this
            # finder and rebinds the name. Claiming it here instead would
            # recurse.
            return None
        return importlib.util.spec_from_loader(fullname, self._loader)

    def invalidate_caches(self):
        pass


def _finder_installed():
    return any(isinstance(finder, AliasFinder) for finder in sys.meta_path)


def install():
    """
    Install the alias. Idempotent, and cheap enough to call from a package
    __init__.

    Called from `unmanic/__init__.py`, so the alias is live for anything
    that imports the package -- the service, the test suite, and community
    plugins loaded via importlib into the running process, none of which
    have to know the shim exists. The `trawlarr/` bootstrap package covers
    the one remaining case: a process that imports the alias name first.
    """
    if not _finder_installed():
        sys.meta_path.insert(0, AliasFinder())
    target = sys.modules.get(TARGET_NAME)
    if target is not None and sys.modules.get(ALIAS_NAME) is not target:
        sys.modules[ALIAS_NAME] = target
