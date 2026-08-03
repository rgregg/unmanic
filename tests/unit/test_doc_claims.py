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
    """

    def test_the_mutation_check_example_still_mutates_something(self):
        # docs/CONTRIBUTING.md shows a worked mutation_check.py invocation.
        # If the --old string no longer appears in the named file, the
        # example reports SURVIVED for a substitution that never happened --
        # which reads as "nothing tests this" when it means "nothing changed".
        assert os.path.isfile(os.path.join(PROJECT_ROOT, 'devops', 'mutation_check.py'))
        assert 'self.record_completed_file()' in _read('trawlarr', 'libs', 'postprocessor.py')
        assert os.path.isfile(os.path.join(
            PROJECT_ROOT, 'tests', 'unit', 'test_safety_mechanism_call_sites.py'))

    def test_the_doc_claims_helper_exists_and_is_executable_python(self):
        path = os.path.join(PROJECT_ROOT, 'devops', 'doc_claims.py')
        assert os.path.isfile(path)
        compile(_read('devops', 'doc_claims.py'), path, 'exec')

    def test_the_license_header_script_referenced_everywhere_exists(self):
        assert os.path.isfile(os.path.join(PROJECT_ROOT, 'devops', 'check_license_headers.sh'))


class TestTheImageTagTheReadmeTellsYouToPull:
    """
    README.md's quickstart and FORK.md's build-pipeline section have to
    agree about which tag exists. They did not: the quickstart said
    `docker pull ghcr.io/rgregg/trawlarr:latest` while FORK.md said, in
    terms, that `:latest` is a leftover from a retired policy and that
    production tracks `:dev` until 1.0.0 is cut.

    This is a consistency pin rather than a truth pin -- nothing in this
    tree knows what is in the registry. It fails when the two documents
    disagree, which is what actually happened.
    """

    @staticmethod
    def _readme_quickstart_tag():
        match = re.search(r'docker pull ghcr\.io/rgregg/trawlarr:(\S+)', _read('README.md'))
        assert match, 'README.md no longer shows a `docker pull` in its quickstart'
        return match.group(1)

    def test_the_quickstart_and_the_run_command_pull_the_same_tag(self):
        readme = _read('README.md')
        tag = self._readme_quickstart_tag()
        assert 'ghcr.io/rgregg/trawlarr:{}\n'.format(tag) in readme, (
            'The README tells you to pull one tag and run another. Whichever is '
            'wrong, the reader ends up running an image they did not choose.'
        )

    def test_the_readme_does_not_recommend_a_tag_fork_md_says_is_stale(self):
        fork = _read('FORK.md')
        tag = self._readme_quickstart_tag()

        if '`:latest` does not exist' in fork:
            assert tag != 'latest', (
                'FORK.md says `:latest` does not track releases yet, and the README '
                'quickstart still tells a new user to pull it. The first command in '
                'the project README hands them a frozen build from whenever the tag '
                'policy changed.'
            )

    def test_the_readme_tag_table_marks_which_tags_exist(self):
        # The table describes a policy that is not in effect yet. It has to
        # say so, or every row of it is a claim about tags nobody can pull.
        readme = _read('README.md')
        assert 'Exists today' in readme, (
            'The image-tag table no longer distinguishes the tags that exist from '
            'the ones the release policy will create. Four of its five rows '
            'describe tags that have never been published.'
        )


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
