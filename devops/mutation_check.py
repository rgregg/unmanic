#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    devops/mutation_check.py

    Break something on purpose and check that the test suite notices.

    WHY
    ---
    Every gap found during the milestone that produced issue #84 was found by
    mutation, not by reading. Six safety mechanisms in this application were
    each thoroughly unit tested and could each be disconnected from the running
    program by deleting one line, with the suite still green. Nobody spotted
    any of them in review. The suite is the only thing that can, and only if
    somebody asks it.

    Deliberately NOT a CI job. A full mutation run over this package is minutes
    of CPU for a signal nobody reads on a Tuesday, and a slow job on the PR
    path gets skipped and then removed. This is a script you run when you add a
    mechanism, pointed at the mutation you are actually worried about.

    WHAT IT DOES
    ------------
    For each mutation: copy the working tree to a temp directory (tracked and
    untracked files, minus ignores), apply a literal string substitution,
    run the tests there, and report whether the suite went red.

      KILLED    the suite failed. The mutation is covered. Good.
      SURVIVED  the suite passed with the code broken. THAT IS A FINDING:
                whatever the mutated line does, nothing tests that it happens.

    Nothing is written to your working tree, and HOME is redirected inside the
    copy so a test that writes to ~/.trawlarr cannot touch yours.

    USAGE
    -----
    Run the built-in catalogue - the mutations that have already escaped once,
    kept as regression guards:

        python3 devops/mutation_check.py

    Check one mutation of your own:

        python3 devops/mutation_check.py \\
            --file trawlarr/libs/postprocessor.py \\
            --old 'self.record_completed_file()' \\
            --new 'pass' \\
            --tests tests/unit/test_safety_mechanism_call_sites.py

    `--tests` is worth setting. The whole suite takes minutes per mutation;
    naming the file that is supposed to catch it takes seconds, and if that
    file does not catch it the answer is the same either way.

    See docs/CONTRIBUTING.md, "Pinning a call site", for when to write a
    behavioural test instead of relying on this.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

#: The mutations this project has already been bitten by. Each one disabled a
#: shipped mechanism end to end while the suite stayed green; each is now
#: expected to be KILLED. Treat a SURVIVED here as a regression in the tests,
#: not as a curiosity.
#:
#: Keep the `tests` field pointed at the test that is *supposed* to catch it.
CATALOGUE = [
    {
        'label': 'PostProcessor.run() no longer records completed files (#33)',
        'file':  'trawlarr/libs/postprocessor.py',
        'old':   'self.record_completed_file()',
        'new':   'pass',
        'tests': 'tests/unit/test_safety_mechanism_call_sites.py tests/integration/test_pipeline_end_to_end.py',
    },
    {
        'label': 'PostProcessor.run() no longer runs the convergence check (#34)',
        'file':  'trawlarr/libs/postprocessor.py',
        'old':   'self.run_convergence_check()',
        'new':   'pass',
        'tests': 'tests/unit/test_safety_mechanism_call_sites.py',
    },
    {
        'label': 'The plugin loader resolves the pre-rename plugins directory',
        'file':  'trawlarr/libs/unplugins/executor.py',
        'old':   "runtimepaths.APP_DIR_NAME, 'plugins'",
        'new':   "runtimepaths.LEGACY_APP_DIR_NAME, 'plugins'",
        'tests': 'tests/unit/test_plugin_executor_default_directory.py '
                 'tests/integration/test_pipeline_end_to_end.py',
    },
    {
        'label': "The library's extension allow-list always reads back empty",
        'file':  'trawlarr/libs/library.py',
        'old':   'def get_file_extension_allowlist(self):',
        'new':   'def get_file_extension_allowlist(self):\n        return []',
        'tests': 'tests/unit/test_library_allowlist_wiring.py '
                 'tests/integration/test_pipeline_end_to_end.py',
    },
    {
        'label': 'RootService never starts the TaskHandler, so nothing queues a discovered file',
        'file':  'trawlarr/service.py',
        'old':   'self.start_handler(data_queues, task_queue)',
        'new':   'pass',
        'tests': 'tests/integration/test_pipeline_end_to_end.py',
    },
    {
        'label': 'The worker stall detector is never consulted (#15)',
        'file':  'trawlarr/libs/workers.py',
        'old':   'if self.__check_for_stall(tracked_procs):',
        'new':   'if False:',
        'tests': 'tests/unit/test_safety_mechanism_call_sites.py',
    },
]

DEFAULT_TESTS = 'tests/unit tests/integration'


def copy_working_tree(destination):
    """
    Copy every file git would consider part of the project into `destination`.

    Tracked and untracked-but-not-ignored, so uncommitted work is included -
    the point is to test the code in front of you, not the code at HEAD.

    :param destination:
    :return:
    """
    listing = subprocess.run(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
        capture_output=True, check=True)
    files = [name for name in listing.stdout.decode().split('\0') if name]
    for name in files:
        source = os.path.join(os.getcwd(), name)
        if not os.path.isfile(source):
            continue
        target = os.path.join(destination, name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target)
    return len(files)


