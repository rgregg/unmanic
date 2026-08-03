#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_doc_claims.py

    Tripwires for behavioural claims made in the documentation (issue #95).

    In one working session six documents asserted a guarantee the code did
    not provide - a status code, a refusal, a "no plugin can", a "terminal
    for the life of the process". Every one was checkable. None had been
    checked. Meanwhile the only two documented behaviours that had never
    drifted were the two DERIVED from code: the migration commands compared
    against `runtimepaths.legacy_config_migration_command_lines()`, and the
    env-var table parametrised from `envvars.RENAMED_ENV_VARS`.

    So: a sentence in a document that asserts a status code, a path, a
    default, a refusal or a "never"/"always" gets a test here that fails
    when it stops being true. Each test quotes the sentence it defends, so
    an author editing the prose can grep their own words and find the
    assertion.

    Deliberately NOT string-matching the prose. Asserting that a document
    contains a particular sentence catches a typo fix and teaches everyone
    to "fix" the test by editing the expected string. The behaviour is what
    is pinned; the quote is a comment.

    See docs/CONTRIBUTING.md, "Pinning a documented claim", and
    devops/doc_claims.py for the inventory and the network measurements
    that cannot live in CI.

    tests/unit/test_security_model_docs.py predates this file and does the
    same job for docs/SECURITY_MODEL.md; claims already pinned there, in
    test_activity_api.py, test_env_vars.py, test_runtime_paths.py and
    test_plugin_requirements_file_gate.py are not duplicated here.
"""
import json
import os
import re

import pytest
import tornado.httputil
import tornado.web

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _read(*relpath):
    with open(os.path.join(PROJECT_ROOT, *relpath), 'r', encoding='utf-8') as f:
        return f.read()


# ---------------------------------------------------------------------------
# Route resolution
#
# The real Tornado application, asked the same question a real request asks:
# "which handler serves this path?". Cheaper than standing up a listening
# server and strictly more honest than reading the route list, because it
# goes through the same rule ordering that produced the 301-instead-of-404
# bug in the first place -- `add_handlers` inserts at -1, so route order in
# the source is not route order at runtime.
# ---------------------------------------------------------------------------

class _FakeContext(object):
    address = ('127.0.0.1', 1)
    protocol = 'http'
    remote_ip = '127.0.0.1'


class _FakeConnection(object):
    context = _FakeContext()

    def set_close_callback(self, callback):
        pass


@pytest.fixture(scope='module')
def app(tmp_path_factory):
    """The application's real route table."""
    from trawlarr.libs.uiserver import UIServer

    server = UIServer.__new__(UIServer)
    # make_web_app() only reads `developer` to decide whether to mount the
    # dev-only routes. Everything this file asks about is in the production
    # set, so pin against that.
    server.developer = False
    return server.make_web_app()


def _handler_for(app, path):
    """The handler class a GET of `path` would be dispatched to."""
    request = tornado.httputil.HTTPServerRequest(
        method='GET', uri=path, connection=_FakeConnection()
    )
    delegate = app.find_handler(request)
    return getattr(delegate, 'handler_class', None)


def _handler_kwargs_for(app, path):
    request = tornado.httputil.HTTPServerRequest(
        method='GET', uri=path, connection=_FakeConnection()
    )
    delegate = app.find_handler(request)
    return getattr(delegate, 'handler_kwargs', {}) or {}


class TestTheRetiredApiPrefixIsATerminal404:
    """
    README.md: "Anything calling the old `/unmanic/api/v2/` path gets a
    **404** - not a redirect."

    This one has already been false once. A catch-all
    `(r"/(.*)", RedirectHandler)` answered the retired prefix with a
    PERMANENT 301 to the dashboard, so a browser or a `curl -L` cached it
    and handed an API client HTML where it had asked for JSON.
    """

    @pytest.mark.parametrize('path', [
        '/unmanic/api/v2/version',
        '/unmanic/api/v1/session/unmanic-sign-out-url',
        '/unmanic/ui/dashboard/',
        '/unmanic/',
    ])
    def test_the_retired_prefix_reaches_the_not_found_handler(self, app, path):
        from trawlarr.libs.uiserver import NotFoundHandler

        assert _handler_for(app, path) is NotFoundHandler, (
            "'{}' is served by something other than NotFoundHandler. If that is a "
            "redirect again, every client that ever called the retired prefix gets "
            "dashboard HTML instead of JSON -- and if it is permanent, their browser "
            "and their `curl -L` cache it, so the breakage outlives the fix.".format(path)
        )

    def test_an_unknown_path_is_a_404_and_not_a_redirect(self, app):
        from trawlarr.libs.uiserver import NotFoundHandler

        assert _handler_for(app, '/no/such/path') is NotFoundHandler

    def test_the_not_found_handler_is_the_default_and_not_a_route(self):
        # README.md's claim only holds because NotFoundHandler is wired as
        # `default_handler_class`, which Tornado consults after every
        # registered route has declined. As a route it could shadow a real
        # one; as a catch-all rule it would be reordered by add_handlers.
        from trawlarr.libs.uiserver import NotFoundHandler, tornado_settings

        assert tornado_settings.get('default_handler_class') is NotFoundHandler

    def test_a_real_route_is_not_shadowed_by_it(self, app):
        from trawlarr.libs.uiserver import NotFoundHandler

        assert _handler_for(app, '/trawlarr/api/v2/version/read') is not NotFoundHandler


class TestTheDocumentedConvenienceRedirects:
    """
    README.md: "The web UI is at `http://<host>:8888/` ... it redirects to
    `/trawlarr/ui/dashboard/`."
    """

    @pytest.mark.parametrize('path', ['/', '/trawlarr/'])
    def test_the_front_door_redirects_to_the_dashboard(self, app, path):
        assert _handler_for(app, path) is tornado.web.RedirectHandler, (
            "A bare '{}' no longer redirects. The README tells every new user "
            "that http://<host>:8888/ is the web UI; if it 404s instead, the "
            "first thing they do after installing appears to fail.".format(path)
        )
        assert _handler_kwargs_for(app, path).get('url') == '/trawlarr/ui/dashboard/'

    def test_the_ui_root_is_served_rather_than_404ing(self, app):
        # `/trawlarr/ui/` is handled by the frontend handler rather than
        # redirected. Either is fine for the README's claim; a 404 is not.
        from trawlarr.libs.uiserver import NotFoundHandler

        assert _handler_for(app, '/trawlarr/ui/') is not NotFoundHandler

    @pytest.mark.parametrize('path', ['/', '/trawlarr/'])
    def test_those_redirects_are_temporary(self, app, path):
        # A permanent redirect off the site root is cached by browsers and
        # by `curl -L`. "Where the front page lives" is not a promise worth
        # making permanently -- and a cached 301 is how the retired-prefix
        # bug above outlived its own fix.
        assert _handler_kwargs_for(app, path).get('permanent') is False


class TestTheInheritedV1ApiSurface:
    """
    docs/SECURITY_MODEL.md: "Only its account routes are retired:
    `/trawlarr/api/v1/session/*` answers `410 Gone`. The rest of v1 is live,
    functional and just as unauthenticated as v2."

    This is the claim that was wrong. An earlier draft of that bullet said
    the inherited v1 routes "are retired and answer `410 Gone`" full stop,
    which reads as "that whole surface is gone" -- in the one document whose
    job is to tell an operator what an unauthenticated request can reach. It
    can reach pending-task creation, a library rescan, plugin install and a
    filesystem browser.

    FORK.md is the bounded version and always was: only the v1 `/session/*`
    account/login routes were retired. Pinned in both directions, because
    both directions are dangerous: a live route the document calls retired
    understates the attack surface, and a retired route the document calls
    live sends an operator hunting for an exposure that is not there.
    """

    #: The v1 endpoints that still resolve to a working handler, and one
    #: method on each that says what it does. Written out rather than
    #: derived, because "which of these is reachable" is exactly the
    #: question the security document answers and a derived list would
    #: silently follow the code wherever it went.
    LIVE = [
        ('pending', 'pending_api', 'ApiPendingHandler',
         ['create_task_from_path', 'trigger_library_rescan']),
        ('history', 'history_api', 'ApiHistoryHandler',
         ['manage_historic_tasks_list']),
        ('plugins', 'plugins_api', 'ApiPluginsHandler',
         ['install_plugin_by_id']),
        ('filebrowser', 'filebrowser_api', 'ApiFilebrowserHandler',
         ['fetch_directory_listing']),
    ]

    @pytest.mark.parametrize('endpoint, module, handler_name, methods', LIVE,
                             ids=[row[0] for row in LIVE])
    def test_the_v1_endpoint_is_reachable_and_does_what_the_document_says(
            self, app, endpoint, module, handler_name, methods):
        import importlib

        from trawlarr.libs.uiserver import NotFoundHandler
        from trawlarr.webserver.api_request_router import Handle404

        handler = _handler_for(app, '/trawlarr/api/v1/{}/list'.format(endpoint))
        expected = getattr(
            importlib.import_module('trawlarr.webserver.api_v1.{}'.format(module)),
            handler_name)

        assert handler is expected, (
            "/trawlarr/api/v1/{}/ no longer resolves to {} (got {}). If it has "
            "been retired, docs/SECURITY_MODEL.md now lists an unauthenticated "
            "capability that does not exist, and an operator reading it is "
            "defending against the wrong thing.".format(endpoint, handler_name, handler)
        )
        assert handler not in (NotFoundHandler, Handle404)

        for method in methods:
            assert callable(getattr(expected, method, None)), (
                "{}.{} is gone. docs/SECURITY_MODEL.md names it as something an "
                "unauthenticated request can do.".format(handler_name, method)
            )

    def test_no_live_v1_endpoint_answers_the_retirement_410(self):
        # "Live" has to mean more than "a handler exists": a handler that
        # 410s everything would satisfy the dispatch check above while the
        # document's list of capabilities was fiction.
        import importlib

        for endpoint, module, _handler, _methods in self.LIVE:
            source = _read('trawlarr', 'webserver', 'api_v1', '{}.py'.format(module))
            assert 'set_status(410' not in source, (
                'v1 /{}/ now retires something. docs/SECURITY_MODEL.md says the '
                'only retired v1 routes are the session ones.'.format(endpoint))
            importlib.import_module('trawlarr.webserver.api_v1.{}'.format(module))

    def test_the_v1_session_routes_are_the_retired_ones(self, app):
        from trawlarr.webserver.api_v1 import session_api

        handler = _handler_for(app, '/trawlarr/api/v1/session/unmanic-sign-out-url')
        assert handler is session_api.ApiSessionHandler

        # Every route this handler serves goes through write_retired().
        source = _read('trawlarr', 'webserver', 'api_v1', 'session_api.py')
        assert 'self.set_status(410' in source, (
            'The v1 session routes stopped answering 410. Both FORK.md and '
            'docs/SECURITY_MODEL.md say they are retired; if they now do '
            'something, they do it unauthenticated.'
        )

    def test_the_v1_surface_is_not_in_the_swagger_contract(self):
        # The other half of the same sentence: Swagger documents v2 only, so
        # a reader auditing the API from Swagger alone sees none of the
        # above. That is the reason the bullet exists.
        spec = json.loads(_read('trawlarr', 'webserver', 'docs', 'api_schema_v2.json'))
        served = json.dumps(spec.get('paths', {})) + json.dumps(spec.get('basePath', ''))

        assert '/api/v1/' not in served


class TestNothingInTheRequestPathAuthenticates:
    """
    docs/SECURITY_MODEL.md: "There is no login handler, no session cookie,
    no API key, no token check, and no per-route `@authenticated` decorator
    anywhere in the request path."

    test_security_model_docs.py greps uiserver.py, calling itself a tripwire
    rather than a proof. The document says "anywhere in the request path",
    so grep the whole request path: an @authenticated decorator landing on
    one API handler would satisfy that test and falsify the sentence.
    """

    @staticmethod
    def _python_sources():
        roots = [os.path.join(PROJECT_ROOT, 'trawlarr', 'webserver')]
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if d not in ('frontend', 'public', '__pycache__')]
                for name in filenames:
                    if name.endswith('.py'):
                        yield os.path.join(dirpath, name)
        yield os.path.join(PROJECT_ROOT, 'trawlarr', 'libs', 'uiserver.py')

    @pytest.mark.parametrize('needle', [
        'tornado.web.authenticated',
        'get_current_user',
        'set_secure_cookie',
        'WWW-Authenticate',
    ])
    def test_no_authentication_hook_exists_anywhere_in_the_request_path(self, needle):
        offenders = []
        for path in self._python_sources():
            with open(path, 'r', encoding='utf-8') as f:
                if needle in f.read():
                    offenders.append(os.path.relpath(path, PROJECT_ROOT))

        assert not offenders, (
            "'{}' now appears in {}. Either Trawlarr has grown authentication -- in "
            "which case docs/SECURITY_MODEL.md and the README both open with a "
            "warning that is no longer true -- or something looks like auth and "
            "isn't, which is worse.".format(needle, ', '.join(offenders))
        )


