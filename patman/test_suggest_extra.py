# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for the suggest rules which need git or extra requests, and for
--explain"""

import os
import subprocess
from unittest import mock

import aiohttp

from u_boot_pylib import terminal

from patman import lorelist
from patman import patchwork
from patman.review import common
from patman.review import explain
from patman.review import history
from patman.review import suggest
from patman.review import suggest_rules
from patman.review import suggest_tests
from patman.test_common import Namespace
from patman.test_lore import feed_entry
from patman.test_suggest import (ME, NOW, PATCH, SuggestHelper, make_mbox,
                                 make_series)


class TestSuggestExtra(SuggestHelper):
    """Test the suggest rules which need git or extra requests"""
    def history_repo(self):
        """Make a repo with a file which you and someone else wrote

        Returns:
            tuple:
                str: Top of the repo
                str: Your commit, which adds the first two lines of a.c
                str: The other commit, by ann@x.org, which adds the third
        """
        repo = os.path.join(self.tmpdir, 'hrepo')
        os.makedirs(repo)

        def git(*args, who=ME):
            env = dict(os.environ, GIT_AUTHOR_NAME='X', GIT_AUTHOR_EMAIL=who,
                       GIT_COMMITTER_NAME='X', GIT_COMMITTER_EMAIL=who)
            return subprocess.run(
                ['git', '-c', 'commit.gpgsign=false', *args], cwd=repo,
                env=env, check=True, capture_output=True,
                text=True).stdout.strip()

        def commit(text, who):
            with open(os.path.join(repo, 'a.c'), 'a', encoding='utf-8') as out:
                out.write(text)
            git('add', 'a.c')
            git('commit', '-q', '-m', 'Change it', who=who)
            return git('rev-parse', 'HEAD')

        git('init', '-q')
        mine = commit('one\ntwo\n', ME)
        return repo, mine, commit('three\n', 'ann@x.org')

    def test_history(self):
        """The history of the tree says who wrote which commits and lines"""
        repo, mine, other = self.history_repo()
        self.assertTrue(history.ref_exists(repo, mine[:12]))
        self.assertFalse(history.ref_exists(repo, '0123456789ab'))
        self.assertEqual([mine[:12]], history.own_commits(
            repo, [mine[:12], other[:12], '0123456789ab'], {ME}))

        # Line 2 is yours and line 3 is not; a file which is not there is
        # passed over
        self.assertEqual({'a.c': 1}, history.blame_mine(
            repo, 'HEAD', {'a.c': [2, 3], 'gone.c': [1]}, {ME}))
        self.assertEqual({}, history.blame_mine(repo, 'HEAD', {'a.c': [3]},
                                                {ME}))
        self.assertEqual({ME, 'ann@x.org'}, history.authors(repo, 'HEAD'))

        # An old commit may have an author which is not UTF-8, which is
        # replaced. Git now converts it, so write the commit as old tools did
        tree = subprocess.run(['git', 'rev-parse', 'HEAD^{tree}'], cwd=repo,
                              check=True, capture_output=True).stdout.strip()
        who = b'M <m\xfc@x.org> 1700000000 +0000'
        commit = subprocess.run(
            ['git', 'hash-object', '-t', 'commit', '-w', '--stdin'],
            cwd=repo, check=True, capture_output=True,
            input=b'tree %s\nparent %s\nauthor %s\ncommitter %s\n\nOld\n' % (
                tree, other.encode(), who, who)).stdout.strip()
        self.assertIn('m\ufffd@x.org', history.authors(repo, commit.decode()))

    def test_extra(self):
        """The tests which need git or extra requests find what they should"""
        repo, mine, _ = self.history_repo()

        # The patch fixes your commit and changes line 2 of a.c, which you
        # wrote
        patch = make_mbox(PATCH.replace('0123456789ab', mine[:12]).replace(
            'drivers/i2c/x.c', 'a.c'))
        series = [make_series(1, 'i2c: New', email='new@x.org'),
                  make_series(2, 'i2c: Again', email='ann@x.org', days=2),
                  make_series(3, 'i2c: Another', email='bob@x.org', days=3),
                  make_series(4, 'dm: Old', email='bob@x.org', days=9)]
        mboxes = {str(num): patch for num in range(1, 5)}
        values = {'fix': 'fixes +1', 'mine': 'blame +2',
                  'fresh': 'newcomer +4'}

        # Patchwork has only an earlier version of the first series
        earlier = mock.AsyncMock(return_value=[{'name': '[PATCH] i2c: New'}])
        _, found, _ = self.suggester(values, series, mboxes, repo=repo,
                                     earlier=earlier)
        self.assertEqual(
            [(1, 7, ['fix +1', 'fresh +4', 'mine +2']),
             (2, 3, ['fix +1', 'mine +2']), (3, 3, ['fix +1', 'mine +2']),
             (4, 3, ['fix +1', 'mine +2'])],
            [(sug.pws['id'], sug.score, sug.reasons) for sug in found])

        # Ann has commits in the tree and Bob sent an earlier series, which
        # is among the recent ones for the third, so patchwork is asked only
        # about the first and last
        self.assertEqual(['new@x.org', 'bob@x.org'],
                         [call.args[2] for call in earlier.call_args_list])

        # The extra tests are not run on a series which cannot reach
        # min_score
        values = {'fresh': 'newcomer +4', 'dm': 'prefix dm: +1',
                  'min_score': '5'}
        earlier = mock.AsyncMock(return_value=[{'name': 'Something else'}])
        _, found, _ = self.suggester(values, series, mboxes, repo=repo,
                                     earlier=earlier)
        self.assertEqual([], found)
        self.assertEqual(['bob@x.org'],
                         [call.args[2] for call in earlier.call_args_list])

        # A series which cannot be checked is left out, with a warning
        earlier = mock.AsyncMock(
            side_effect=aiohttp.ServerDisconnectedError())
        with terminal.capture() as (_, err):
            _, found, _ = self.suggester(values, series, mboxes, repo=repo,
                                         earlier=earlier)
        self.assertEqual([], found)
        self.assertIn("Cannot check 'dm: Old': Server disconnected",
                      err.getvalue())

    def test_earlier_series(self):
        """Patchwork and lore are asked for the series someone sent before"""
        paths = []

        def fake(subpath):
            paths.append(subpath)
            return [{'id': 5}]

        pwork = patchwork.Patchwork.for_testing(fake)
        self.assertEqual([{'id': 5}], common.run(pwork.earlier_series(
            None, 'uboot', 'ann@x.org', '2026-10-01T10:00:00')))
        self.assertEqual(
            ['series/?project=uboot&submitter=ann%40x.org'
             '&before=2026-10-01T10%3A00%3A00&order=-date&per_page=10'],
            paths)
        self.assertEqual('submitter-series', patchwork.request_kind(paths[0]))

        # Lore's search takes the day too, so the series sent that day are
        # left out
        llist = lorelist.LoreList('https://lore.kernel.org/my-list')
        feed = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/'
                'Atom">' + feed_entry('old@x', '[PATCH] dm: Old',
                                      '2026-09-20T10:00:00Z') +
                feed_entry('new@x', '[PATCH] dm: New',
                           '2026-10-01T09:00:00Z') + '</feed>')
        read = mock.AsyncMock(return_value=feed.encode('utf-8'))
        with mock.patch.object(llist.lore, 'read', read):
            found = common.run(llist.earlier_series(
                None, 'dtc', 'ann@x.org', '2026-10-01T10:00:00'))
        self.assertEqual(['dm: Old'], [ser['name'] for ser in found])
        self.assertEqual(
            '?q=f%3Aann%40x.org+AND+s%3APATCH+AND+NOT+s%3ARe+AND+d%3A..'
            '20261001&x=A', read.call_args.args[1])

        # Lore says that nothing matches with a 404
        with mock.patch.object(llist.lore, 'read',
                               mock.AsyncMock(return_value=False)):
            self.assertEqual([], common.run(llist.earlier_series(
                None, 'dtc', 'ann@x.org', '2026-10-01T10:00:00')))

    def explainer(self, values, repo):
        """Make a suggester for explaining series, with made-up patches

        Args:
            values (dict): Settings, as in [suggest]
            repo (str): Repo for the tests which need the history

        Returns:
            tuple:
                Suggester: The suggester
                Patchwork: Where the series come from, with get_series_mbox(),
                    earlier_series() and the comments mocked
        """
        cser = self.cser or self.get_database()
        pwork = patchwork.Patchwork.for_testing(None)
        pwork.project_set(1, 'uboot')
        pwork.get_series_mbox = mock.AsyncMock(return_value=make_mbox(PATCH))
        pwork.earlier_series = mock.AsyncMock(return_value=[])
        pwork.get_cover_comments = mock.AsyncMock(return_value=[])
        pwork.get_patch_comments = mock.AsyncMock(return_value=[])
        sug = suggest.Suggester(suggest_rules.get_rules(values), pwork, cser,
                                {ME}, cser.db, NOW, 10, 'HEAD')
        sug._top = repo  # pylint: disable=W0212
        return sug, pwork

    def test_replies_delegate_checks(self):
        """no-replies, delegate and checks use the comments and patch list"""
        series = [make_series(1, 'a: One', days=5),
                  make_series(2, 'b: Two', days=5),
                  make_series(3, 'c: Three', days=1)]
        series[0]['cover_letter']['id'] = 100
        ann = {'name': 'Ann Acker', 'email': 'ann@x.org'}
        tom = {'name': 'Tom Rini', 'email': 'trini@x.org'}
        comments = {11: [{'submitter': ann}], 12: [], 21: [{'submitter': tom}],
                    22: [{'submitter': tom}]}
        lists = {
            '1': [{'delegate': {'username': 'me', 'email': ME},
                   'check': 'success'},
                  {'delegate': None, 'check': 'success'}],
            '2': [{'delegate': None, 'check': 'fail'},
                  {'delegate': None, 'check': 'success'}],
            '3': [{'delegate': None, 'check': None}]}
        values = {'quiet': 'no-replies 3- +2', 'mine': 'delegate me +3',
                  'nobody': 'delegate none +1', 'ci': 'checks fail -1'}
        cser = self.get_database()
        pwork = patchwork.Patchwork.for_testing(None)
        pwork.get_cover_comments = mock.AsyncMock(return_value=[])
        pwork.get_patch_comments = mock.AsyncMock(
            side_effect=lambda _client, pid: comments[pid])
        pwork.get_series_patches = mock.AsyncMock(
            side_effect=lambda _client, link: lists[link])
        sug = suggest.Suggester(suggest_rules.get_rules(values), pwork, cser,
                                {ME}, cser.db, NOW, 10, 'HEAD')
        found = sug.find(series)

        # Only the submitter replied to the first, which is delegated to
        # you; Tom replied to the second, whose CI failed; the third is too
        # new for 'quiet' and has no delegate
        self.assertEqual(
            [(1, 5, ['mine +3', 'quiet +2']), (3, 1, ['nobody +1'])],
            [(sug.pws['id'], sug.score, sug.reasons) for sug in found])

        # The replies are read until one is found, and not at all for a
        # series too new for the rule
        pwork.get_cover_comments.assert_called_once_with(mock.ANY, 100)
        reads = pwork.get_patch_comments.call_args_list
        self.assertEqual([11, 12, 21], [call.args[1] for call in reads])

        extra = common.run(sug.extra(None, series[1], None))
        self.assertEqual(
            [suggest_tests.Result(False, 'reply from Tom Rini'),
             suggest_tests.Result(False, 'none'),
             suggest_tests.Result(True, '1 fail, 1 success'),
             suggest_tests.Result(False, '1 fail, 1 success')],
            [suggest_tests.EXTRA_TESTS['no-replies']((3, None), extra),
             suggest_tests.EXTRA_TESTS['delegate'](('me',), extra),
             suggest_tests.EXTRA_TESTS['checks'](('fail',), extra),
             suggest_tests.EXTRA_TESTS['checks'](('pass',), extra)])
        extra = common.run(sug.extra(None, series[0], None))
        self.assertEqual(
            [suggest_tests.Result(True, 'no replies, 5 days'),
             suggest_tests.Result(True, 'me'),
             suggest_tests.Result(True, '2 success')],
            [suggest_tests.EXTRA_TESTS['no-replies']((3, None), extra),
             suggest_tests.EXTRA_TESTS['delegate'](('me',), extra),
             suggest_tests.EXTRA_TESTS['checks'](('pass',), extra)])
        self.assertEqual(
            suggest_tests.Result(False, '3 days'),
            suggest_tests.EXTRA_TESTS['no-replies']((5, None), extra._replace(
                age=3, replier=None)))

        # On lore there are no delegates or CI results
        lore = suggest_tests.Extra({}, False, '')
        self.assertEqual(
            [suggest_tests.Result(False, 'not known')] * 2,
            [suggest_tests.EXTRA_TESTS['delegate'](('none',), lore),
             suggest_tests.EXTRA_TESTS['checks'](('pass',), lore)])
        sug.pwork = mock.Mock(spec=['get_patch_comments'])
        found = common.run(
            sug._patch_list(None, series[0]))  # pylint: disable=W0212
        self.assertEqual((None, False, None), found)

    def test_explain(self):
        """--explain shows what each rule finds, even those not reached"""
        repo, _, _ = self.history_repo()
        values = {'i2c': 'path drivers/i2c/** +3', 'bots': 'from *bot*@* skip',
                  'fresh': 'newcomer +2', 'quiet': 'no-replies 3- +1',
                  'old': 'age 5- -1', 'spi': 'not word spi +1'}
        sug, _ = self.explainer(values, repo)
        self.assertEqual([
            'i2c: New (v1, 2 patches)',
            '  https://pw/1',
            '  Rule   Test                   Match  Score  Seen',
            "  bots   from '*bot*@*'         no     skip   new@x.org",
            '  old    age 5-                 no     -1     1 day',
            "  i2c    path 'drivers/i2c/**'  yes    +3     drivers/i2c/x.c",
            '  spi    not word spi           yes    +1     5 lines',
            '  fresh  newcomer               yes    +2     no earlier series',
            '  quiet  no-replies 3-          no     +1     1 day',
            'Score 6: suggested if among the best 10 recent series'],
            explain.explain(sug, make_series(1, 'i2c: New', email='new@x.org'),
                            'https://pw/1'))

        # A 'skip' rule which matches leaves the series out, as does one with
        # too low a score, and the rest are still shown
        lines = explain.explain(
            sug, make_series(1, 'i2c: New', email='bot@x.org', days=7),
            'https://pw/1')
        self.assertIn("  bots   from '*bot*@*'         yes    skip   bot@x.org",
                      lines)
        self.assertEqual("Not suggested: rule 'bots' skips it", lines[-1])
        sug, _ = self.explainer({'old': 'age 5- -1', 'i2c': 'path x +1'},
                                repo)
        self.assertEqual(
            'Not suggested: score -1 is below min_score 1',
            explain.explain(sug, make_series(1, 'i2c: New', days=7),
                            'https://pw/1')[-1])

        # Only the prefixes of the subjects are shown, each once
        info = suggest_tests.series_info(make_series(1, 'i2c: x: New'),
                                         NOW, False, [])
        self.assertEqual(suggest_tests.Result(True, 'i2c: x:'),
                         suggest_tests.FREE_TESTS['prefix'](('i2c:',), info))
        self.assertEqual(suggest_tests.Result(False, 'i2c: x:'),
                         suggest_tests.FREE_TESTS['prefix'](('dm:',), info))

        # A series which cannot be read shows what is known
        sug.pwork.get_series_mbox.side_effect = ValueError('No mbox')
        lines = explain.explain(sug, make_series(1, 'i2c: New', days=7),
                                'https://pw/1')
        self.assertIn('  i2c   path x  ?      +1     not known', lines)
        self.assertIn('Cannot read the patches: No mbox', lines)

    def test_do_explain(self):
        """--explain reads the series and shows how the rules score it"""
        cser = self.get_database()
        repo, _, _ = self.history_repo()
        pws = make_series(100, 'boot: Fix it')
        values = {'boot': 'prefix boot: +2'}
        _, pwork = self.explainer(values, repo)
        pwork.get_series = mock.AsyncMock(return_value=pws)
        with mock.patch.dict(suggest.settings.suggest, values, clear=True), \
                terminal.capture() as (out, _):
            self.assertEqual(0, explain.do_explain(
                Namespace(limit=None), pwork, cser, {ME}, cser.db, '100'))
        lines = out.getvalue().splitlines()
        self.assertEqual(['boot: Fix it (v1, 2 patches)',
                          '  https://my-fake-url/project/uboot/list/'
                          '?series=100&state=*&archive=both'], lines[:2])
        self.assertEqual('Score 2: suggested if among the best 10 recent '
                         'series', lines[-1])
        self.assertEqual('100', pwork.get_series.call_args.args[1])

        # Your own series is not suggested
        with mock.patch.dict(suggest.settings.suggest, values, clear=True), \
                terminal.capture() as (out, _):
            explain.do_explain(Namespace(limit=None), pwork, cser,
                               {'ann@x.org'}, cser.db, '100')
        self.assertEqual('Not suggested: it is your own series',
                         out.getvalue().splitlines()[-1])

        # Without rules, it says so
        with mock.patch.dict(suggest.settings.suggest, {}, clear=True), \
                terminal.capture() as (_, err):
            self.assertEqual(1, explain.do_explain(
                Namespace(limit=None), pwork, cser, {ME}, cser.db, '100'))
        self.assertIn('No rules for suggesting', err.getvalue())