def apply_mutation(root, relative_path, old, new):
    """
    Replace `old` with `new` in the copied file. Returns the number of
    replacements, so a mutation whose target text has moved is reported as a
    mistake rather than silently testing nothing.

    :param root:
    :param relative_path:
    :param old:
    :param new:
    :return:
    """
    path = os.path.join(root, relative_path)
    with open(path, 'r', encoding='utf-8') as handle:
        content = handle.read()
    occurrences = content.count(old)
    if occurrences:
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(content.replace(old, new))
    return occurrences


def run_tests(root, tests, extra_args):
    """
    Run pytest inside the mutated copy, with HOME redirected.

    :param root:
    :param tests:
    :param extra_args:
    :return: True when the suite passed (i.e. the mutation SURVIVED)
    """
    environment = dict(os.environ)
    environment['HOME'] = tempfile.mkdtemp(prefix='trawlarr-mutation-home-')
    environment.pop('HOME_DIR', None)
    environment['PYTHONPATH'] = root
    command = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider']
    command += tests.split()
    command += extra_args
    completed = subprocess.run(command, cwd=root, env=environment,
                               capture_output=True)
    shutil.rmtree(environment['HOME'], ignore_errors=True)
    return completed.returncode == 0, completed


#: check_one() outcomes. NOT_APPLIED is kept distinct from SURVIVED on
#: purpose: "the text to mutate has moved" is a broken mutation, and
#: reporting it as a surviving one would send the reader hunting for a
#: missing test that is probably there.
KILLED = 'KILLED'
SURVIVED = 'SURVIVED'
NOT_APPLIED = 'NOT_APPLIED'


def check_one(mutation, extra_args, verbose=False):
    """
    Run one mutation. Returns one of KILLED / SURVIVED / NOT_APPLIED.

    :param mutation:
    :param extra_args:
    :param verbose:
    :return:
    """
    label = mutation.get('label') or '{}: {!r} -> {!r}'.format(
        mutation['file'], mutation['old'], mutation['new'])
    print('\n=== {}'.format(label))

    root = tempfile.mkdtemp(prefix='trawlarr-mutation-')
    try:
        copy_working_tree(root)
        occurrences = apply_mutation(root, mutation['file'], mutation['old'], mutation['new'])
        if not occurrences:
            print('    NOT APPLIED  the text to mutate was not found in {}.'.format(mutation['file']))
            print('                 The code has moved. Fix the mutation, not the test.')
            return NOT_APPLIED
        if occurrences > 1:
            print('    note      applied to {} occurrences'.format(occurrences))

        passed, completed = run_tests(root, mutation.get('tests') or DEFAULT_TESTS, extra_args)
        if verbose or passed:
            sys.stdout.write(completed.stdout.decode(errors='replace')[-4000:])
        if passed:
            print('    SURVIVED  nothing failed with this code broken.')
            print('              Whatever that line does, no test proves it happens.')
            return SURVIVED
        print('    KILLED    the suite noticed.')
        return KILLED
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[3].strip())
    parser.add_argument('--file', help='File to mutate, relative to the repo root')
    parser.add_argument('--old', help='Exact text to replace')
    parser.add_argument('--new', default='pass', help='Replacement text (default: "pass")')
    parser.add_argument('--tests', help='Test paths to run (default: the whole suite)')
    parser.add_argument('--label', help='Description used in the report')
    parser.add_argument('--verbose', action='store_true', help='Always print the pytest output')
    parser.add_argument('pytest_args', nargs='*', help='Extra arguments passed through to pytest')
    args = parser.parse_args()

    if args.file or args.old:
        if not (args.file and args.old):
            parser.error('--file and --old must be given together')
        mutations = [{
            'label': args.label,
            'file':  args.file,
            'old':   args.old,
            'new':   args.new,
            'tests': args.tests,
        }]
    else:
        mutations = CATALOGUE
        if args.tests:
            mutations = [dict(m, tests=args.tests) for m in mutations]

    results = [(m, check_one(m, args.pytest_args, verbose=args.verbose)) for m in mutations]
    survivors = [m for m, outcome in results if outcome == SURVIVED]
    not_applied = [m for m, outcome in results if outcome == NOT_APPLIED]

    print('\n{} mutation(s) checked: {} killed, {} survived, {} not applied.'.format(
        len(mutations), len(mutations) - len(survivors) - len(not_applied),
        len(survivors), len(not_applied)))
    for mutation in survivors:
        print('  SURVIVED:    {}'.format(mutation.get('label') or mutation['file']))
    for mutation in not_applied:
        print('  NOT APPLIED: {}'.format(mutation.get('label') or mutation['file']))
    return 1 if (survivors or not_applied) else 0


if __name__ == '__main__':
    raise SystemExit(main())