class TestTheDocumentedDefaultBindAddress:
    """
    docs/SECURITY_MODEL.md: "`ui_address` defaults to `''` and `ui_port` to
    `8888` (`trawlarr/config.py`), so an unconfigured install listens on
    `0.0.0.0:8888`."

    The single most consequential default in the application, given that
    nothing in front of the port authenticates.
    """

    def test_the_defaults_are_what_the_security_document_says(self):
        source = _read('trawlarr', 'config.py')

        assert re.search(r'self\.ui_port\s*=\s*8888', source), (
            'The default UI port is no longer 8888. docs/SECURITY_MODEL.md and the '
            'README both name that port in their "never expose this" warning, and '
            'the shipped compose files publish it.'
        )
        assert re.search(r"self\.ui_address\s*=\s*''", source), (
            'The default bind address changed. docs/SECURITY_MODEL.md tells the '
            'reader an unconfigured install listens on every interface; if that is '
            'no longer true the advice built on top of it is wrong too.'
        )


class TestTheDocumentedRoutePaths:
    """
    Paths quoted in prose, which are the easiest thing in a document to get
    subtly wrong and the least likely to be noticed: a reader who copies one
    into a script gets a 404 and blames their own typing.
    """

    @pytest.mark.parametrize('module, route', [
        # docs/SECURITY_MODEL.md: "the upload API
        # (`/trawlarr/api/v2/upload/plugin/file`) accepts a plugin zip"
        ('upload_api', r"/upload/plugin/file"),
        # docs/AUTOMATION.md: "`GET /trawlarr/api/v2/activity/status`"
        ('activity_api', r"/activity/status"),
        # docs/AUTOMATION.md: "pause the workers for its duration
        # (`POST /trawlarr/api/v2/workers/worker/pause/all`)"
        ('workers_api', r"/workers/worker/pause/all"),
    ])
    def test_the_documented_route_exists(self, module, route):
        import importlib

        mod = importlib.import_module('trawlarr.webserver.api_v2.{}'.format(module))
        patterns = []
        for name in dir(mod):
            handler = getattr(mod, name)
            for entry in getattr(handler, 'routes', []) or []:
                patterns.append(entry.get('path_pattern'))

        assert route in patterns, (
            "The documented route '{}' is not in {}.routes. A reader copying it out "
            "of the docs into a maintenance script gets a 404 and no explanation "
            "of why.".format(route, module)
        )

    def test_the_swagger_ui_is_where_the_security_document_says(self):
        # docs/SECURITY_MODEL.md: "Swagger UI is served at
        # `/trawlarr/swagger` and documents every endpoint."
        source = _read('trawlarr', 'libs', 'uiserver.py')

        assert '"{}/swagger".format(URL_PREFIX)' in source

    def test_the_checked_in_openapi_contract_is_where_automation_says(self):
        # docs/AUTOMATION.md links the contract by path. A moved file makes
        # that link a 404 on the repo browser.
        path = os.path.join(PROJECT_ROOT, 'trawlarr', 'webserver', 'docs', 'api_schema_v2.json')
        assert os.path.isfile(path)

        with open(path, 'r', encoding='utf-8') as f:
            spec = json.load(f)
        assert '/activity/status' in json.dumps(spec.get('paths', {}))


