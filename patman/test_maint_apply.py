# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for applying a series from the list as a maintainer"""

import email
import email.policy
import os
import re
from unittest import mock

from u_boot_pylib import terminal

from patman import control
from patman import cseries
from patman import lore
from patman import lorelist
from patman import maint
from patman import maint_apply
from patman.test_maint import FRED, ME, RepoTestCase

LIST = 'https://lore.kernel.org/devicetree-compiler'
HERVE = 'Herve Codina <herve@x.org>'
ANN = 'Ann Acker <ann@x.org>'
TIM = 'Tim Tester <tim@x.org>'

# The line which starts each patch from 'git format-patch'
RE_FROM = re.compile(r'^From [0-9a-f]{40} .*\n', re.M)


def message(msgid, subject, sender, body, reply_to=None):
    """Make the text of an email, as it appears in an mbox file

    Args:
        msgid (str): Message-ID, without angle brackets
        subject (str): Subject
        sender (str): Who sent it
        body (str): Body
        reply_to (str or None): Message-ID it replies to

    Return:
        str: The email, with its 'From ' line
    """
    lines = ['From mboxrd@z Thu Jan  1 00:00:00 1970', f'From: {sender}',
             f'Subject: {subject}', 'Date: Tue, 27 Oct 2026 10:38:38 +0200',
             f'Message-ID: <{msgid}>']
    if reply_to:
        lines += [f'In-Reply-To: <{reply_to}>', f'References: <{reply_to}>']
    return '\n'.join(lines) + '\n\n' + body.rstrip('\n') + '\n\n'


def title(msg):
    """Get the title of a patch, without its prefix

    Args:
        msg (email.message.EmailMessage): Patch from 'git format-patch'

    Return:
        str: Title, e.g. 'dtc: Add a file'
    """
    return str(msg['Subject']).rsplit('] ', maxsplit=1)[-1]


class LoreTestCase(RepoTestCase):
    """A test with a git repo like dtc's and a thread on a fake lore"""
    def setUp(self):
        super().setUp()
        self.thread = ''

    def make_patches(self, author):
        """Make two patches on a branch which is then deleted, so that they
        are not in the repo

        Args:
            author (str): Who wrote them

        Return:
            str: The patches, from 'git format-patch'
        """
        self.git('checkout', '-q', '-b', 'dev')
        sob = f'Signed-off-by: {author}'
        for subject, line in (('dtc: Add a file', 'one'),
                              ('dtc: Grow the file', 'two')):
            with open(os.path.join(self.repo, 'a.txt'), 'a',
                      encoding='utf-8') as outf:
                outf.write(f'{line}\n')
            self.git('add', 'a.txt')
            self.commit(subject, author, [sob])
        patches = self.git('format-patch', '--stdout', '-2')
        self.git('checkout', '-q', 'main')
        self.git('branch', '-q', '-D', 'dev')
        return patches

    def make_series(self, author=FRED, cover=True, total=2):
        """Make a series of two patches and its thread on the list

        Args:
            author (str): Who wrote and sent the series
            cover (bool): True to give it a cover letter
            total (int): Number of patches the subjects claim, e.g. 3 to make
                the series look incomplete

        Return:
            str: Message-ID of the series
        """
        patches = self.make_patches(author)
        name = author.split()[0].lower()
        root = f'{name}-0@x.org' if cover else f'{name}-1@x.org'
        if cover:
            self.thread += message(root, f'[PATCH 0/{total}] dtc: Tidy',
                                   author, 'Some tidy-ups')
        texts = [text for text in RE_FROM.split(patches) if text.strip()]
        for seq, text in enumerate(texts, start=1):
            msg = email.message_from_string(text, policy=email.policy.default)
            msgid = f'{name}-{seq}@x.org'
            self.thread += message(
                msgid, f'[PATCH {seq}/{total}] {title(msg)}', author,
                msg.get_content(), None if msgid == root else root)
        return root

    def add_series(self, link=None):
        """Note the current branch as a series which patman sent

        Args:
            link (str or None): Message-ID it was sent with, or None to set
                up the database with no series

        Return:
            str: Name of the series
        """
        name = maint.current_branch(self.repo)
        cser = cseries.Cseries(self.tmpdir)
        cser.open_database()
        if link:
            cser.db.ser_ver_add(cser.db.series_add(name, 'Tidy'), 1,
                                link=link)
            cser.db.commit()
        cser.close_database()
        return name

    def reply(self, msgid, to, sender, body):
        """Add a reply to the thread

        Args:
            msgid (str): Message-ID of the reply
            to (str): Message-ID which it replies to
            sender (str): Who sent it
            body (str): Body
        """
        self.thread += message(msgid, 'Re: [PATCH] dtc', sender, body, to)

    def lore_list(self, newer=()):
        """Make a LoreList which reads the test's thread

        Args:
            newer (list of dict): Series which lore finds with the same
                title, e.g. a newer version

        Return:
            LoreList: The list
        """
        llist = lorelist.LoreList(LIST)

        async def get_thread(_client, msgid, recheck=False):
            del recheck
            messages = lore.parse_mbox(self.thread.encode('utf-8'))
            if msgid not in {lorelist.msgid_of(msg) for msg in messages}:
                return None
            return messages

        async def query_series(_client, _desc):
            return list(newer)

        llist.lore.get_thread = get_thread
        llist.query_series = query_series
        return llist

    def apply(self, link, llist=None, **kwargs):
        """Apply a series with the dtc rules

        Return:
            tuple: (exit code, output)
        """
        kwargs.setdefault('fetch', False)
        with terminal.capture() as (out, _):
            ret = maint_apply.apply_series(
                kwargs.pop('rules', None) or self.rules(),
                llist or self.lore_list(), link, git_dir=self.repo, **kwargs)
        return ret, out.getvalue()

    def trailers(self, ref):
        """Get the trailers of each commit on a branch which is not upstream

        Return:
            list of list of str: Trailers of each commit, oldest first
        """
        out = self.git('log', '--reverse', '--format=%(trailers:only)%x00',
                       f'us/main..{ref}')
        return [part.strip().splitlines() for part in out.split('\0')
                if part.strip()]


