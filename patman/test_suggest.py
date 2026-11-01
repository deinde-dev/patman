# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for the rules which suggest series to review"""

import re
import unittest
from unittest import mock

from u_boot_pylib import terminal

from patman import lorelist
from patman import patchwork
from patman.review import suggest

# Rules of each kind, as they might be in the settings
RULES = {
    'i2c': 'path drivers/i2c/** arch/arm/mach-bcm283x/**  +3',
    'listed': 'maintainer r +3',
    'reviewed': 'has Reviewed-by Acked-by  -3',
    'quiet': 'no-replies 3- +1',
    'bots': 'from *bot*@*  skip',
    'stable': 'not rfc +1',
    'udev': r'word "struct udevice \*" +1',
    'mine': 'delegate Me need',
    'fresh': 'newcomer +2',
    'dm': 'off',
    'limit': '8',
    'min_score': '2',
}


class TestSuggest(unittest.TestCase):
    """Tests for parsing and checking the rules"""

    def test_parse(self):
        """Each kind of rule is parsed, and kept in order of cost"""
        rules = suggest.get_rules(RULES)
        self.assertEqual(
            ['bots', 'stable', 'i2c', 'listed', 'mine', 'quiet', 'reviewed',
             'udev', 'fresh'],
            [rule.name for rule in rules.rules])
        by_name = {rule.name: rule for rule in rules.rules}
        self.assertEqual(
            suggest.Rule('i2c', 'path',
                         ('drivers/i2c/**', 'arch/arm/mach-bcm283x/**'),
                         False, 3),
            by_name['i2c'])
        self.assertEqual(('R',), by_name['listed'].args)
        self.assertEqual(('reviewed-by', 'acked-by'), by_name['reviewed'].args)
        self.assertEqual(-3, by_name['reviewed'].score)
        self.assertEqual((3, None), by_name['quiet'].args)
        self.assertEqual('skip', by_name['bots'].score)
        self.assertTrue(by_name['stable'].negate)
        self.assertEqual([r'struct udevice \*'],
                         [pat.pattern for pat in by_name['udev'].args])
        self.assertEqual(('me',), by_name['mine'].args)
        self.assertEqual('need', by_name['mine'].score)
        self.assertEqual((8, 2, 30),
                         (rules.limit, rules.min_score, rules.days))

        # Without settings, there are no rules, and the defaults
        self.assertEqual(suggest.Rules([], 10, 1, 30), suggest.get_rules({}))

        # With 'maintainer' and no roles, both match
        self.assertEqual(('M', 'R'),
                         suggest.parse_rule('x', 'maintainer +1').args)
        self.assertEqual((5, 5), suggest.parse_rule('x', 'patches 5 +1').args)
        self.assertEqual((None, 3),
                         suggest.parse_rule('x', 'version -3 +1').args)

    def test_errors(self):
        """A rule or setting which is not valid gives a clear error"""
        for value, msg in (
                ('pth x +1', "Suggest rule 'x' has an unknown test 'pth'; use "
                             'one of: age, blame,'),
                ('path +1', "Suggest rule 'x': 'path' needs at least one "
                            'argument'),
                ('path a', "Suggest rule 'x' ends with 'a', not a score: use "
                           'e.g. +3 or -2, or skip or need'),
                ('rfc', "Suggest rule 'x' needs a test and a score"),
                ('not', "Suggest rule 'x' needs a test and a score"),
                ('rfc a +1', "Suggest rule 'x': 'rfc' takes no arguments"),
                ('patches 3-1 +1', "Suggest rule 'x': 'patches' has a range "
                                   'which is back to front'),
                ('patches 3 4 +1', "Suggest rule 'x': 'patches' needs one "
                                   'number or range'),
                ('age - +1', "Suggest rule 'x': 'age' needs one number or "
                             'range'),
                ('maintainer X +1', "Suggest rule 'x': 'maintainer' takes "
                                    'only M and R'),
                ('word ( +1', "Suggest rule 'x': 'word' has a bad pattern"),
                ('from "a +1', "Suggest rule 'x': No closing quotation"),
                ('checks maybe +1', "Suggest rule 'x': 'checks' needs pass "
                                    'or fail'),
                ('delegate +1', "Suggest rule 'x': 'delegate' needs me or "
                                'none')):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as exc:
                    suggest.get_rules({'x': value})
                self.assertTrue(str(exc.exception).startswith(msg),
                                str(exc.exception))

        for name, value, msg in (
                ('limit', '0', "The suggest setting 'limit' needs a number "
                               "above 0, not '0'"),
                ('days', 'x', "The suggest setting 'days' needs a number "
                              "above 0, not 'x'"),
                ('min_score', '1.5', "The suggest setting 'min_score' needs "
                                     "a number, not '1.5'")):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as exc:
                    suggest.get_rules({name: value})
                self.assertEqual(msg, str(exc.exception))

        # A low score is fine, for rule sets which only subtract
        self.assertEqual(-2, suggest.get_rules({'min_score': '-2'}).min_score)

    def test_check(self):
        """Rules which can never match are reported"""
        rules = suggest.get_rules(RULES)
        self.assertEqual([], suggest.check_rules(rules, False))
        self.assertEqual(
            ["Suggest rule 'mine' uses 'delegate', which lore does not have, "
             'so it never matches'],
            suggest.check_rules(rules, True))
        rules = suggest.get_rules({'big': 'patches 20- -2'})
        self.assertEqual(['No suggest rule adds to the score, so no series '
                          'reaches min_score 1'],
                         suggest.check_rules(rules, False))
        rules = suggest.get_rules({'big': 'patches 20- -2', 'min_score': '0'})
        self.assertEqual([], suggest.check_rules(rules, False))

        # With no rules, show_rules() says so, so there is no warning
        self.assertEqual([], suggest.check_rules(suggest.get_rules({}), False))

    def test_show(self):
        """The rules are shown as understood, grouped by cost"""
        self.assertEqual(
            ['Rules for suggesting U-Boot series:',
             '  From the list of series:',
             "    bots      from '*bot*@*'  skip",
             '    stable    not rfc  +1',
             '  From each patch:',
             "    i2c       path 'drivers/i2c/**' "
             "'arch/arm/mach-bcm283x/**'  +3",
             '    listed    maintainer R  +3',
             '    mine      delegate me  need',
             '    quiet     no-replies 3-  +1',
             '    reviewed  has reviewed-by acked-by  -3',
             r"    udev      word 'struct udevice \*'  +1",
             '  With extra requests or git:',
             '    fresh     newcomer  +2',
             'Suggest at most 8, with a score of at least 2, sent in the last '
             '30 days'],
            suggest.show_rules(suggest.get_rules(RULES), 'U-Boot'))
        self.assertEqual(
            ['No rules for suggesting dtc series: add them to the [suggest] '
             'section of the settings'],
            suggest.show_rules(suggest.get_rules({}), 'dtc'))

        # A shown rule reads back as the same rule
        for rule in suggest.get_rules(RULES).rules:
            line = suggest.show_rules(
                suggest.Rules([rule], 10, 1, 30), 'x')[2]
            name, value = re.match(r'\s+(\S+)\s+(.*)$', line).groups()
            with self.subTest(name=name):
                again = suggest.parse_rule(name, value)
                self.assertEqual(rule.test, again.test)
                self.assertEqual(
                    [getattr(arg, 'pattern', arg) for arg in rule.args],
                    [getattr(arg, 'pattern', arg) for arg in again.args])
                self.assertEqual((rule.negate, rule.score),
                                 (again.negate, again.score))

    def test_do_rules(self):
        """--suggest-rules shows the rules, and fails if any cannot match"""
        pwork = patchwork.Patchwork.for_testing(None)
        with mock.patch.dict(suggest.settings.suggest, RULES, clear=True), \
                terminal.capture() as (out, err):
            self.assertEqual(0, suggest.do_rules(pwork))
        self.assertIn('    i2c       path', out.getvalue())
        self.assertEqual('', err.getvalue())

        with mock.patch.dict(suggest.settings.suggest, RULES, clear=True), \
                terminal.capture() as (out, err):
            self.assertEqual(1, suggest.do_rules(lorelist.LoreList('https://lore.kernel.org/dtc')))
        self.assertIn("'mine' uses 'delegate'", err.getvalue())

        with mock.patch.dict(suggest.settings.suggest, {}, clear=True), \
                terminal.capture() as (out, err):
            self.assertEqual(1, suggest.do_rules(pwork))
        self.assertIn('No rules for suggesting', out.getvalue())
        self.assertEqual('', err.getvalue())