class TestTheActivityGateThreadClaims:
    """
    docs/AUTOMATION.md, after being bounded in issue #95: the supervisor
    covers "the Foreman, the PostProcessor and the TaskHandler"; a thread it
    has given up on is a terminal 500; the Foreman is ALSO checked live on
    every request and the other two are not.

    The unbounded version of this ("if one of the pipeline threads is not
    running, the endpoint returns 500") was the same shape as three of the
    six false claims in issue #95: a blanket statement over a set where it
    held for one member.
    """

    def test_the_critical_thread_set_is_the_three_the_document_names(self):
        from trawlarr.libs import threadhealth

        assert threadhealth.CRITICAL_THREADS == frozenset(
            {'Foreman', 'PostProcessor', 'TaskHandler'}
        ), (
            'The set of threads whose death takes the activity gate down has '
            'changed. docs/AUTOMATION.md names them one by one, and an external '
            'maintenance job decides whether to start writing to your library '
            'based on that list.'
        )

    def test_giving_up_on_a_thread_is_terminal(self):
        # AUTOMATION.md: "the endpoint keeps returning 500 until Trawlarr is
        # restarted". This was false once (issue #81): the restart budget is
        # a rolling window, so aging out restarts used to un-fail a thread.
        from trawlarr.libs.threadhealth import ThreadHealthRegistry

        registry = ThreadHealthRegistry()
        registry.record_failure('Foreman', 'restart budget exhausted')
        registry.forget_restarts('Foreman')

        assert registry.failed_critical_threads() == ['Foreman'], (
            'A thread the supervisor gave up on stopped being reported as failed. '
            'The activity endpoint goes back to answering `busy: false` from the '
            'task counts of a pipeline that is not running, and an external gate '
            'opens precisely because Trawlarr has broken.'
        )

    def test_the_foreman_is_checked_live_and_the_other_two_are_not(self):
        # This is the BOUND the document now states. If someone adds live
        # checks for the other two, that is an improvement -- and the
        # document's caveat about the supervision-interval window becomes
        # untrue and should be deleted.
        import inspect

        from trawlarr.webserver.helpers import activity

        source = inspect.getsource(activity.get_activity_status)

        assert "get_unmanic_running_thread('foreman')" in source
        assert 'postprocessor' not in source.lower().replace('post-processing', '')


