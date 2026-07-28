#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# Bootstrap for the `trawlarr` namespace (issue #49, step 1).
#
# `unmanic` is still the real package; this is nothing but a doorway into
# it for a process that reaches for the `trawlarr` name first. Importing
# the real package installs the alias finder (see
# unmanic/namespace_shim.py), which then serves every `trawlarr.*`
# submodule from the real module objects.
#
# This whole directory disappears in step 2, when the tree moves and the
# shim inverts.

import sys

import unmanic
from unmanic.namespace_shim import install

install()

# Replace this bootstrap module with the real package, so that
# `trawlarr is unmanic` and there is only ever one module object. The
# import machinery re-reads sys.modules[__name__] after executing a
# module, so callers of `import trawlarr` receive the replacement.
sys.modules[__name__] = unmanic
