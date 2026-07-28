#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# Namespace alias shim: make `unmanic.*` resolve to the *same module
# objects* as `trawlarr.*`.
#
# Step 2 of the rename tracked in issue #49 moved the tree, so the two
# names have now swapped: `trawlarr` is the real package on disk and
# `unmanic` is the backwards-compatibility alias. The mechanism is
# direction-agnostic; only the two constants below changed.
#
# This is no longer a nicety. Every community plugin in the ecosystem --
# including the ones this deployment runs -- has `from unmanic.libs...`
# written into its source, and plugins are exec'd inside the running
# process (see libs/unplugins/executor.py). This shim is the entire
# reason those keep working.
#
# Why a meta path finder rather than `sys.modules['unmanic'] = trawlarr`:
#
#   The naive sys.modules entry only covers the top-level name. On
#   `import unmanic.libs.filetest`, the import machinery resolves the
#   parent (`unmanic` -> the trawlarr module object), then goes looking for
#   a *child* named `unmanic.libs` using the parent's `__path__`. It finds
#   trawlarr/libs/ on disk and happily builds a second, independent module
#   object for it. `unmanic.libs.filetest is not trawlarr.libs.filetest`,
#   and every module-level singleton in that tree -- logger handles, the
#   settings object, plugin caches -- silently exists twice. A meta path
#   finder is consulted *before* the path-based finder for every name at
#   every depth, so it intercepts the whole subtree and hands back the
#   already-imported real module instead of loading a copy.

import importlib
import importlib.abc
import importlib.util
import sys

# The legacy name callers may still use.
ALIAS_NAME = 'unmanic'
# The name that actually exists on disk.
TARGET_NAME = 'trawlarr'

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


def _target_is_importable(target_name):
    """
    Can `target_name` actually be imported?

    A MetaPathFinder must return None for names it cannot supply --
    `importlib.util.find_spec()` and every `try: import ... except
    ImportError` in the wild rely on that. Claiming the whole aliased
    namespace unconditionally would make `find_spec('unmanic.libs.nope')`
    hand back a truthy spec for a module that does not exist, and the
    failure would only surface later as an exception from the loader.
    """
    if target_name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(target_name) is not None
    except (ImportError, AttributeError, ValueError):
        # ImportError: an ancestor package does not exist.
        # AttributeError/ValueError: an ancestor exists but is not a
        # package, or carries a broken __spec__.
        return False


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
        target_name = _target_for(spec.name)
        try:
            module = importlib.import_module(target_name)
        except ModuleNotFoundError as err:
            if getattr(err, 'name', None) != target_name:
                # Something *inside* the real module failed to import.
                # That is the real module's problem, not the alias's.
                raise
            # Name the module the caller actually wrote. A plugin author
            # who typos `unmanic.libs.nope` should not be told that
            # `trawlarr.libs.nope` is missing -- they never mentioned
            # `trawlarr` and may not know it exists.
            raise ModuleNotFoundError(
                "No module named {!r}".format(spec.name), name=spec.name
            ) from err
        self._saved_attributes[id(module)] = {
            name: getattr(module, name) for name in _RESTORED_ATTRIBUTES if hasattr(module, name)
        }
        # Registered here as well as by the import machinery so that a
        # partially-completed import still resolves to the real module.
        sys.modules[spec.name] = module
        return module

    def _target_loader(self, fullname):
        """
        The real loader behind an aliased name, or None.

        `runpy` (and anything else using the InspectLoader/ExecutionLoader
        protocols) asks the *loader* for code and source rather than going
        through the module object, so those calls have to be forwarded to
        whoever actually loaded the real module.
        """
        target_name = _target_for(fullname)
        module = sys.modules.get(target_name)
        loader = getattr(getattr(module, '__spec__', None), 'loader', None)
        if loader is None:
            try:
                spec = importlib.util.find_spec(target_name)
            except (ImportError, AttributeError, ValueError):
                return None, target_name
            loader = getattr(spec, 'loader', None) if spec is not None else None
        return loader, target_name

    def _delegate(self, method, fullname):
        loader, target_name = self._target_loader(fullname)
        if loader is None or not hasattr(loader, method):
            raise ImportError(
                "alias loader cannot {} for {!r}".format(method, fullname), name=fullname
            )
        return getattr(loader, method)(target_name)

    def is_package(self, fullname):
        # Without this, spec_from_loader() leaves submodule_search_locations
        # unset and every aliased package looks like a plain module. That is
        # what broke `python -m unmanic`: runpy saw a non-package and went
        # straight for its code object instead of looking for __main__.
        loader, target_name = self._target_loader(fullname)
        if loader is not None and hasattr(loader, 'is_package'):
            try:
                return loader.is_package(target_name)
            except ImportError:
                pass
        module = sys.modules.get(target_name)
        return hasattr(module, '__path__')

    def get_code(self, fullname):
        return self._delegate('get_code', fullname)

    def get_source(self, fullname):
        return self._delegate('get_source', fullname)

    def get_filename(self, fullname):
        return self._delegate('get_filename', fullname)

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
            # e.g. `python -c "import unmanic"`. Let the small bootstrap
            # package on disk import the real package, which installs this
            # finder and rebinds the name. Claiming it here instead would
            # recurse.
            return None
        if not _target_is_importable(_target_for(fullname)):
            # Decline rather than promise a module we cannot deliver.
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

    Called from `trawlarr/__init__.py`, so the alias is live for anything
    that imports the package -- the service, the test suite, and community
    plugins loaded via importlib into the running process, none of which
    have to know the shim exists. The `unmanic/` bootstrap package covers
    the one remaining case: a process that imports the alias name first.
    """
    if not _finder_installed():
        sys.meta_path.insert(0, AliasFinder())
    target = sys.modules.get(TARGET_NAME)
    if target is not None and sys.modules.get(ALIAS_NAME) is not target:
        sys.modules[ALIAS_NAME] = target