class TestThePluginDependencyPaths:
    """
    docs/PLUGIN-DEPENDENCIES.md: "`~/.trawlarr/plugins/<plugin_id>/site-packages`,
    one directory per plugin" and "Each install writes a receipt at
    `site-packages/.trawlarr-plugin-deps.json`".

    Operators are told to look for these on disk when a plugin will not load
    after an image update.
    """

    def test_the_site_packages_directory_is_named_as_documented(self, tmp_path):
        from trawlarr.libs import plugin_dependencies

        path = plugin_dependencies.site_packages_path(str(tmp_path / 'my_plugin'))
        assert os.path.basename(path) == 'site-packages'
        assert os.path.basename(os.path.dirname(path)) == 'my_plugin'

    def test_the_receipt_is_named_as_documented(self, tmp_path):
        from trawlarr.libs import plugin_dependencies

        path = plugin_dependencies.receipt_path(str(tmp_path / 'my_plugin'))
        assert path.endswith(os.path.join('site-packages', '.trawlarr-plugin-deps.json'))

    def test_the_opt_in_variable_is_named_as_documented(self):
        # Both PLUGIN-DEPENDENCIES.md and SECURITY_MODEL.md name this
        # variable. An operator who sets a variable that no longer exists
        # believes a gate is open when it is shut, or shut when it is open.
        from trawlarr.libs import plugin_dependencies

        assert plugin_dependencies.ALLOW_INSTALL_ENV_VAR == \
            'TRAWLARR_ALLOW_PLUGIN_DEPENDENCY_INSTALL'


