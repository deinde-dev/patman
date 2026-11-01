# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for the rules which suggest series to review"""

from datetime import datetime, timedelta, timezone
import re
from unittest import mock

from u_boot_pylib import terminal

from patman import lorelist
from patman import patchwork
from patman.review import common
from patman.review import scan
from patman.review import suggest
from patman.test_common import CseriesTester, Namespace

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


# The time the tests are run at
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

ME = 'me@x.org'


# pylint: disable-next=R0913
def make_series(sid, name, version=1, count=2, email='ann@x.org',
                who='Ann Acker', days=1, tag='PATCH'):
    """Make a series as patchwork gives it

    Args:
        sid (int): Series ID
        name (str): Name, without tags
        version (int): Version
        count (int): Number of patches
        email (str): Submitter's address
        who (str): Submitter's name
        days (int): Days before NOW that it was sent
        tag (str): Tag of its patches, e.g. 'PATCH' or 'RFC'

    Returns:
        dict: The series
    """
    ver = f',v{version}' if version > 1 else ''
    date = (NOW - timedelta(days=days, hours=1)).isoformat()
    return {
        'id': sid, 'name': name, 'version': version, 'total': count,
        'date': date, 'submitter': {'name': who, 'email': email},
        'cover_letter': {'name': f'[{tag}{ver},0/{count}] {name}'},
        'patches': [{'id': sid * 10 + num,
                     'name': f'[{tag}{ver},{num}/{count}] {name} {num}'}
                    for num in range(1, count + 1)]}


