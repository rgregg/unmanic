#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# Backwards-compatibility bootstrap for the legacy `unmanic` namespace
# (issue #49, step 2).
#
# `trawlarr` is the real package; this is nothing but a doorway into it
# for a process that reaches for the old name first -- most importantly a
# community plugin, whose source says `from unmanic.libs... import ...`
# and which is exec'd inside the running service. Importing the real
# package installs the alias finder (see trawlarr/namespace_shim.py),
# which then serves every `unmanic.*` submodule from the real module
# objects.
#
# In the common case this file is never executed at all: the service and
# the test suite import `trawlarr` first, the finder is already on
# sys.meta_path by the time any plugin runs, and it claims the `unmanic`
# name before the path-based finder ever looks in this directory.

import sys

import trawlarr
from trawlarr.namespace_shim import install

install()

# Replace this bootstrap module with the real package, so that
# `unmanic is trawlarr` and there is only ever one module object. The
# import machinery re-reads sys.modules[__name__] after executing a
# module, so callers of `import unmanic` receive the replacement.
sys.modules[__name__] = trawlarr