class TestTheDocumentedRuntimeNames:
    """
    The package-layout table in docs/DEVELOPING.md and the Unmanic-to-
    Trawlarr table in README.md. Generated in spirit from
    `trawlarr/libs/runtimepaths.py`, which the same document says is where
    these names live: "Change them there, not inline."
    """

    def test_the_names_in_the_tables_are_the_ones_runtimepaths_owns(self):
        from trawlarr.libs import runtimepaths

        assert runtimepaths.APP_DIR_NAME == '.trawlarr'
        assert runtimepaths.LEGACY_APP_DIR_NAME == '.unmanic'
        assert runtimepaths.DATABASE_FILE_NAME == 'trawlarr.db'
        assert runtimepaths.LEGACY_DATABASE_FILE_NAME == 'unmanic.db'
        assert runtimepaths.URL_PREFIX == '/trawlarr'
        assert runtimepaths.API_URL_PREFIX == '/trawlarr/api'
        assert runtimepaths.IGNORE_LEGACY_CONFIG_ENV_VAR == 'TRAWLARR_IGNORE_LEGACY_CONFIG'

    def test_the_environment_variable_prefix_is_the_documented_one(self):
        # README.md: "Environment variables are prefixed `TRAWLARR_`, not
        # `UNMANIC_`" -- and the warning scanner works by prefix, so the
        # prefix is load-bearing rather than cosmetic.
        from trawlarr.libs import envvars

        for legacy, replacement in envvars.RENAMED_ENV_VARS.items():
            assert legacy.startswith('UNMANIC_') or 'UNMANIC' in legacy
            assert 'TRAWLARR' in replacement


class TestTheCoverageFloorsInContributing:
    """
    docs/CONTRIBUTING.md, "The coverage ratchet": a table naming the floor
    value for each suite. The previous version of that table also carried a
    measured-on-`main` column, which went stale within a fortnight; it has
    been deleted rather than pinned. The floors themselves are pinnable, so
    they are pinned.
    """

    @staticmethod
    def _floor_row(suite):
        for line in _read('docs', 'CONTRIBUTING.md').splitlines():
            if line.startswith('| {}'.format(suite)):
                return [cell.strip() for cell in line.strip('|').split('|')]
        raise AssertionError('No floor row for {} in CONTRIBUTING.md'.format(suite))

    def test_the_python_floor_matches_the_workflow(self):
        workflow = _read('.github', 'workflows', 'test.yml')
        match = re.search(r'PYTHON_COVERAGE_FLOOR:\s*"(\d+)"', workflow)
        assert match, 'PYTHON_COVERAGE_FLOOR is no longer declared in test.yml'

        documented = self._floor_row('Python')[2]
        assert documented.startswith(match.group(1)), (
            "CONTRIBUTING.md documents the Python coverage floor as '{}' while "
            "test.yml enforces {}. A contributor reading the doc aims at the wrong "
            "number and finds out from a red build.".format(documented, match.group(1))
        )

    def test_the_frontend_floor_matches_vitest_config(self):
        config = _read('trawlarr', 'webserver', 'frontend', 'vitest.config.js')
        match = re.search(r'functions:\s*(\d+)', config)
        assert match, 'No functions threshold in vitest.config.js'

        documented = self._floor_row('Frontend')[2]
        assert documented.startswith(match.group(1))


