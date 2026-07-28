#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    trawlarr.__init__.py
 
    Written by:               Josh.5 <jsunnex@gmail.com>
    Date:                     04 May 2020, (11:20 AM)
 
    Copyright:
           Copyright (C) Josh Sunnex - All Rights Reserved
 
           Permission is hereby granted, free of charge, to any person obtaining a copy
           of this software and associated documentation files (the "Software"), to deal
           in the Software without restriction, including without limitation the rights
           to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
           copies of the Software, and to permit persons to whom the Software is
           furnished to do so, subject to the following conditions:
  
           The above copyright notice and this permission notice shall be included in all
           copies or substantial portions of the Software.
  
           THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
           EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
           MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
           IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
           DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
           OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
           OR OTHER DEALINGS IN THE SOFTWARE.

"""

import warnings

# Fork addition: register the legacy `unmanic` name as an alias of this
# package, so that `unmanic.<anything>` resolves to the very same module
# objects as `trawlarr.<anything>`. Installed here rather than in a helper
# that callers must remember to import, so it is live for anything that
# touches the package at all -- in particular community plugins, which are
# exec'd in-process and import `unmanic.*` by name. See
# trawlarr/namespace_shim.py and issue #49.
from .namespace_shim import install as _install_namespace_alias

_install_namespace_alias()

from .metadata import __author__
from .metadata import __version__
from .metadata import __description__
from .metadata import __disclaimer__
from .metadata import __forum__
from .metadata import __video__
from .metadata import __website__
from .metadata import __copyright__
