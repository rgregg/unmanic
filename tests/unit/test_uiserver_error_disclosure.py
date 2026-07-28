#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# An unhandled request must not hand a Python traceback to the caller.
#
# This became reachable in the same change that introduced NotFoundHandler.
# Before it, the application ended its route list with a catch-all redirect,
# so no request ever reached an error path and the traceback setting did not
# matter. With a real 404 handler in place it matters a great deal:
# docs/SECURITY_MODEL.md states plainly that Trawlarr performs no
# authentication, so anything served on an error page goes to whoever can
# reach the port.
#
# The trap is that `serve_traceback` is never set to True anywhere in the
# non-developer path. It arrives implicitly: tornado.web.Application.__init__
# does `if self.settings.get("debug"): self.settings.setdefault(
# "serve_traceback", True)`, and `debug` is True at module scope. Setting it
# explicitly to False wins, because setdefault does not override an existing
# key -- while update_tornado_settings() can still switch it back on for a
# developer run.

import pytest
import tornado.web

from trawlarr.libs.uiserver import tornado_settings


@pytest.mark.unittest
class TestErrorPagesDoNotDiscloseTracebacks(object):

    def test_serve_traceback_is_explicitly_disabled(self):
        assert tornado_settings.get('serve_traceback') is False, (
            "serve_traceback must be set explicitly; leaving it implicit lets "
            "tornado's debug flag turn it on"
        )

    def test_debug_does_not_re_enable_tracebacks_on_the_application(self):
        # The regression this pins: Application.__init__ setdefault()s
        # serve_traceback=True whenever debug is truthy.
        app = tornado.web.Application([], **tornado_settings)
        assert app.settings.get('debug') is True
        assert app.settings.get('serve_traceback') is False

    def test_a_developer_run_can_still_opt_back_in(self):
        # update_tornado_settings() assigns directly rather than via
        # setdefault, so the developer path is unaffected by the explicit
        # False above. Guard that assumption without mutating the shared dict.
        settings = dict(tornado_settings)
        settings['serve_traceback'] = True
        app = tornado.web.Application([], **settings)
        assert app.settings.get('serve_traceback') is True