class TestTheDocumentedToolInvocations:
    """
    Commands a contributor is told to run. A command that no longer works is
    a claim like any other, and it fails at the worst moment: when someone
    is trying to follow the instructions.

    An earlier version of this class asserted that `devops/doc_claims.py`
    exists and compiles, and that `devops/check_license_headers.sh` exists.
    Both were decoration -- they restated that a file this same commit added
    was still there, and counted as pins while pinning nothing. They are
    replaced here by tests that read the invocation out of the document and
    check that it would still do what the document says it does; the
    inventory's own honesty is pinned by TestThisInventoryIsHonest below.
    """

    @staticmethod
    def _shell_blocks(*document):
        """Every ```bash fenced block in a document, as one string each."""
        return re.findall(r'```(?:bash|sh|console)\n(.*?)```',
                          _read(*document), re.DOTALL)

    @classmethod
    def _documented_commands(cls, *document):
        """Shell command lines, with `\\`-continuations joined up."""
        commands = []
        for block in cls._shell_blocks(*document):
            for line in block.replace('\\\n', ' ').splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    commands.append(' '.join(line.split()))
        return commands

    def _mutation_check_example(self):
        for command in self._documented_commands('docs', 'CONTRIBUTING.md'):
            if 'mutation_check.py' in command and '--old' in command:
                return command
        raise AssertionError(
            'docs/CONTRIBUTING.md no longer shows a worked mutation_check.py '
            'invocation. "Break it and watch the test fail" is the section\'s '
            'whole point; without an example nobody runs the tool.')

    def test_the_mutation_check_example_would_still_mutate_something(self):
        # Read the example out of the document rather than restating it
        # here: an invocation this test hardcodes is one the document can
        # drift away from silently. If the --old string no longer appears in
        # the named --file, the example reports SURVIVED for a substitution
        # that never happened -- which reads as "nothing tests this" when it
        # means "nothing changed".
        command = self._mutation_check_example()

        target = re.search(r"--file\s+(\S+)", command)
        old = re.search(r"--old\s+'([^']+)'", command)
        tests = re.search(r"--tests\s+(\S+)", command)
        assert target and old and tests, (
            'The worked example no longer passes --file, --old and --tests: '
            '{}'.format(command))

        for path in (target.group(1), tests.group(1), 'devops/mutation_check.py'):
            assert os.path.isfile(os.path.join(PROJECT_ROOT, path)), (
                'The worked mutation_check.py example names {}, which does not '
                'exist. A contributor following the section gets an error '
                'instead of a demonstration.'.format(path))

        with open(os.path.join(PROJECT_ROOT, target.group(1)), encoding='utf-8') as f:
            source = f.read()
        assert old.group(1) in source, (
            "The example substitutes {!r} in {}, which no longer contains it. "
            "mutation_check.py reports NOT APPLIED, and the one worked example of "
            "the project's own verification tool does not work.".format(
                old.group(1), target.group(1)))

    @pytest.mark.parametrize('document', [
        ('docs', 'CONTRIBUTING.md'),
        ('docs', 'DEVELOPING.md'),
    ], ids=lambda d: '/'.join(d))
    def test_every_repo_script_a_document_tells_you_to_run_exists(self, document):
        # Catches the whole class of "run devops/<script>" instructions,
        # including the license-header one, without naming any of them here
        # -- a hardcoded list goes stale the first time a script is added.
        missing = []
        for command in self._documented_commands(*document):
            for token in command.split():
                token = token.lstrip('./')
                if re.match(r'^(devops|scripts)/[\w.\-]+$', token):
                    if not os.path.isfile(os.path.join(PROJECT_ROOT, token)):
                        missing.append((token, command))

        assert not missing, '{} tells you to run {}'.format(
            '/'.join(document),
            '; '.join('{} (in `{}`), which does not exist'.format(t, c)
                      for t, c in missing))


