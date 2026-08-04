#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    devops/doc_claims.py

    Which documented behavioural claims are pinned, and by what.

    WHY
    ---
    Issue #95: in one working session six documents asserted a guarantee the
    code did not provide. All six were checkable in under a minute; none had
    been checked. The two documented behaviours that had never drifted were
    the two derived from code - the migration commands byte-compared against
    `runtimepaths.legacy_config_migration_command_lines()`, and the env-var
    table parametrised from `envvars.RENAMED_ENV_VARS`.

    WHAT THIS IS NOT
    ----------------
    It is not the checker. The assertions live in ordinary unit tests
    (tests/unit/test_doc_claims.py and friends) and already run on every PR
    in milliseconds, which is the right place for them: a claim worth
    pinning is worth pinning where the suite will notice.

    Building a second, doc-parsing gate on top of that was considered and
    rejected. To check "this sentence is true" automatically you must first
    identify the sentence, which means either string-matching prose - a test
    that a typo fix turns red, and that everyone quickly learns to "fix" by
    editing the expected string - or annotating every claim in every
    document with a machine-readable marker nobody will keep up to date.
    Both produce a gate people route around, which is worse than no gate.
    The same argument that keeps mutation_check.py out of CI.

    So this is two things a test suite cannot be:

      1. An INVENTORY. `doc_claims.py` with no arguments prints every
         documented claim that currently has a pin, the file it lives in and
         the test that defends it. A reviewer looking at a documentation
         diff can ask "is this sentence one of the pinned ones?" and get an
         answer without reading the whole suite.

         An inventory that lies is worse than none, and the first version of
         this one did: its only check was that a file with the named path
         EXISTED, so it printed `[ok]` beside a row claiming
         `tests/unit/test_env_vars.py` pinned the README's env-var table
         when nothing in that file had ever read the README. A check of
         "a file with this name exists" certifies anything.

         So every row now carries EVIDENCE: literal strings that must appear
         in the named test's source - the document it reads, the symbol it
         compares against. The row is verified by finding them, and a row
         that names a class must name one the file defines. A claim nobody
         can check that way is marked `[??] unverified` and says why; it is
         never printed as `ok`. `tests/unit/test_doc_claims.py` runs this
         verification, so a row that rots fails the suite rather than
         waiting for someone to run the script.

         Evidence is a necessary condition, not a sufficient one - it
         proves the test is looking at the right thing, not that it asserts
         the right thing about it. The sufficient check is the one
         docs/CONTRIBUTING.md describes: break the behaviour with
         `devops/mutation_check.py` and watch the test fail.

      2. The MEASUREMENTS CI CANNOT MAKE. One claim in
         docs/PLUGIN-DEPENDENCIES.md is a survey of somebody else's
         repository: what the 56 plugins in the official Unmanic catalog
         actually ship. That cannot be a unit test - it needs the network,
         it measures a tree nobody here controls, and as a CI job it would
         be both slow and flaky. It can be a script that carries its own
         date, and that is what `--survey-plugin-catalog` is.

    USAGE
    -----
        python3 devops/doc_claims.py
        python3 devops/doc_claims.py --survey-plugin-catalog

    The survey downloads ~56 plugin zips (a few MB) and prints the table
    that belongs in docs/PLUGIN-DEPENDENCIES.md. When you re-run it, update
    the table AND its date, or delete the paragraph. A measurement with no
    date is a claim nobody can check.
