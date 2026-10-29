# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for checking the commits which a maintainer is about to push"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from u_boot_pylib import command
from u_boot_pylib import terminal

from patman import maint
from patman.test_common import Namespace

ME = 'Simon Glass <sjg@chromium.org>'
FRED = 'Fred Bloggs <fred@x.org>'

# The rules which dtc uses
DTC = {
    'upstream': 'git.kernel.org/pub/scm/utils/dtc/dtc.git main',
    'signoff': ME,
    'history': 'prefer-linear',
    'trailers_end': 'message-id signoff',
    'pr_trailers_end': 'link signoff',
    'signoff_own': 'no',
    'require': 'author-signoff',
    'issue_trailers': 'Closes Link',
    'github': 'dgibson/dtc',
    'gitlab': 'dgibson/dtc',
}


class TestMaint(unittest.TestCase):
    """Test checking the queue against a project's rules"""
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='patman.')
        self.repo = os.path.join(self.tmpdir, 'repo')
        self.git('init', '-q', '-b', 'main', self.repo, cwd=self.tmpdir)
        self.git('remote', 'add', 'us',
                 'git://git.kernel.org/pub/scm/utils/dtc/dtc.git')
        self.git('remote', 'add', 'david', 'git@github.com:dgibson/dtc.git')
        self.commit('Initial commit', FRED, [])
        self.git('update-ref', 'refs/remotes/us/main', 'HEAD')

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def git(self, *args, cwd=None):
        """Run git in the test's repo

        Args:
            args (list of str): Arguments
            cwd (str or None): Directory to run in, or None for the repo

        Return:
            str: Output
        """
        return command.output('git', '-c', 'user.name=Simon Glass', '-c',
                              'user.email=sjg@chromium.org', *args,
                              cwd=cwd or self.repo)

    def commit(self, subject, author, trailers):
        """Add a commit to the main branch

        Args:
            subject (str): Subject
            author (str): Author
            trailers (list of str): Trailers, e.g. 'Signed-off-by: Fred'

        Return:
            str: Hash of the commit
        """
        msg = f'{subject}\n\nDo it\n'
        if trailers:
            msg += '\n' + '\n'.join(trailers) + '\n'
        self.git('commit', '-q', '--allow-empty', f'--author={author}', '-m',
                 msg)
        return self.git('rev-parse', 'HEAD').strip()

    def rules(self, **kwargs):
        """Get the dtc rules for the test's repo, changed as given"""
        return maint.get_rules({**DTC, **kwargs}, self.repo)

    def check(self, **kwargs):
        """Check the queue, giving the problems of each commit by subject"""
        rules = self.rules()
        commits = maint.read_commits(rules.upstream, rules.branch, self.repo)
        return {cmt.subject: problems for cmt, problems in
                maint.check_commits(rules, commits, **kwargs)}

    def test_norm_url(self):
        """The URLs of a repo are matched however they are written"""
        for url in ('git://git.kernel.org/pub/scm/utils/dtc/dtc.git',
                    'https://git.kernel.org/pub/scm/utils/dtc/dtc/',
                    'git@git.kernel.org:pub/scm/utils/dtc/dtc.git',
                    'git.kernel.org/pub/scm/utils/dtc/dtc.git'):
            self.assertEqual('git.kernel.org/pub/scm/utils/dtc/dtc',
                             maint.norm_url(url), url)
        self.assertEqual('us', maint.find_remote('us', self.repo))
        self.assertEqual('us', maint.find_remote(
            'git.kernel.org/pub/scm/utils/dtc/dtc.git', self.repo))
        self.assertEqual('david',
                         maint.find_remote('github.com/dgibson/dtc', self.repo))
        self.assertIsNone(maint.find_remote('gitlab.com/dgibson/dtc',
                                            self.repo))

    def test_rules(self):
        """The rules are read from the settings, with defaults"""
        rules = self.rules()
        self.assertEqual(
            ('us/main', 'main', ME, 'prefer-linear', ['message-id', 'signoff'],
             ['link', 'signoff'], False, ['author-signoff'],
             ['Closes', 'Link'],
             ['github.com/dgibson/dtc', 'gitlab.com/dgibson/dtc']),
            (rules.upstream, rules.branch, rules.signoff, rules.history,
             rules.trailers_end, rules.pr_trailers_end, rules.signoff_own,
             rules.require, rules.issue_trailers, rules.repos))

        # The defaults, with the sign-off from git
        with mock.patch.object(maint.gitutil, 'get_default_user_name',
                               return_value='Fred Bloggs'), \
                mock.patch.object(maint.gitutil, 'get_default_user_email',
                                  return_value='fred@x.org'):
            rules = maint.get_rules({'upstream': 'us main'}, self.repo,
                                    branch='queue')
            self.assertEqual('main',
                             maint.get_rules({'upstream': 'us main'},
                                             self.repo).branch)
        self.assertEqual(
            ('us/main', 'queue', FRED, 'linear', ['signoff'], ['signoff'],
             True, [], ['Closes', 'Link'], []),
            (rules.upstream, rules.branch, rules.signoff, rules.history,
             rules.trailers_end, rules.pr_trailers_end, rules.signoff_own,
             rules.require, rules.issue_trailers, rules.repos))

        for values, msg in (
                ({}, "No upstream to check against: set 'upstream'"),
                ({'upstream': 'us'}, "needs a remote (its name or URL) and a "
                                     "branch, e.g. 'us main', not 'us'"),
                ({'upstream': 'gl main'}, "No remote matches 'gl'"),
                ({**DTC, 'history': 'straight'},
                 "Unknown value 'straight' in the maint setting 'history': "
                 'use linear, prefer-linear, any'),
                ({**DTC, 'trailers_end': 'msgid signoff'},
                 "Unknown value 'msgid' in the maint setting 'trailers_end'"),
                ({**DTC, 'require': 'dco'},
                 "Unknown value 'dco' in the maint setting 'require'")):
            with self.assertRaises(ValueError) as exc:
                maint.get_rules(values, self.repo)
            self.assertIn(msg, str(exc.exception))

    def test_list_patches(self):
        """A patch from the list ends with its Message-ID and your sign-off"""
        self.commit('good', FRED, [
            f'Signed-off-by: {FRED}', 'Reviewed-by: Herve <h@x.org>',
            'Message-ID: <1@x.org>', f'Signed-off-by: {ME}'])
        self.commit('tag after', FRED, [
            f'Signed-off-by: {FRED}', 'Message-ID: <2@x.org>',
            'Reviewed-by: Herve <h@x.org>', f'Signed-off-by: {ME}'])
        self.commit('no sign-off', FRED, [
            'Message-ID: <3@x.org>', f'Signed-off-by: {ME}'])
        self.commit('not mine', FRED, [
            f'Signed-off-by: {FRED}', 'Message-ID: <4@x.org>',
            'Signed-off-by: David Gibson <david@gibson.dropbear.id.au>'])
        self.commit('two', FRED, [
            f'Signed-off-by: {FRED}', 'Message-ID: <5@x.org>',
            'Message-ID: <6@x.org>', f'Signed-off-by: {ME}'])

        # Your own patch has your sign-off as its author, before the
        # Message-ID
        self.commit('mine', ME, [f'Signed-off-by: {ME}', 'Message-ID: <7@x>'])
        self.commit('mine signed', ME, [
            f'Signed-off-by: {ME}', 'Message-ID: <8@x>', f'Signed-off-by: {ME}'])
        end = ("The trailers should end with Message-ID:, then "
               f"'Signed-off-by: {ME}', but end with ")
        self.assertEqual({
            'good': [],
            'tag after': [f'{end}Reviewed-by:, Signed-off-by:'],
            'no sign-off': [f'No Signed-off-by from the author, {FRED}'],
            'not mine': [f'{end}Message-ID:, Signed-off-by:'],
            'two': ['There is more than one Message-ID:'],
            'mine': [],
            'mine signed': ['The trailers should end with Message-ID:, but '
                            'end with Signed-off-by:'],
        }, self.check())

    def test_pull_requests(self):
        """The commits of a pull request end with your sign-off, and the last
        with a Link: to it too"""
        self.commit('pr 1', FRED, [f'Signed-off-by: {FRED}',
                                   f'Signed-off-by: {ME}'])
        self.commit('pr 2', FRED, [
            f'Signed-off-by: {FRED}',
            'Link: https://github.com/dgibson/dtc/pull/193',
            f'Signed-off-by: {ME}'])
        self.commit('list', FRED, [
            f'Signed-off-by: {FRED}', 'Message-ID: <1@x.org>',
            f'Signed-off-by: {ME}'])

        # A merge request on GitLab, whose Link: comes after the sign-off
        self.commit('mr', FRED, [
            f'Signed-off-by: {FRED}', f'Signed-off-by: {ME}',
            'Link: https://gitlab.com/dgibson/dtc/-/merge_requests/4'])

        # Nothing says where this came from, nor the next, since a link to
        # another repo's pull request does not count
        self.commit('orphan', FRED, [f'Signed-off-by: {FRED}',
                                     f'Signed-off-by: {ME}'])
        self.commit('other repo', FRED, [
            f'Signed-off-by: {FRED}', 'Link: https://github.com/x/y/pull/1',
            f'Signed-off-by: {ME}'])

        # Your own commit need not be sent to the list
        self.commit('direct', ME, [f'Signed-off-by: {ME}'])
        orphan = ['No Message-ID:, and no Link: to a pull or merge request '
                  'after it, so it is not clear where it is from']
        self.assertEqual({
            'pr 1': [], 'pr 2': [], 'list': [],
            'mr': [f"The trailers should end with Link:, then 'Signed-off-by: "
                   f"{ME}', but end with Signed-off-by:, Link:"],
            'orphan': orphan, 'other repo': orphan, 'direct': [],
        }, self.check())

    def test_direct(self):
        """Your own commit need not be sent to the list"""
        self.commit('direct', ME, [f'Signed-off-by: {ME}'])
        self.commit('unsigned', ME, ['Reviewed-by: Fred <fred@x.org>'])
        self.assertEqual({
            'direct': [],
            'unsigned': [f'No Signed-off-by from the author, {ME}',
                         "The trailers should end with 'Signed-off-by: "
                         f"{ME}', but end with Reviewed-by:"],
        }, self.check())

    def test_patman_tags(self):
        """patman's own tags must not be pushed"""
        self.commit('tagged', ME, [f'Signed-off-by: {ME}',
                                   'Series-links: 1:abc@x.org',
                                   'Change-Id: I1234'])
        self.assertEqual({'tagged': [
            "Has patman's own tags (Series-links:, Change-Id:), which 'patman "
            "send' removes but a push does not: push the patches as they were "
            'sent']}, self.check())

    def test_issues(self):
        """A link to an issue is in a trailer for it, to the project's repo"""
        base = [f'Signed-off-by: {FRED}']
        end = ['Message-ID: <1@x.org>', f'Signed-off-by: {ME}']
        self.commit('fixes', FRED, [
            'Closes: https://github.com/dgibson/dtc/issues/83', *base, *end])
        self.commit('relates', FRED, [
            'Link: https://gitlab.com/dgibson/dtc/-/issues/2', *base, *end])
        self.commit('other', FRED, [
            'Closes: https://github.com/other/dtc/issues/1', *base, *end])
        self.commit('wrong trailer', FRED, [
            'Fixes: https://github.com/dgibson/dtc/issues/83', *base, *end])
        self.assertEqual({
            'fixes': [], 'relates': [],
            'other': ['Closes: links to an issue in github.com/other/dtc, '
                      "which is not one of the project's repos "
                      '(github.com/dgibson/dtc, gitlab.com/dgibson/dtc)'],
            'wrong trailer': ['Fixes: links to an issue; use Closes: or '
                              'Link:'],
        }, self.check())

    def test_merge(self):
        """A merge commit is allowed only when the rules say so"""
        self.git('checkout', '-q', '-b', 'side')
        self.commit('side', ME, [f'Signed-off-by: {ME}'])
        self.git('checkout', '-q', 'main')
        self.commit('main', ME, [f'Signed-off-by: {ME}'])
        self.git('merge', '-q', '--no-ff', '-m', 'Merge side', 'side')
        merge = 'Merge side'
        self.assertEqual(['Merge commit: use --allow-merge if it is meant'],
                         self.check()[merge])
        self.assertEqual([], self.check(allow_merge=True)[merge])

        rules = self.rules(history='linear')
        commits = maint.read_commits(rules.upstream, rules.branch, self.repo)
        self.assertEqual(['Merge commit, but the history must be linear'],
                         dict((cmt.subject, probs) for cmt, probs in
                              maint.check_commits(rules, commits, True))[merge])
        rules = self.rules(history='any')
        self.assertEqual([], dict((cmt.subject, probs) for cmt, probs in
                                  maint.check_commits(rules, commits))[merge])

    def test_do_check(self):
        """The check says what is wrong with the queue"""
        rules = self.rules()
        with terminal.capture() as (out, _):
            self.assertEqual(0, maint.do_check(rules, git_dir=self.repo))
        self.assertEqual('Nothing to check: main has no commits which are not '
                         'in us/main\n', out.getvalue())

        good = self.commit('good', FRED, [
            f'Signed-off-by: {FRED}', 'Message-ID: <1@x.org>',
            f'Signed-off-by: {ME}'])
        with terminal.capture() as (out, _):
            self.assertEqual(0, maint.do_check(rules, git_dir=self.repo))
        self.assertEqual(f'Checking 1 commit in us/main..main\n'
                         f'  ok   {good[:12]} good\nAll ok\n', out.getvalue())

        bad = self.commit('bad', FRED, [f'Signed-off-by: {FRED}'])
        with terminal.capture() as (out, _):
            self.assertEqual(1, maint.do_check(rules, git_dir=self.repo))
        self.assertEqual(
            f'Checking 2 commits in us/main..main\n'
            f'  ok   {good[:12]} good\n'
            f'  FAIL {bad[:12]} bad\n'
            '       - No Message-ID:, and no Link: to a pull or merge request '
            'after it, so it is not clear where it is from\n'
            '1 of 2 with problems\n', out.getvalue())

        # A branch which is behind has nothing to check
        self.git('update-ref', 'refs/remotes/us/main', bad)
        self.git('reset', '-q', '--hard', 'HEAD~1')
        with terminal.capture() as (out, _):
            self.assertEqual(0, maint.do_check(rules, git_dir=self.repo))
        self.assertEqual('Nothing to check: main is behind us/main; to bring '
                         'it up to date: git merge --ff-only us/main\n',
                         out.getvalue())

        # On another branch, the merge needs main checked out
        self.git('checkout', '-q', '-b', 'other')
        with terminal.capture() as (out, _):
            self.assertEqual(0, maint.do_check(rules, git_dir=self.repo))
        self.assertEqual('Nothing to check: main is behind us/main; to bring '
                         'it up to date: git checkout main && git merge '
                         '--ff-only us/main\n', out.getvalue())
        self.git('checkout', '-q', 'main')

        # The branch must be on top of the upstream
        self.git('update-ref', 'refs/remotes/us/main', good)
        self.git('reset', '-q', '--hard', 'HEAD~1')
        other = self.commit('other', ME, [f'Signed-off-by: {ME}'])
        with terminal.capture() as (out, _):
            self.assertEqual(1, maint.do_check(rules, git_dir=self.repo))
        self.assertEqual("'main' does not contain us/main, so it cannot be "
                         'pushed; to rebase it: git rebase us/main main\n'
                         'Checking 1 commit in us/main..main\n'
                         f'  ok   {other[:12]} other\n'
                         'All ok, once rebased on us/main\n', out.getvalue())

        with self.assertRaises(ValueError) as exc:
            maint.do_check(maint.get_rules(DTC, self.repo, 'queue'),
                           git_dir=self.repo)
        self.assertEqual("No branch 'queue'", str(exc.exception))

        # With HEAD detached, the branch must be given
        self.git('checkout', '-q', '--detach')
        with self.assertRaises(ValueError) as exc:
            self.rules()
        self.assertEqual('No branch is checked out: use -b to say which to '
                         'check', str(exc.exception))