class TestMaintApply(LoreTestCase):
    """Test applying a series from the list"""
    def test_apply(self):
        """A series is applied to the push branch, with its tags"""
        link = self.make_series()
        self.reply('r1@x.org', 'fred-1@x.org', HERVE,
                   f'> one\n\nReviewed-by: {HERVE}')
        self.reply('r2@x.org', 'fred-0@x.org', ANN, f'Acked-by: {ANN}')
        self.reply('r3@x.org', 'r2@x.org', TIM, f'Tested-by: {TIM}')
        self.reply('r4@x.org', 'fred-2@x.org', TIM,
                   f'Tested-by: {TIM}\nFixes: abc ("x")')
        ret, out = self.apply(link)
        self.assertEqual(0, ret, out)
        self.assertTrue(out.startswith(
                      '   1 [1/2] dtc: Add a file\n'
                      f'       + Reviewed-by: {HERVE}\n'
                      f'       + Acked-by: {ANN}\n'
                      f'       + Tested-by: {TIM}\n'), out)
        self.assertIn('Checking 2 commits in us/main..main-push\n', out)
        self.assertNotIn('FAIL', out)
        self.assertTrue(out.endswith('2 of 2 patches applied to main-push\n'),
                        out)

        # The tags come after the author's, then the Message-ID and your
        # sign-off; a tag in reply to the cover letter, or to a reply to it,
        # goes on each patch
        self.assertEqual([
            [f'Signed-off-by: {FRED}', f'Reviewed-by: {HERVE}',
             f'Acked-by: {ANN}', f'Tested-by: {TIM}',
             'Message-ID: <fred-1@x.org>', f'Signed-off-by: {ME}'],
            [f'Signed-off-by: {FRED}', f'Acked-by: {ANN}', f'Tested-by: {TIM}',
             'Message-ID: <fred-2@x.org>', f'Signed-off-by: {ME}']],
                         self.trailers('main-push'))

        # The author and the tree are kept, and the checkout is not touched
        self.assertEqual(FRED, self.git('log', '-1', '--format=%an <%ae>',
                                        'main-push').strip())
        self.assertEqual('one\ntwo\n',
                         self.git('show', 'main-push:a.txt'))
        self.assertEqual('main', maint.current_branch(self.repo))
        self.assertEqual(1, len(self.git('worktree', 'list').splitlines()))

        # Applying it again does nothing
        before = self.git('rev-parse', 'main-push')
        self.assertEqual(
            (0, "'dtc: Tidy (v1)' is on main-push already: nothing to "
                'apply\n'), self.apply(link))
        self.assertEqual(before, self.git('rev-parse', 'main-push'))

    def test_fetch(self):
        """The upstream is fetched first, as someone may have pushed to it"""
        link = self.make_series()
        bare = os.path.join(self.tmpdir, 'us.git')
        self.git('init', '-q', '--bare', bare, cwd=self.tmpdir)
        self.git('remote', 'set-url', 'us', bare)
        self.commit('Pushed by David', FRED, [f'Signed-off-by: {FRED}'])
        david = self.git('rev-parse', 'HEAD').strip()
        self.git('push', '-q', 'us', 'HEAD:refs/heads/main')
        self.git('reset', '-q', '--hard', 'HEAD~1')
        self.git('update-ref', 'refs/remotes/us/main', 'HEAD')

        rules = self.rules(upstream='us main')
        ret, out = self.apply(link, rules=rules, fetch=True)
        self.assertEqual(0, ret, out)
        self.assertEqual(david, self.git('rev-parse', 'us/main').strip())
        self.assertEqual(david, self.git('rev-parse', 'main-push~2').strip())

        # Nothing is applied if the upstream cannot be fetched
        self.git('remote', 'set-url', 'us', os.path.join(self.tmpdir, 'no'))
        ret, out = self.apply(link, rules=rules, fetch=True, redo=True)
        self.assertEqual(1, ret)
        self.assertTrue(out.startswith('Cannot fetch us: '), out)

    def test_own(self):
        """Your own series does not get your sign-off again"""
        link = self.make_series(ME, cover=False)
        ret, out = self.apply(link)
        self.assertEqual(0, ret, out)
        self.assertEqual([[f'Signed-off-by: {ME}', 'Message-ID: <simon-1@x.org>'],
                          [f'Signed-off-by: {ME}', 'Message-ID: <simon-2@x.org>']],
                         self.trailers('main-push'))

    def test_some_patches(self):
        """Only some of the patches can be applied, and nothing in a dry
        run"""
        link = self.make_series()
        ret, out = self.apply(link, dry_run=True)
        self.assertEqual(0, ret)
        self.assertTrue(out.endswith('Would apply 2 of 2 patches to '
                                     'main-push\n'), out)
        self.assertFalse(maint.gitutil.ref_exists('refs/heads/main-push',
                                                  self.repo))

        ret, out = self.apply(link, patches='1')
        self.assertEqual(0, ret, out)
        self.assertEqual(1, len(self.trailers('main-push')))
        self.assertIn('1 of 2 patches applied to main-push\n', out)

        # The rest of the series cannot be applied without -P, which is
        # given; nor can a patch which is there already
        with self.assertRaises(ValueError) as exc:
            self.apply(link)
        self.assertEqual("main-push has patch 1 of 'dtc: Tidy (v1)' already: "
                         'use -P 2 to apply the others', str(exc.exception))
        self.assertEqual(
            (0, "Those patches of 'dtc: Tidy (v1)' are on main-push "
                'already: nothing to apply\n'),
            self.apply(link, patches='1'))
        ret, out = self.apply(link, patches='2')
        self.assertEqual(0, ret, out)
        self.assertEqual(2, len(self.trailers('main-push')))

        for spec, msg in (('3', "Patch '3' is not in the series, which has "
                                '2 patches'),
                          ('x', "Cannot parse 'x': use e.g. '1-3,5'")):
            with self.assertRaises(ValueError) as exc:
                maint_apply.parse_patches(spec, 2)
            self.assertEqual(msg, str(exc.exception))
        self.assertEqual([0, 2, 3, 4], maint_apply.parse_patches('1,3-5', 5))

    def test_refused(self):
        """A series which is incomplete, or not the latest, is refused"""
        link = self.make_series(total=3)
        with self.assertRaises(ValueError) as exc:
            self.apply(link)
        self.assertEqual("Only 2 of the 3 patches of 'dtc: Tidy (v1)' are on "
                         'the list: use -f to apply them anyway',
                         str(exc.exception))

        self.thread = ''
        link = self.make_series()
        llist = self.lore_list([{'id': 'v2@x.org', 'name': 'dtc: Tidy',
                                 'version': 2},
                                {'id': 'other@x.org', 'name': 'dtc: Other',
                                 'version': 3}])
        with self.assertRaises(ValueError) as exc:
            self.apply(link, llist)
        self.assertEqual("'dtc: Tidy (v1)' is not the latest version: v2 is "
                         'v2@x.org; use -f to apply it anyway',
                         str(exc.exception))
        ret, out = self.apply(link, llist, force=True)
        self.assertEqual(0, ret, out)

    def test_conflict(self):
        """A series which does not apply leaves the push branch as it was"""
        link = self.make_series()
        self.git('checkout', '-q', '-b', 'main-push')
        with open(os.path.join(self.repo, 'a.txt'), 'w',
                  encoding='utf-8') as outf:
            outf.write('different\n')
        self.git('add', 'a.txt')
        self.commit('dtc: Conflict', ME, [f'Signed-off-by: {ME}'])
        before = self.git('rev-parse', 'main-push')

        # It is checked out here, so the patches are tried here
        ret, out = self.apply(link)
        self.assertEqual(1, ret)
        self.assertIn("Cannot apply 'dtc: Tidy (v1)' to main-push, even with "
                      'a three-way merge, so nothing is applied; perhaps ask '
                      'for a rebase:', out)
        self.assertEqual(before, self.git('rev-parse', 'main-push'))
        self.assertEqual('', self.git('status', '--porcelain'))

        # A tree with changes is not touched
        with open(os.path.join(self.repo, 'a.txt'), 'w',
                  encoding='utf-8') as outf:
            outf.write('changed\n')
        with self.assertRaises(ValueError) as exc:
            self.apply(link)
        self.assertEqual(f'main-push is checked out in {self.repo}, which has '
                         'changes: commit or stash them first',
                         str(exc.exception))

    def test_redo(self):
        """-R starts the push branch again, e.g. to pick up new tags"""
        link = self.make_series()
        ret, out = self.apply(link)
        self.assertEqual(0, ret, out)

        # Another series is on the push branch, and a tag comes in
        self.git('checkout', '-q', 'main-push')
        with open(os.path.join(self.repo, 'b.txt'), 'w',
                  encoding='utf-8') as outf:
            outf.write('other\n')
        self.git('add', 'b.txt')
        self.commit('dtc: Other', ME, [f'Signed-off-by: {ME}'])
        self.git('checkout', '-q', 'main')
        self.reply('r1@x.org', 'fred-1@x.org', HERVE,
                   f'Reviewed-by: {HERVE}')
        before = self.git('rev-parse', 'main-push').strip()
        self.assertEqual('3', self.git('rev-list', '--count',
                                       'us/main..main-push').strip())

        # A dry run says what would be dropped, and drops nothing
        ret, out = self.apply(link, redo=True, dry_run=True)
        self.assertEqual(0, ret, out)
        self.assertIn('Would start main-push again from us/main, dropping 3 '
                      f'commits (was {before[:12]})\n', out)
        self.assertEqual(before, self.git('rev-parse', 'main-push').strip())

        ret, out = self.apply(link, redo=True)
        self.assertEqual(0, ret, out)
        self.assertIn('Starting main-push again from us/main, dropping 3 '
                      f'commits (was {before[:12]})\n', out)
        self.assertEqual(
            ['dtc: Add a file', 'dtc: Grow the file'],
            self.git('log', '--reverse', '--format=%s',
                     'us/main..main-push').splitlines())
        self.assertIn(f'Reviewed-by: {HERVE}', self.trailers('main-push')[0])
        self.assertEqual('main', maint.current_branch(self.repo))

    def test_redo_fails(self):
        """-R leaves the push branch as it was if the series does not apply,
        or the branch is checked out with changes"""
        link = self.make_series()
        ret, out = self.apply(link)
        self.assertEqual(0, ret, out)
        before = self.git('rev-parse', 'main-push')

        with mock.patch.object(maint_apply, '_am', return_value='no good'):
            ret, out = self.apply(link, redo=True)
        self.assertEqual(1, ret)
        self.assertIn('so nothing is applied', out)
        self.assertEqual(before, self.git('rev-parse', 'main-push'))

        self.git('checkout', '-q', 'main-push')
        with open(os.path.join(self.repo, 'a.txt'), 'a',
                  encoding='utf-8') as outf:
            outf.write('changed\n')
        with self.assertRaises(ValueError) as exc:
            self.apply(link, redo=True)
        self.assertEqual(f'main-push is checked out in {self.repo}, which has '
                         'changes: commit or stash them first',
                         str(exc.exception))
        self.assertEqual(before, self.git('rev-parse', 'main-push'))

        # Once it is clean, it is reset where it is checked out
        self.git('checkout', '-q', '--', 'a.txt')
        ret, out = self.apply(link, redo=True)
        self.assertEqual(0, ret, out)
        self.assertEqual('', self.git('status', '--porcelain'))
        self.assertEqual(2, len(self.trailers('main-push')))

    def test_checked_out(self):
        """The push branch is applied to where it is checked out"""
        link = self.make_series()
        self.git('branch', 'main-push')
        self.git('checkout', '-q', 'main-push')
        ret, out = self.apply(link)
        self.assertEqual(0, ret, out)
        with open(os.path.join(self.repo, 'a.txt'), encoding='utf-8') as inf:
            self.assertEqual('one\ntwo\n', inf.read())
        self.assertEqual('', self.git('status', '--porcelain'))

    def test_command(self):
        """'patman maint apply' reads the series from the upstream's list"""
        cser = cseries.Cseries(self.tmpdir)
        cser.open_database()
        cser.db.upstream_add('us', 'https://us', lore_url=LIST)
        cser.db.commit()
        cser.close_database()
        llist = control.maint_list(self.rules(), self.tmpdir)
        self.assertEqual((LIST, 'us'), (llist.url, llist.upstream))
        with self.assertRaises(ValueError) as exc:
            control.maint_list(maint.get_rules(
                {'upstream': 'david main', 'signoff': ME}, self.repo),
                self.tmpdir)
        self.assertEqual("The upstream 'david' has no list on lore: give it "
                         "one with 'patman upstream set david -l <url>'",
                         str(exc.exception))

    def test_link(self):
        """The series to apply may be given by name, or be the branch's"""
        cser = cseries.Cseries(self.tmpdir)
        cser.open_database()
        sid = cser.db.series_add(maint.current_branch(self.repo), 'Fix it')
        cser.db.ser_ver_add(sid, 1, link='v1@x.org')
        cser.db.ser_ver_add(sid, 2, link='v2@x.org')
        cser.db.ser_ver_add(sid, 3)
        cser.db.ser_ver_add(cser.db.series_add('unsent', 'Not yet'), 1)
        cser.db.commit()
        cser.close_database()

        def link(series):
            with terminal.capture() as (out, _):
                found = maint_apply.find_link(series, self.tmpdir, self.repo)
            return found, out.getvalue().strip()

        # A Message-ID or link is used as it is
        self.assertEqual(('a@x.org', ''), link('a@x.org'))
        self.assertEqual((f'{LIST}/a@x.org/', ''), link(f'{LIST}/a@x.org/'))

        # Else the latest version which has been sent is used, of the
        # current branch's series or the one named
        name = maint.current_branch(self.repo)
        want = ('v2@x.org', f"Series '{name}' v2: v2@x.org")
        self.assertEqual(want, link(None))
        self.assertEqual(want, link(name))

        for series, msg in (
                ('unsent', "Series 'unsent' has not been sent"),
                ('bogus', "There is no series 'bogus'")):
            with self.assertRaises(ValueError) as exc:
                link(series)
            self.assertEqual(f'{msg}: give the Message-ID of the series to '
                             'apply', str(exc.exception))
