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
         the test that defends it - and, more usefully, checks that each
         named test still exists. A reviewer looking at a documentation diff
         can ask "is this sentence one of the pinned ones?" and get an
         answer without reading the whole suite.

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
import json
import os
import sys
import tempfile
import zipfile

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

REPO_JSON_URL = 'https://raw.githubusercontent.com/Unmanic/unmanic-plugins/repo/repo.json'


#: The inventory. Each entry: the document, a short form of the claim, and
#: the test that fails when it stops being true.
#:
#: Keep this list honest rather than complete-looking. If you pin a claim,
#: add it. If you delete a claim, delete the row. A row naming a test that
#: does not exist is reported as an error below, which is the only automatic
#: check this script performs.
PINNED_CLAIMS = [
    (
        'README.md',
        'The retired /unmanic/api/v2/ prefix answers 404, not a redirect',
        'tests/unit/test_doc_claims.py::TestTheRetiredApiPrefixIsATerminal404',
    ),
    (
        'README.md',
        'http://<host>:8888/ redirects to /trawlarr/ui/dashboard/',
        'tests/unit/test_doc_claims.py::TestTheDocumentedConvenienceRedirects',
    ),
    (
        'README.md',
        'The migration command block is what the application prints',
        'tests/unit/test_runtime_paths.py',
    ),
    (
        'README.md',
        'The UNMANIC_* -> TRAWLARR_* table is the full inventory (eight)',
        'tests/unit/test_env_vars.py',
    ),
    (
        'README.md',
        'The quickstart pulls a tag FORK.md agrees exists',
        'tests/unit/test_doc_claims.py::TestTheImageTagTheReadmeTellsYouToPull',
    ),
    (
        'README.md / docs/DEVELOPING.md',
        'Config dir, database, API prefix, env prefix in the rename tables',
        'tests/unit/test_doc_claims.py::TestTheDocumentedRuntimeNames',
    ),
    (
        'docs/SECURITY_MODEL.md',
        'Nothing in the request path authenticates',
        'tests/unit/test_doc_claims.py::TestNothingInTheRequestPathAuthenticates',
    ),
    (
        'docs/SECURITY_MODEL.md',
        'No xsrf_cookies, no cookie_secret, no auth headers',
        'tests/unit/test_security_model_docs.py::TestDocumentedAuthBehaviour',
    ),
    (
        'docs/SECURITY_MODEL.md',
        'ui_address defaults to \'\' and ui_port to 8888',
        'tests/unit/test_doc_claims.py::TestTheDocumentedDefaultBindAddress',
    ),
    (
        'docs/SECURITY_MODEL.md',
        'The Caddy example publishes no Trawlarr port and authenticates *',
        'tests/unit/test_security_model_docs.py::TestReverseProxyExample',
    ),
    (
        'docs/SECURITY_MODEL.md',
        'docker-compose-ssl.yml binds to loopback only',
        'tests/unit/test_security_model_docs.py::TestTlsTestFixtureStaysLoopback',
    ),
    (
        'docs/SECURITY_MODEL.md',
        'The plugin upload route path, and the Swagger UI path',
        'tests/unit/test_doc_claims.py::TestTheDocumentedRoutePaths',
    ),
    (
        'docs/AUTOMATION.md',
        'busy is false only when every worker is idle and all counts are zero',
        'tests/unit/test_activity_api.py',
    ),
    (
        'docs/AUTOMATION.md',
        'An undeterminable state is a 500, never {"busy": false}',
        'tests/unit/test_activity_api.py',
    ),
    (
        'docs/AUTOMATION.md',
        'The three supervised pipeline threads; giving up on one is terminal;'
        ' only the Foreman is checked live',
        'tests/unit/test_doc_claims.py::TestTheActivityGateThreadClaims',
    ),
    (
        'docs/AUTOMATION.md',
        'The /activity/status and workers pause/all route paths',
        'tests/unit/test_doc_claims.py::TestTheDocumentedRoutePaths',
    ),
    (
        'docs/PLUGIN-DEPENDENCIES.md',
        'All four package-manager routes are behind the one flag',
        'tests/unit/test_plugin_requirements_file_gate.py',
    ),
    (
        'docs/PLUGIN-DEPENDENCIES.md',
        'A requirements file naming nothing is a no-op, not a refusal',
        'tests/unit/test_plugin_requirements_file_gate.py',
    ),
    (
        'docs/PLUGIN-DEPENDENCIES.md',
        'Failures are terminal: no plugin record is written',
        'tests/unit/test_plugin_requirements_file_gate.py',
    ),
    (
        'docs/PLUGIN-DEPENDENCIES.md',
        'site-packages path and receipt filename, and the opt-in variable name',
        'tests/unit/test_doc_claims.py::TestThePluginDependencyPaths',
    ),
    (
        'docs/CONTRIBUTING.md',
        'The coverage floor values match the workflow and vitest.config.js',
        'tests/unit/test_doc_claims.py::TestTheCoverageFloorsInContributing',
    ),
    (
        'docs/CONTRIBUTING.md',
        'The worked mutation_check.py invocation still mutates something',
        'tests/unit/test_doc_claims.py::TestTheDocumentedToolInvocations',
    ),
    (
        'trawlarr/libs/sanity.py',
        'What checked=False means, and which probe failures are a failure',
        'tests/unit/test_doc_claims.py::TestTheSanityCheckComments',
    ),
    (
        'all six documents',
        'Every relative link resolves to a file that exists',
        'tests/unit/test_doc_claims.py::TestEveryRelativeLinkInTheDocsResolves',
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
        'Registry state, not repository state. The cross-document consistency '
        'of the tag the README recommends IS pinned; what is actually pushed '
        'is not.',
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


def _relative_test_target(target):
    return target.split('::', 1)[0]


def print_inventory():
    print('Pinned documentation claims')
    print('=' * 78)
    missing = []
    for document, claim, test in PINNED_CLAIMS:
        path = os.path.join(PROJECT_ROOT, _relative_test_target(test))
        ok = os.path.isfile(path)
        if not ok:
            missing.append(test)
        print('  [{}] {}\n        {}\n        {}'.format(
            'ok' if ok else '!!', document, claim, test))
    print()
    print('Known-unpinned claims')
    print('=' * 78)
    for document, claim, why in UNPINNED_CLAIMS:
        print('  [--] {}\n        {}\n        {}'.format(document, claim, why))
    print()
    print('{} pinned, {} deliberately unpinned.'.format(
        len(PINNED_CLAIMS), len(UNPINNED_CLAIMS)))
    if missing:
        print()
        print('ERROR: {} inventory row(s) name a test file that does not exist:'.format(
            len(missing)))
        for test in missing:
            print('  {}'.format(test))
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