class TestThisInventoryIsHonest:
    """
    devops/doc_claims.py is the map of which documented sentences are
    pinned. Issue #95's review found it certifying a row that was not
    pinned at all -- the README's `UNMANIC_*` table, named as defended by
    tests/unit/test_env_vars.py, which had never read the README.

    It printed `[ok]` because its only check was that a file with that name
    existed. A check of "a file with this name exists" certifies anything,
    and a map that certifies anything is worse than no map: it is the thing
    a reviewer consults *instead of* reading the test.

    So the inventory now carries evidence per row and verifies it, and that
    verification runs here. A row that rots is a red suite rather than a
    line of output nobody runs.
    """

    @staticmethod
    def _module():
        import importlib.util

        path = os.path.join(PROJECT_ROOT, 'devops', 'doc_claims.py')
        spec = importlib.util.spec_from_file_location('_doc_claims_under_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_every_row_names_a_test_that_reads_what_it_claims_to_pin(self):
        module = self._module()

        broken = ['{}\n      {}\n      {}'.format(claim.claim, ', '.join(claim.tests),
                                                  verdict.detail)
                  for claim, verdict in module.verify_all()
                  if verdict.status == 'broken']

        assert not broken, (
            'devops/doc_claims.py claims these sentences are pinned by tests that '
            'do not look at them:\n    {}\nA row nobody can check is how the '
            'env-var table came to be listed as pinned for a fortnight while '
            'nothing pinned it.'.format('\n    '.join(broken)))

    def test_no_row_is_silently_unverifiable(self):
        module = self._module()

        for claim, verdict in module.verify_all():
            if verdict.status == 'unverified':
                assert verdict.detail, (
                    'Row "{}" is unverifiable and does not say why. "Unverified" '
                    'without a reason is indistinguishable from an oversight; the '
                    'inventory has a known-unpinned list for claims that cannot '
                    'be checked.'.format(claim.claim))

    def test_the_documents_it_names_exist(self):
        module = self._module()

        missing = []
        for claim in module.PINNED_CLAIMS:
            for part in claim.document.split(' / '):
                if '/' in part or part.endswith('.md') or part.endswith('.py'):
                    if not os.path.exists(os.path.join(PROJECT_ROOT, part)):
                        missing.append(part)

        assert not missing, (
            'The inventory pins claims in documents that do not exist: {}'.format(
                ', '.join(sorted(set(missing)))))

    def test_the_verifier_rejects_a_row_whose_test_does_not_mention_it(self):
        # The check that the check works. Without this, `verify()` could
        # start returning ok unconditionally and every assertion above would
        # pass -- which is exactly the failure it exists to prevent.
        module = self._module()

        fabricated = module.pinned(
            'README.md', 'A claim nothing pins',
            'tests/unit/test_env_vars.py',
            evidence=['NoSuchSymbolAppearsInThatFile'])

        assert module.verify(fabricated).status == 'broken'

    def test_the_verifier_rejects_a_row_naming_a_class_that_is_gone(self):
        module = self._module()

        fabricated = module.pinned(
            'README.md', 'A claim pinned by a class that does not exist',
            'tests/unit/test_doc_claims.py::TestNoSuchClass',
            evidence=['import os'])

        assert module.verify(fabricated).status == 'broken'


class TestTheImageTagTheReadmeTellsYouToPull:
    """
    README.md's quickstart said `docker pull ghcr.io/rgregg/trawlarr:latest`
    when no release had ever been cut, so `:latest` existed only as a frozen
    leftover from the previous tag policy. The very first command in the
    project README handed a new user a stale image.

    The authority for "which tags exist" is not FORK.md's prose -- it is
    `.github/workflows/build.yml`, which is the only thing that pushes any
    of them. On a push to `main` it publishes `:dev` and `:main-<sha>`, and
    deliberately not `:latest`; the semver tags and `:latest` are published
    only on a GitHub *Release* event. Whether one has ever fired is registry
    and release state, not repository state -- it stays in doc_claims.py's
    known-unpinned list. What IS pinnable is that the tag the README's first
    command hands a new user is one CI pushes on every merge, which is the
    property that was actually violated.

    An earlier version of this class guarded its pin behind
    ``if '`:latest` does not exist' in fork:`` -- a prose match, which is the
    one thing docs/CONTRIBUTING.md says not to do. Rewording that sentence in
    FORK.md would have silently switched the assertion off and left the class
    green, which is how a pin becomes decoration.
    """

    @staticmethod
    def _readme_quickstart_tag():
        match = re.search(r'docker pull ghcr\.io/rgregg/trawlarr:(\S+)', _read('README.md'))
        assert match, 'README.md no longer shows a `docker pull` in its quickstart'
        return match.group(1)

    @staticmethod
    def _tags_published_from_main():
        """The literal tags build.yml pushes on a push to `main`."""
        workflow = _read('.github', 'workflows', 'build.yml')
        # The `else` branch of the release/continuous split.
        match = re.search(r'TAGS="\$\{IMAGE\}:([^"]+)"\s*\n\s*fi', workflow)
        assert match, (
            'build.yml no longer resolves its continuous-build tags in a single '
            'TAGS= assignment. This pin reads that line to find out which tags '
            'exist without a release; re-point it rather than deleting it.'
        )
        tags = set()
        for part in ('${IMAGE}:' + match.group(1)).split(','):
            name = part.split(':', 1)[1]
            if '${' not in name:  # :main-${SHA_SHORT} is per-commit
                tags.add(name)
        assert tags, 'No fixed tag is published from main at all.'
        return tags

    @staticmethod
    def _readme_tag_table():
        """{tag: the 'Exists today' cell}, from README's tag table."""
        rows = {}
        for line in _read('README.md').splitlines():
            match = re.match(r'^\|\s*`:([A-Za-z0-9.\-]+)`\s*\|(.+)\|(.+)\|\s*$', line)
            if match:
                rows[match.group(1)] = match.group(3).strip()
        return rows

    def test_the_quickstart_and_the_run_command_pull_the_same_tag(self):
        readme = _read('README.md')
        tag = self._readme_quickstart_tag()
        assert 'ghcr.io/rgregg/trawlarr:{}\n'.format(tag) in readme, (
            'The README tells you to pull one tag and run another. Whichever is '
            'wrong, the reader ends up running an image they did not choose.'
        )

    def test_the_quickstart_pulls_a_tag_the_build_workflow_actually_pushes(self):
        tag = self._readme_quickstart_tag()
        published = self._tags_published_from_main()

        assert tag in published, (
            "README.md's quickstart pulls `:{}`, which build.yml does not publish "
            "on a push to main (it publishes {}). Every other tag comes from a "
            "GitHub Release event, and there has not been one -- so the first "
            "command in the README hands a new user either nothing or a build "
            "frozen at whenever the tag policy last changed.".format(
                tag, ', '.join(':' + t for t in sorted(published)))
        )

    def test_the_tag_table_marks_exactly_the_published_tags_as_existing(self):
        # The table describes a policy that is not in effect yet, so its
        # "Exists today" column is the only thing keeping four of its five
        # rows from being claims about tags nobody can pull.
        table = self._readme_tag_table()
        assert table, 'The README image-tag table no longer parses as a table.'

        marked = {tag for tag, cell in table.items()
                  if re.match(r'^\**yes\b', cell, re.IGNORECASE)}

        assert marked == self._tags_published_from_main(), (
            'The README tag table says {} exist(s) today; build.yml publishes {} '
            'without a release. A row marked "yes" for a tag nobody pushes sends '
            'a reader to pull an image that is not there.'.format(
                sorted(marked) or 'nothing', sorted(self._tags_published_from_main()))
        )

    def test_fork_md_documents_every_tag_the_readme_offers(self):
        # Cross-document consistency, structurally: FORK.md's image-tag table
        # is the reference list. A tag the README recommends and FORK.md has
        # never heard of is one of the two documents being out of date.
        fork_tags = set(re.findall(r'`ghcr\.io/rgregg/trawlarr:([A-Za-z0-9.\-]+)`',
                                   _read('FORK.md')))
        missing = sorted(set(self._readme_tag_table()) - fork_tags)

        assert not missing, (
            'README.md offers tag(s) {} that FORK.md does not document. The two '
            'tag policies disagreed once already, and the reader following the '
            'README got the losing side of it.'.format(', '.join(missing)))


class TestTheSanityCheckComments:
    """
    trawlarr/libs/sanity.py carried two sentences that did not describe the
    code (issue #95, failure 5). Both are now pinned by the behaviour they
    describe.
    """

    @staticmethod
    def _probe():
        return {'streams': [{'codec_type': 'video', 'codec_name': 'h264'}]}

    def test_one_file_probing_and_the_other_not_still_reports_unchecked(self):
        # The docstring used to say checked=False means "no probe of EITHER
        # file". It does not: a comparison needs a PAIR of probes, so one
        # file probing and the other not is also nothing to compare.
        from trawlarr.libs import sanity

        result = sanity.evaluate(
            source_probe=None, output_probe=self._probe(),
            source_size=None, output_size=None,
        )
        assert result.checked is False
        assert result.failures == []

    def test_the_feature_being_disabled_also_reports_unchecked(self, tmp_path):
        # ...and so does the feature simply being switched off, which the
        # old docstring's "i.e. the tooling itself was unavailable" excluded.
        from trawlarr.libs import sanity

        result = sanity.check_task_output(
            str(tmp_path / 'in.mkv'), str(tmp_path / 'out.mkv'),
            settings=sanity.SanityCheckSettings(enabled=False),
        )
        assert result.checked is False

    def test_a_readable_source_and_an_unreadable_output_is_a_failure(self):
        # The comment used to list "an unreadable mount" among the faults
        # that fail both files identically and are therefore NOT reported
        # here. Source and output live on different mounts, so a storage
        # fault can perfectly well fail one and not the other -- and when it
        # is the output's, this must fire.
        from trawlarr.libs import sanity

        result = sanity.evaluate(
            source_probe=self._probe(), output_probe=None,
            source_size=1000, output_size=1000,
        )
        assert [f['id'] for f in result.failures] == [sanity.CHECK_UNPROBEABLE_OUTPUT], (
            'An output that ffprobe cannot read a single stream from is being '
            'delivered to the library again. That is the single most damaged file '
            'a task can produce, and it was once the one case that always passed.'
        )

    def test_both_files_unprobeable_with_no_sizes_is_not_a_failure(self):
        from trawlarr.libs import sanity

        result = sanity.evaluate(
            source_probe=None, output_probe=None,
            source_size=None, output_size=None,
        )
        assert result.checked is False
        assert result.failures == []


class TestEveryRelativeLinkInTheDocsResolves:
    """
    Not a behavioural claim, but the cheapest possible one to get wrong and
    the one a reader hits first. A dead link in SECURITY_MODEL.md pointing
    at the reverse-proxy example is a reader who does not deploy the
    reverse-proxy example.
    """

    DOCUMENTS = [
        ('README.md',),
        ('FORK.md',),
        ('docs', 'SECURITY_MODEL.md'),
        ('docs', 'PLUGIN-DEPENDENCIES.md'),
        ('docs', 'AUTOMATION.md'),
        ('docs', 'DEVELOPING.md'),
        ('docs', 'CONTRIBUTING.md'),
    ]

    @pytest.mark.parametrize('document', DOCUMENTS, ids=lambda d: '/'.join(d))
    def test_every_relative_link_points_at_a_file_that_exists(self, document):
        text = _read(*document)
        base = os.path.dirname(os.path.join(PROJECT_ROOT, *document))

        broken = []
        for target in re.findall(r'\]\(([^)\s]+)\)', text):
            if target.startswith(('http://', 'https://', '#', 'mailto:')):
                continue
            target = target.split('#', 1)[0]
            if not target:
                continue
            if not os.path.exists(os.path.normpath(os.path.join(base, target))):
                broken.append(target)

        assert not broken, '{} links to missing files: {}'.format(
            '/'.join(document), ', '.join(sorted(set(broken))))
