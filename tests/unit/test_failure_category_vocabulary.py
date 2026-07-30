#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# Every failure category a caller records must be one the vocabulary
# knows about.
#
# `taskfailure.normalise_category()` degrades an unrecognised category to
# CATEGORY_UNKNOWN rather than raising -- correct at runtime, since a
# failure while recording a failure would be worse than an imprecise
# label. But it means a caller can record a category that does not exist
# and get silence: the task is still marked failed, so nothing looks
# broken, and only the *reason* quietly becomes "unknown".
#
# That already happened. #40 records 'configuration' when it refuses a
# task whose plugin has a required setting unset; #25 defines the
# category vocabulary. Each was green on its own branch. Merged, the
# refusal was recorded as UNKNOWN, and the only thing that noticed was
# one assertion on a log header. Had that assertion been slightly looser,
# a whole failure category would have silently collapsed into "unknown"
# in the health view.
#
# This pins the vocabulary against its callers so the next feature that
# introduces a category has to add it here too.

import ast
import inspect
import os

import pytest

from trawlarr.libs import taskfailure


def _recorded_categories():
    """String literals passed as the `category` argument to the failure
    recorders, found across the package.

    Static rather than behavioural on purpose: the point is to catch a
    caller that no test exercises, which is exactly the case that got
    through.
    """
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(taskfailure.__file__)))
    recorders = {'record', 'record_task_failure'}
    found = {}
    for dirpath, dirnames, filenames in os.walk(package_root):
        dirnames[:] = [d for d in dirnames if d not in ('frontend', '__pycache__', 'migrations_v1')]
        for filename in filenames:
            if not filename.endswith('.py'):
                continue
            path = os.path.join(dirpath, filename)
            with open(path, encoding='utf-8') as handle:
                try:
                    tree = ast.parse(handle.read(), filename=path)
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, 'attr', None) or getattr(func, 'id', None)
                if name not in recorders:
                    continue
                # category is the second positional arg of record(), and the
                # first of the worker's record_task_failure() helper.
                index = 1 if name == 'record' else 0
                if len(node.args) > index and isinstance(node.args[index], ast.Constant):
                    value = node.args[index].value
                    if isinstance(value, str):
                        found.setdefault(value, set()).add(os.path.relpath(path, package_root))
    return found


@pytest.mark.unittest
class TestFailureCategoryVocabulary:

    def test_every_recorded_category_is_a_known_one(self):
        recorded = _recorded_categories()
        assert recorded, "found no literal category arguments -- the AST scan is broken, not the code"

        unknown = {
            category: sorted(files)
            for category, files in recorded.items()
            if category not in taskfailure.KNOWN_CATEGORIES
        }
        assert unknown == {}, (
            "These categories are recorded but not in KNOWN_CATEGORIES, so "
            "normalise_category() silently turns them into '{}': {}. Add them to "
            "taskfailure.KNOWN_CATEGORIES.".format(taskfailure.CATEGORY_UNKNOWN, unknown)
        )

    def test_the_configuration_category_exists(self):
        """The specific regression: #40 records this, #25 defines the list."""
        assert taskfailure.CATEGORY_CONFIGURATION in taskfailure.KNOWN_CATEGORIES
        assert taskfailure.normalise_category('configuration') == taskfailure.CATEGORY_CONFIGURATION

    def test_an_unrecognised_category_still_degrades_rather_than_raising(self):
        """The safety behaviour this test does not want to change: recording
        a failure must never itself raise."""
        assert taskfailure.normalise_category('not_a_real_category') == taskfailure.CATEGORY_UNKNOWN
        assert taskfailure.normalise_category(None) == taskfailure.CATEGORY_UNKNOWN

    def test_categories_are_unique(self):
        categories = list(taskfailure.KNOWN_CATEGORIES)
        assert len(categories) == len(set(categories))

    def test_the_vocabulary_is_reachable_from_the_worker_helper(self):
        """`record_task_failure` is the worker-side entry point; it must go
        through the same coercion rather than writing raw strings."""
        source = inspect.getsource(taskfailure.record)
        assert 'normalise_category' in source, (
            "taskfailure.record() no longer normalises the category, so an "
            "unknown value would reach the database verbatim"
        )