class TestSuggest(CseriesTester):
    """Tests for the rules which suggest series to review"""

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

    def test_series_info(self):
        """What a series' list entry says is found for the tests"""
        pws = make_series(1, 'dm: core: Add a thing', version=3, count=2,
                          tag='RFC', days=4)
        info = suggest.series_info(pws, NOW, True, ['new'])
        self.assertEqual(['dm: core: Add a thing 1', 'dm: core: Add a thing 2'],
                         info.subjects)
        self.assertEqual(('ann@x.org', 'ann acker', 2, 3, 4, True, True,
                          ['new']),
                         info[1:])
        self.assertFalse(suggest.series_info(make_series(2, 'x'), NOW, False,
                                             []).rfc)

        # Lore's dates have no time zone, and are in UTC; a series with no
        # patches is known by its name
        pws = {'id': 'a@x', 'name': '[PATCH] boot: Fix it',
               'date': '2026-10-29T11:00:00Z', 'submitter': {}}
        info = suggest.series_info(pws, NOW, False, [])
        self.assertEqual(['boot: Fix it'], info.subjects)
        self.assertEqual(3, info.age)
        self.assertEqual(0, suggest.series_info({'id': 1}, NOW, False, []).age)

    def test_free_tests(self):
        """Each free test matches as it should"""
        info = suggest.series_info(
            make_series(1, 'boot: Fix the order', version=2, count=4,
                        email='Bob@Chromium.org', who='Bob Builder', days=5),
            NOW, False, ['new', 'superseded'])
        for value, matched in (
                ('prefix boot:', True), ('prefix BOOT:', True),
                ('prefix dm: bootstd:', False), ('prefix boot', True),
                ('from *@chromium.org', True), ('from bob*', True),
                ("from 'bob builder'", True), ('from ann@x.org', False),
                ('patches 4', True), ('patches 5-', False), ('patches -4', True),
                ('version 2-3', True), ('version 3-', False),
                ('age 5', True), ('age -4', False), ('age 3-', True),
                ('rfc', False), ('not rfc', True),
                ('reviewed-before', False),
                ('state new', True), ('state accepted', False)):
            with self.subTest(value=value):
                rule = suggest.parse_rule('r', f'{value} +1')
                self.assertEqual(
                    (1 if matched else 0, ['r +1'] if matched else []),
                    suggest.score_series([rule], info))

        # Every free test has code to run it
        self.assertEqual(
            sorted(name for name, test in suggest.TESTS.items()
                   if test.cost == suggest.FREE),
            sorted(suggest.FREE_TESTS))

    def test_score(self):
        """Scores add up, 'skip' leaves a series out and 'need' keeps it in"""
        rules = suggest.get_rules({
            'boot': 'prefix boot: +3', 'small': 'patches -2 +1',
            'old': 'age 20- -2', 'rfc': 'rfc skip',
            'only-boot': 'prefix boot: bootstd: need'}).rules
        def score(**kwargs):
            pws = make_series(1, kwargs.pop('name', 'boot: Fix it'), **kwargs)
            return suggest.score_series(
                rules, suggest.series_info(pws, NOW, False, []))

        self.assertEqual((4, ['boot +3', 'small +1']), score())
        self.assertEqual((1, ['boot +3', 'old -2']), score(count=3, days=25))
        self.assertIsNone(score(tag='RFC'))
        self.assertIsNone(score(name='dm: Fix it'))
        self.assertEqual((1, ['small +1']), score(name='bootstd: Fix it'))

    def add_review(self, name, version, link, reviewed=True):
        """Add a series which has been reviewed

        Args:
            name (str): Name of the series
            version (int): Version reviewed
            link (str): Its link
            reviewed (bool): True to add a review of its first patch
        """
        db = self.cser.db
        idnum = (db.series_find_review_by_name(name) or [None])[0]
        if not idnum:
            idnum = db.series_add(f'r{link}', name)
            db.series_set_source(idnum, 'review')
        svid = db.ser_ver_add(idnum, version, link=link)
        if reviewed:
            db.review_add(svid, 1, 'comment', False, 't')
        self.cser.commit()

    def test_find(self):
        """Series are found, scored and put in order, leaving out those which
        are yours, reviewed already, or not the newest version"""
        cser = self.get_database()
        self.add_review('x86: Handle relocations', 1, '200')
        self.add_review('dm: Reviewed already', 1, '300')
        self.add_review('dm: Started only', 1, '310', reviewed=False)
        series = [
            make_series(100, 'boot: Newest', days=1),
            make_series(110, 'boot: Mine', email=ME, days=1),
            make_series(201, 'x86: Handle relocations', version=2, days=2),
            make_series(300, 'dm: Reviewed already', days=3),
            make_series(310, 'dm: Started only', days=3),
            make_series(400, 'boot: Older', days=4),
            make_series(500, 'video: Not mine', days=4),
            make_series(601, 'boot: Twice', version=2, days=2),
            make_series(600, 'boot: Twice', version=1, days=8)]
        rules = suggest.get_rules({
            'boot': 'prefix boot: +2', 'dm': 'prefix dm: +2',
            'again': 'reviewed-before +3'})
        pwork = patchwork.Patchwork.for_testing(None)
        found = suggest.find_suggestions(rules, series, pwork, cser, {ME},
                                         cser.db, NOW)
        self.assertEqual(
            [(201, 3, ['again +3']), (100, 2, ['boot +2']),
             (601, 2, ['boot +2']), (310, 2, ['dm +2']),
             (400, 2, ['boot +2'])],
            [(sug.pws['id'], sug.score, sug.reasons) for sug in found])

        # A higher min_score leaves out the lower scores
        rules = rules._replace(min_score=3)
        self.assertEqual([201], [sug.pws['id'] for sug in
                                 suggest.find_suggestions(
                                     rules, series, pwork, cser, {ME},
                                     cser.db, NOW)])

    def test_show_suggestions(self):
        """The suggestions are shown with their links and reasons"""
        found = [suggest.Suggestion(make_series(1, 'boot: Fix it', version=2,
                                                count=1), 5,
                                    ['boot +2', 'again +3']),
                 suggest.Suggestion(make_series(2, 'dm: Add it', count=3), 2,
                                    ['dm +2']),
                 suggest.Suggestion(make_series(3, 'dm: More'), 2, ['dm +2'])]
        urls = {'1': 'https://pw/1', '2': 'https://pw/2', '3': 'https://pw/3'}
        self.assertEqual(
            ['  5  boot: Fix it (v2, 1 patch)',
             '       https://pw/1',
             '       boot +2, again +3',
             '  2  dm: Add it (v1, 3 patches)',
             '       https://pw/2',
             '       dm +2',
             '2 of 40 recent series suggested; 1 more, which --limit can '
             'show'],
            suggest.show_suggestions(found, 40, 2, urls))
        self.assertEqual(['0 of 40 recent series suggested'],
                         suggest.show_suggestions([], 40, 2, urls))

    def test_do_suggest(self):
        """--suggest reads the recent series and shows the suggestions"""
        cser = self.get_database()
        pwork = patchwork.Patchwork.for_testing(None)
        pwork.project_set(1, 'uboot')
        series = [make_series(100, 'boot: Fix it'),
                  make_series(101, 'video: Other')]
        recent = mock.AsyncMock(return_value=series)
        values = {'boot': 'prefix boot: +2', 'i2c': 'path drivers/i2c/** +3'}
        with mock.patch.dict(suggest.settings.suggest, values, clear=True), \
                mock.patch.object(scan, 'recent_series', recent), \
                terminal.capture() as (out, err):
            self.assertEqual(0, suggest.do_suggest(Namespace(limit=None),
                                                   pwork, cser, {ME}, cser.db))
        self.assertEqual(
            ['  2  boot: Fix it (v1, 2 patches)',
             '       https://my-fake-url/project/uboot/list/?series=100'
             '&state=*&archive=both',
             '       boot +2',
             '1 of 2 recent series suggested'],
            out.getvalue().splitlines())
        self.assertIn("Suggest rule 'i2c' uses 'path', which --suggest does "
                      'not run yet, so it is left out', err.getvalue())
        since = recent.call_args.args[2]
        self.assertEqual(
            {'partial': True, 'max_age': suggest.SUGGEST_MAX_AGE,
             'consistent': True}, recent.call_args.kwargs)
        self.assertEqual(
            (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
                '%Y-%m-%d'), since)

        # Without rules, it says so
        with mock.patch.dict(suggest.settings.suggest, {}, clear=True), \
                terminal.capture() as (out, err):
            self.assertEqual(1, suggest.do_suggest(Namespace(limit=None),
                                                   pwork, cser, {ME}, cser.db))
        self.assertIn('No rules for suggesting', err.getvalue())

    def test_recent_series(self):
        """Recent series are read page by page until they are too old"""
        def page_of(days, count):
            return [make_series(num, f's{num}', days=days)
                    for num in range(count)]

        since = (NOW - timedelta(days=10)).strftime('%Y-%m-%d')
        pwork = patchwork.Patchwork.for_testing(None)
        pages = [page_of(1, scan.SCAN_PAGE), page_of(9, scan.SCAN_PAGE),
                 page_of(20, scan.SCAN_PAGE)]
        get = mock.AsyncMock(side_effect=pages)
        with mock.patch.object(pwork, 'get_recent_series', get):
            got = common.run(scan.recent_series(None, pwork, since))
        self.assertEqual(3, get.call_count)
        self.assertEqual(2 * scan.SCAN_PAGE, len(got))

        # A page after the first which cannot be read ends the list, if a
        # partial list will do
        fail = ValueError("Could not read URL 'x' (status 502)")
        get = mock.AsyncMock(side_effect=[pages[0], fail])
        with mock.patch.object(pwork, 'get_recent_series', get), \
                terminal.capture() as (_, err):
            got = common.run(scan.recent_series(None, pwork, since,
                                                partial=True))
        self.assertEqual(scan.SCAN_PAGE, len(got))
        self.assertEqual(
            f"Could not read URL 'x' (status 502); using the {scan.SCAN_PAGE} "
            f"series sent since {pages[0][-1]['date'][:10]}, from the pages "
            'before it', err.getvalue().strip())
        for side_effect, partial in (([pages[0], fail], False),
                                     ([fail], True)):
            get = mock.AsyncMock(side_effect=side_effect)
            with mock.patch.object(pwork, 'get_recent_series', get), \
                    self.assertRaises(ValueError):
                common.run(scan.recent_series(None, pwork, since,
                                              partial=partial))

        # With 'consistent', a later page is used from the cache only if it
        # was read at or after the first page, here 20 minutes ago
        get = mock.AsyncMock(side_effect=pages)
        with mock.patch.object(pwork, 'get_recent_series', get), \
                mock.patch.object(pwork, 'read_age', return_value=1200):
            common.run(scan.recent_series(None, pwork, since, max_age=3600,
                                          consistent=True))
        self.assertEqual([3600, 1200, 1200],
                         [call.kwargs['max_age'] for call in get.call_args_list])

        # Without it, or with no copy of the first page, the ages are as given
        for consistent, age, ages in ((False, 1200, [600, 600, 600]),
                                      (True, None, [600, 0, 0])):
            get = mock.AsyncMock(side_effect=pages)
            with mock.patch.object(pwork, 'get_recent_series', get), \
                    mock.patch.object(pwork, 'read_age', return_value=age):
                common.run(scan.recent_series(None, pwork, since,
                                              consistent=consistent))
            self.assertEqual(ages, [call.kwargs['max_age']
                                    for call in get.call_args_list])

        # A short page from patchwork is the last
        get = mock.AsyncMock(side_effect=[page_of(1, 3)])
        with mock.patch.object(pwork, 'get_recent_series', get):
            self.assertEqual(3, len(common.run(
                scan.recent_series(None, pwork, since))))

        # A page of lore's feed has as many series as its messages make, so
        # only an empty page is the last
        llist = lorelist.LoreList('https://lore.kernel.org/dtc')
        get = mock.AsyncMock(side_effect=[page_of(1, 3), page_of(2, 2), []])
        with mock.patch.object(llist, 'get_recent_series', get):
            self.assertEqual(5, len(common.run(
                scan.recent_series(None, llist, since))))
        self.assertEqual(3, get.call_count)