"""
import argparse
import collections
import json
import os
import re
import sys
import tempfile
import zipfile

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

REPO_JSON_URL = 'https://raw.githubusercontent.com/Unmanic/unmanic-plugins/repo/repo.json'


#: One row of the inventory.
#:
#:   document  - where the sentence lives.
#:   claim     - a short form of it.
#:   tests     - the test(s) that fail when it stops being true, each as
#:               `path` or `path::ClassName`. More than one when the claim
#:               spans them: "all four routes are gated" is not pinned by a
#:               file that only knows about three of them.
#:   evidence  - literal strings that must appear in those tests' combined
#:               source. This is what makes the row checkable: the document
#:               the test reads, the symbol it compares against, the path it
#:               asserts. Empty means "cannot be checked mechanically", and
#:               the row is reported as UNVERIFIED rather than ok.
#:   why       - required when `evidence` is empty: why not.
Claim = collections.namedtuple('Claim', 'document claim tests evidence why')


def pinned(document, claim, tests, evidence=(), why=''):
    assert evidence or why, 'An unverifiable row must say why: {}'.format(claim)
    if isinstance(tests, str):
        tests = [tests]
    return Claim(document, claim, tuple(tests), tuple(evidence), why)


#: The inventory.
#:
#: Keep this list honest rather than complete-looking. If you pin a claim,
#: add it with the evidence that proves the test is looking at it. If you
#: delete a claim, delete the row. Rows are verified by `verify()` below and
#: by tests/unit/test_doc_claims.py, so a stale row is a red suite.
PINNED_CLAIMS = [
    pinned(
        'README.md',
        'The retired /unmanic/api/v2/ prefix answers 404, not a redirect',
        'tests/unit/test_doc_claims.py::TestTheRetiredApiPrefixIsATerminal404',
        evidence=['/unmanic/api/v2/version', 'NotFoundHandler'],
    ),
    pinned(
        'README.md',
        'http://<host>:8888/ redirects to /trawlarr/ui/dashboard/',
        'tests/unit/test_doc_claims.py::TestTheDocumentedConvenienceRedirects',
        evidence=['/trawlarr/ui/dashboard/', 'RedirectHandler'],
    ),
    pinned(
        'README.md',
        'The migration command block is what the application prints',
        'tests/unit/test_runtime_paths.py',
        evidence=['README.md', 'legacy_config_migration_command_lines'],
    ),
    pinned(
        'README.md',
        'The UNMANIC_* -> TRAWLARR_* table is the full inventory (eight)',
        'tests/unit/test_env_vars.py',
        # This is the row that was certified pinned and was not. The
        # evidence is the two things a real pin has to touch: the document
        # the table is in, and the dict it has to equal.
        evidence=['README.md', 'Renaming the environment variables',
                  'RENAMED_ENV_VARS'],
    ),
    pinned(
        'README.md',
        'The quickstart pulls a tag the build workflow actually publishes',
        'tests/unit/test_doc_claims.py::TestTheImageTagTheReadmeTellsYouToPull',
        evidence=['docker pull ghcr', 'build.yml'],
    ),
    pinned(
        'README.md / docs/DEVELOPING.md',
        'Config dir, database, API prefix, env prefix in the rename tables',
        'tests/unit/test_doc_claims.py::TestTheDocumentedRuntimeNames',
        evidence=['runtimepaths.APP_DIR_NAME', 'IGNORE_LEGACY_CONFIG_ENV_VAR'],
    ),
    pinned(
        'docs/SECURITY_MODEL.md',
        'Nothing in the request path authenticates',
        'tests/unit/test_doc_claims.py::TestNothingInTheRequestPathAuthenticates',
        evidence=['tornado.web.authenticated', 'get_current_user'],
    ),
    pinned(
        'docs/SECURITY_MODEL.md',
        'No xsrf_cookies, no cookie_secret, no auth headers',
        'tests/unit/test_security_model_docs.py::TestDocumentedAuthBehaviour',
        evidence=['xsrf_cookies', 'cookie_secret'],
    ),
    pinned(
        'docs/SECURITY_MODEL.md',
        'Of the inherited v1 API only /session/* is retired; pending, history,'
        ' plugins and filebrowser are live and unauthenticated',
        'tests/unit/test_doc_claims.py::TestTheInheritedV1ApiSurface',
        evidence=['api_v1', 'ApiPendingHandler', 'ApiFilebrowserHandler',
                  'set_status(410'],
    ),
    pinned(
        'docs/SECURITY_MODEL.md',
        'ui_address defaults to \'\' and ui_port to 8888',
        'tests/unit/test_doc_claims.py::TestTheDocumentedDefaultBindAddress',
        evidence=['ui_address', 'ui_port', '8888'],
    ),
    pinned(
        'docs/SECURITY_MODEL.md',
        'The Caddy example publishes no Trawlarr port and authenticates *',
        'tests/unit/test_security_model_docs.py::TestReverseProxyExample',
        evidence=['Caddyfile', 'basic_auth'],
    ),
    pinned(
        'docs/SECURITY_MODEL.md',
        'docker-compose-ssl.yml binds to loopback only',
        'tests/unit/test_security_model_docs.py::TestTlsTestFixtureStaysLoopback',
        evidence=['docker-compose-ssl.yml', '127.0.0.1'],
    ),
    pinned(
        'docs/SECURITY_MODEL.md',
        'The plugin upload route path, and the Swagger UI path',
        'tests/unit/test_doc_claims.py::TestTheDocumentedRoutePaths',
        evidence=['/upload/plugin/file', 'swagger'],
    ),
    pinned(
        'docs/AUTOMATION.md',
        'busy is false only when every worker is idle and all counts are zero',
        'tests/unit/test_activity_api.py',
        evidence=['busy', 'idle'],
    ),
    pinned(
        'docs/AUTOMATION.md',
        'An undeterminable state is a 500, never {"busy": false}',
        'tests/unit/test_activity_api.py',
        evidence=['500', 'busy'],
    ),
    pinned(
        'docs/AUTOMATION.md',
        'The three supervised pipeline threads; giving up on one is terminal;'
        ' only the Foreman is checked live',
        'tests/unit/test_doc_claims.py::TestTheActivityGateThreadClaims',
        evidence=['CRITICAL_THREADS', 'forget_restarts', 'PostProcessor'],
    ),
    pinned(
        'docs/AUTOMATION.md',
        'The /activity/status and workers pause/all route paths',
        'tests/unit/test_doc_claims.py::TestTheDocumentedRoutePaths',
        evidence=['/activity/status', '/workers/worker/pause/all'],
    ),
    pinned(
        'docs/PLUGIN-DEPENDENCIES.md',
        'All four package-manager routes are behind the one flag',
        # Two files, because the claim is about four routes and the
        # file-gate suite knows about three of them. The single-file version
        # of this row passed the old "does a file with this name exist"
        # check while `python_dependencies` -- the fourth route, and the one
        # the document lists first -- appeared nowhere in it.
        ['tests/unit/test_plugin_requirements_file_gate.py',
         'tests/unit/test_plugin_declared_dependencies.py'],
        evidence=['requirements.post-install.txt', 'defer_dependency_install',
                  'package.json', 'python_dependencies'],
    ),
    pinned(
        'docs/PLUGIN-DEPENDENCIES.md',
        'A requirements file naming nothing is a no-op, not a refusal',
        'tests/unit/test_plugin_requirements_file_gate.py',
        evidence=['is_a_no_op'],
    ),
    pinned(
        'docs/PLUGIN-DEPENDENCIES.md',
        'Failures are terminal: no plugin record is written',
        'tests/unit/test_plugin_requirements_file_gate.py',
        evidence=['no plugin record'],
    ),
    pinned(
        'docs/PLUGIN-DEPENDENCIES.md',
        'site-packages path and receipt filename, and the opt-in variable name',
        'tests/unit/test_doc_claims.py::TestThePluginDependencyPaths',
        evidence=['site-packages', '.trawlarr-plugin-deps.json',
                  'ALLOW_INSTALL_ENV_VAR'],
    ),
    pinned(
        'docs/CONTRIBUTING.md',
        'The coverage floor values match the workflow and vitest.config.js',
        'tests/unit/test_doc_claims.py::TestTheCoverageFloorsInContributing',
        evidence=['CONTRIBUTING.md', 'PYTHON_COVERAGE_FLOOR', 'vitest.config.js'],
    ),
    pinned(
        'docs/CONTRIBUTING.md',
        'The worked mutation_check.py invocation is one that still applies,'
        ' and the commands it tells you to run exist',
        'tests/unit/test_doc_claims.py::TestTheDocumentedToolInvocations',
        evidence=['CONTRIBUTING.md', 'mutation_check.py', '--old'],
    ),
    pinned(
        'devops/doc_claims.py',
        'Every row of this inventory names a test that pins what it claims',
        'tests/unit/test_doc_claims.py::TestThisInventoryIsHonest',
        evidence=['doc_claims', 'PINNED_CLAIMS', 'verify'],
    ),
    pinned(
        'trawlarr/libs/sanity.py',
        'What checked=False means, and which probe failures are a failure',
        'tests/unit/test_doc_claims.py::TestTheSanityCheckComments',
        evidence=['sanity.evaluate', 'checked'],
    ),
    pinned(
        'trawlarr/webserver/api_v2/README.md',
        'The error taxonomy table: every code, and the status it maps to',
        'tests/unit/test_api_error_contract.py::TestTheDocumentedErrorContract',
        evidence=['README.md', 'API_ERROR_CODES'],
    ),
    pinned(
        'trawlarr/webserver/api_v2/README.md',
        'The example 500 body is the envelope the code actually builds',
        'tests/unit/test_api_error_contract.py::TestTheDocumentedErrorContract',
        evidence=['README.md', 'build_error_envelope'],
    ),
    pinned(
        'all seven documents',
        'Every relative link resolves to a file that exists',
        'tests/unit/test_doc_claims.py::TestEveryRelativeLinkInTheDocsResolves',
        evidence=['SECURITY_MODEL.md', 'os.path.exists'],
    ),
]


#: Claims that are deliberately NOT pinned, and why. Being explicit about
#: this is the point of the exercise: an unpinned claim a reader can see is
#: flagged is better than one they cannot.
UNPINNED_CLAIMS = [
    (
        'docs/PLUGIN-DEPENDENCIES.md',
        'What the 56 catalog plugins actually ship',
        'A survey of an upstream repository. Needs the network, measures a '
        'tree nobody here controls. Carries its date; re-run with '
        '--survey-plugin-catalog.',
    ),
    (
        'README.md / FORK.md',
        'Which image tags exist in GHCR right now',
        'Registry state, not repository state -- and whether a GitHub Release '
        'has ever fired is release state, which the repository cannot see '
        'either. What IS pinned is that the tag the README quickstart hands a '
        'new user is one build.yml pushes on every merge to main.',
    ),
    (
        'docs/DEVELOPING.md',
        'The Docker development instructions and profiling recipes',
        'Would need a Docker daemon and a built image. The smoke workflow '
        'covers the image; these recipes are not worth a gate.',
    ),
    (
        'FORK.md',
        'The upstream-difference audit trail ("what we changed and why")',
        'Historical narrative rather than a live guarantee. The behavioural '
        'claims inside it - retired 410 routes, the namespace shim, the '
        'runtime names - are pinned by their own suites.',
    ),
]


#: The result of checking one row. `status` is 'ok', 'unverified' or
#: 'broken'; `detail` says what is wrong when it is not ok.
Verdict = collections.namedtuple('Verdict', 'status detail')


def _split_target(target):
    """`path::ClassName` -> (path, 'ClassName' or None)."""
    if '::' in target:
        path, _, name = target.partition('::')
        return path, name
    return target, None


def verify(claim):
    """Check that the tests named by `claim` plausibly pin it.

    Three things, in increasing order of usefulness:

      1. The test file exists. (The old check, and the whole of it.)
      2. If the row names a class, the file defines that class. A renamed
         class leaves a row pointing at nothing.
      3. Every evidence string appears somewhere in the named tests. This
         is the one that matters: it is what tells the difference between a
         test that reads README.md and a test that merely lives in a file
         whose name mentions the same subject.

    Necessary, not sufficient. Evidence proves the test is looking at the
    right thing. Only mutation testing proves it would notice.
    """
    combined = []
    for target in claim.tests:
        relpath, class_name = _split_target(target)
        path = os.path.join(PROJECT_ROOT, relpath)

        if not os.path.isfile(path):
            return Verdict('broken', 'no such test file: {}'.format(relpath))

        with open(path, 'r', encoding='utf-8') as handle:
            source = handle.read()

        if class_name and not re.search(r'^class {}\b'.format(re.escape(class_name)),
                                        source, re.MULTILINE):
            return Verdict('broken', '{} defines no class {}'.format(relpath, class_name))

        combined.append(source)

    haystack = '\n'.join(combined)
    absent = [token for token in claim.evidence if token not in haystack]
    if absent:
        return Verdict('broken', '{} never mention(s) {}'.format(
            ', '.join(claim.tests), ', '.join(repr(token) for token in absent)))

    if not claim.evidence:
        return Verdict('unverified', claim.why)

    # The test must be looking at THIS document. Evidence alone is not enough:
    # a row can cite tokens that happen to appear in some unrelated test and
    # collect an [ok] for a claim nothing checks. That is not hypothetical --
    # a review pinned an invented security guarantee ("Every request is
    # rejected unless it carries a valid API token") to tests/unit/test_env_vars.py
    # with evidence of 'README.md' and 'RENAMED_ENV_VARS', and this function
    # certified it. An inventory that can rubber-stamp is worse than no
    # inventory, because it converts "unchecked" into "checked".
    # Only when the row names an actual file. A few rows describe a set
    # ("all seven documents") rather than a path, and those are checked by
    # the test itself walking the set.
    document = os.path.basename(claim.document)
    if document.endswith('.md') and document not in haystack:
        return Verdict('broken', '{} never open(s) or name(s) {} - evidence can be '
                                 'satisfied by an unrelated test'.format(
                                     ', '.join(claim.tests), document))

    return Verdict('ok', '')


def verify_all():
    """[(claim, verdict)] for the whole inventory."""
    return [(claim, verify(claim)) for claim in PINNED_CLAIMS]


_MARKER = {'ok': 'ok', 'unverified': '??', 'broken': '!!'}


def print_inventory():
    results = verify_all()

    print('Pinned documentation claims')
    print('=' * 78)
    for claim, verdict in results:
        print('  [{}] {}\n        {}\n        {}'.format(
            _MARKER[verdict.status], claim.document, claim.claim,
            '\n        '.join(claim.tests)))
        if verdict.detail:
            print('        -> {}'.format(verdict.detail))
    print()
    print('Known-unpinned claims')
    print('=' * 78)
    for document, claim, why in UNPINNED_CLAIMS:
        print('  [--] {}\n        {}\n        {}'.format(document, claim, why))
    print()

    broken = [(claim, verdict) for claim, verdict in results
              if verdict.status == 'broken']
    unverified = [claim for claim, verdict in results
                  if verdict.status == 'unverified']

    print('{} pinned and verified, {} pinned but unverifiable, '
          '{} deliberately unpinned.'.format(
              len(results) - len(broken) - len(unverified),
              len(unverified), len(UNPINNED_CLAIMS)))
    print('"Verified" means the named test reads the document or the symbol the')
    print('row claims it pins. It does not mean the assertion is a good one --')
    print('for that, break the behaviour with devops/mutation_check.py.')

    if broken:
        print()
        print('ERROR: {} inventory row(s) do not check what they claim:'.format(
            len(broken)))
        for claim, verdict in broken:
            print('  {}\n    {}\n    {}'.format(
                claim.claim, ', '.join(claim.tests), verdict.detail))
        return 1

    print('Run the pins:  pytest tests/unit/test_doc_claims.py')
    return 0


def survey_plugin_catalog():
    """
    Re-measure the claim in docs/PLUGIN-DEPENDENCIES.md about what the
    official catalog's plugins ship.

    Downloads every published zip and counts, because the alternative -
    reading the plugins' source repositories - measures what their `master`
    branch looks like today rather than what a Trawlarr install would
    actually download and unpack.
    """
    import urllib.request

    print('Fetching {}'.format(REPO_JSON_URL))
    with urllib.request.urlopen(REPO_JSON_URL) as response:
        repo = json.loads(response.read().decode('utf-8'))

    base = repo['repo']['repo_data_directory'].rstrip('/')
    plugins = repo['plugins']
    print('{} plugins in the catalog.'.format(len(plugins)))

    counts = {
        'total':             len(plugins),
        'post_install':      0,
        'defer':             0,
        'requirements':      0,
        'requirements_empty': 0,
        'requirements_named': 0,
        'vendored':          0,
        'package_json':      0,
        'declared':          0,
    }
    named_but_not_vendored = []
    failures = []

    workdir = tempfile.mkdtemp(prefix='trawlarr-catalog-survey-')
    for index, plugin in enumerate(plugins, start=1):
        plugin_id = plugin['id']
        version = plugin['version']
        url = '{0}/{1}/{1}-{2}.zip'.format(base, plugin_id, version)
        destination = os.path.join(workdir, '{}-{}.zip'.format(plugin_id, version))
        sys.stdout.write('\r  [{}/{}] {}{}'.format(
            index, len(plugins), plugin_id, ' ' * 20))
        sys.stdout.flush()
        try:
            urllib.request.urlretrieve(url, destination)
        except Exception as error:  # noqa: BLE001 - reported, not swallowed
            failures.append((plugin_id, str(error)))
            continue

        with zipfile.ZipFile(destination) as archive:
            names = archive.namelist()

            def _ships(filename):
                return any(name.rsplit('/', 1)[-1] == filename for name in names)

            info = {}
            for name in names:
                if name.endswith('info.json'):
                    info = json.loads(archive.read(name))
                    break

            vendored = any('site-packages/' in name for name in names)
            counts['vendored'] += bool(vendored)
            counts['post_install'] += bool(_ships('requirements.post-install.txt'))
            counts['defer'] += bool(info.get('defer_dependency_install'))
            counts['declared'] += bool(info.get('python_dependencies'))
            counts['package_json'] += bool(_ships('package.json'))

            requirements = [n for n in names if n.rsplit('/', 1)[-1] == 'requirements.txt']
            if requirements:
                counts['requirements'] += 1
                text = archive.read(requirements[0]).decode('utf-8', 'replace')
                lines = [line.strip() for line in text.splitlines()
                         if line.strip() and not line.strip().startswith('#')]
                if lines:
                    counts['requirements_named'] += 1
                    if not vendored:
                        named_but_not_vendored.append(plugin_id)
                else:
                    counts['requirements_empty'] += 1

    print('\n')
    if failures:
        print('{} zip(s) could not be downloaded; the counts below are incomplete:'.format(
            len(failures)))
        for plugin_id, error in failures:
            print('  {}: {}'.format(plugin_id, error))
        print()

    print('| Measured | Count |')
    print('|---|---|')
    print('| Plugins in the catalog | {} |'.format(counts['total']))
    print('| Ship a `requirements.post-install.txt` | {} |'.format(counts['post_install']))
    print('| Set `defer_dependency_install` | {} |'.format(counts['defer']))
    print('| Ship a `requirements.txt` | {} |'.format(counts['requirements']))
    print('| …of those, naming no package at all (empty or comments only) | {} |'.format(
        counts['requirements_empty']))
    print('| …of those, naming at least one package | {} |'.format(
        counts['requirements_named']))
    print('| Ship a vendored `site-packages/` | {} |'.format(counts['vendored']))
    print('| Ship a `package.json` | {} |'.format(counts['package_json']))
    print('| Declare `python_dependencies` in `info.json` | {} |'.format(counts['declared']))
    print()

    if named_but_not_vendored:
        print('Plugins that NAME packages without vendoring site-packages/: {}'.format(
            ', '.join(sorted(named_but_not_vendored))))
        print('If this list is non-empty, the "exactly the 16 above" line in')
        print('docs/PLUGIN-DEPENDENCIES.md is no longer true. Fix it.')
    else:
        print('Every plugin naming a package also vendors site-packages/.')

    print()
    print('The gate only reads a `requirements.txt` when the plugin also sets')
    print('`defer_dependency_install`. While that count is 0, no catalog plugin')
    print('changes behaviour under TRAWLARR_ALLOW_PLUGIN_DEPENDENCY_INSTALL.')
    print()
    print('Update the table AND its date in docs/PLUGIN-DEPENDENCIES.md, or')
    print('delete the paragraph. An undated measurement is a claim nobody can check.')
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='Inventory of pinned documentation claims (issue #95).',
    )
    parser.add_argument(
        '--survey-plugin-catalog', action='store_true',
        help='Re-measure what the official Unmanic plugin catalog ships. '
             'Needs the network; downloads ~56 zips.',
    )
    args = parser.parse_args()

    if args.survey_plugin_catalog:
        return survey_plugin_catalog()
    return print_inventory()


if __name__ == '__main__':
    sys.exit(main())
