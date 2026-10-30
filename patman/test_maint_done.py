# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for finishing with a series which a maintainer applied"""

from unittest import mock

from u_boot_pylib import terminal

from patman import cseries
from patman import maint
from patman import maint_done
from patman import maint_thanks
from patman.test_maint import ME
from patman.test_maint_apply import LoreTestCase


class TestMaintDone(LoreTestCase):
    """Test finishing with a series which a maintainer applied"""
    def done(self, what=None, sent=None, deleted=(), answer='y', **kwargs):
        """Finish with a series, with Gmail faked

        Args:
            what (str or None): Series to finish with
            sent (int or None): Number of drafts which were sent, or None
                for all of them
            deleted (list of int): Drafts which were deleted without being
                sent
            answer (str): Answer to give if asked
            kwargs: Other arguments for do_done()

        Return:
            tuple: Exit code, output, list of drafts looked for in Gmail,
                name of each series archived
        """
        looked = []
        archived = []

        def sync(_service, items):
            looked.extend(items)
            return ({item.idnum: ('text', 'msg', 'thread') for item in items
                     if sent is None or item.idnum < sent},
                    list(deleted), {})

        with mock.patch.object(maint_done.gmail, 'get_service'), \
                mock.patch.object(maint_done.gmail, 'sync_drafts',
                                  side_effect=sync), \
                mock.patch.object(cseries.Cseries, 'archive',
                                  side_effect=archived.append), \
                mock.patch('builtins.input', return_value=answer), \
                terminal.capture() as (out, _):
            ret = maint_done.do_done(self.rules(), self.lore_list(), what,
                                     test_db=self.tmpdir, git_dir=self.repo,
                                     **kwargs)
        return ret, out.getvalue(), looked, archived

    def thanked(self, root, drafts=1):
        """Note that a series is thanked, as 'maint thanks' does"""
        state = maint_thanks.read_state(self.repo)
        state['thanked'].append(root)
        state['drafts'][root] = [{'id': f'd{num}', 'time': 3 * maint_done.DAY}
                                 for num in range(drafts)]
        maint_thanks.write_state(state, self.repo)

    def upstream(self):
        """Take the push branch upstream, as 'maint publish' does"""
        self.git('update-ref', 'refs/remotes/us/main', 'main-push')

    def test_own(self):
        """Your own series is finished with once its thanks are sent, and
        its branch archived"""
        root = self.make_series(ME)
        name = self.add_series(root)
        found = f"Series '{name}' v1: {root}\n"
        series = "'dtc: Tidy (v1)'"

        # Only part of it is upstream
        self.apply(root, patches='1')
        self.upstream()
        ret, out, _, _ = self.done()
        self.assertEqual((1, found + f"{series} is only partly in us/main, "
                          'without patch(es) 2; use -f to finish with it '
                          'anyway\n'), (ret, out))

        self.apply(root, patches='2')
        self.upstream()
        ret, out, _, _ = self.done()
        self.assertEqual((1, found + f"{series} is not thanked yet: run "
                          "'patman maint thanks'; use -f to finish with it "
                          'anyway\n'), (ret, out))

        self.thanked(root)
        ret, out, looked, _ = self.done(sent=0)
        self.assertEqual((1, found + f'The thanks for {series} are not sent '
                          'yet; use -f to finish with it anyway\n'), (ret, out))
        self.assertEqual([('d0', 'Re: [PATCH 0/2] dtc: Tidy',
                           3 * maint_done.DAY, '1970/01/03')],
                         [(item.draft_id, item.subject, item.since, item.after)
                          for item in looked])

        ret, out, _, _ = self.done(sent=0, deleted=[0])
        self.assertIn('were deleted without being sent', out)

        # A dry run does nothing
        ret, out, _, archived = self.done(dry_run=True)
        self.assertEqual((0, found + f"Would finish with {series}, archiving "
                          f"series '{name}'\n", []), (ret, out, archived))

        # Not archived if the answer is no
        ret, out, _, archived = self.done(answer='n')
        self.assertEqual((0, found + f'Finished with {series}\n', []),
                         (ret, out, archived))

        ret, out, _, archived = self.done(name)
        self.assertEqual((0, found + f'Finished with {series}\n', [name]),
                         (ret, out, archived))
        self.assertEqual([root], maint_thanks.read_state(self.repo)['done'])

    def test_all(self):
        """With no series, each which is thanked is gone through"""
        root = self.make_series()
        self.apply(root)
        self.upstream()
        self.add_series()
        self.assertEqual((0, 'Nothing to finish with\n'), self.done()[:2])

        # Thanks drafted before patman kept the drafts cannot be checked
        self.thanked(root, 0)
        ret, out, looked, archived = self.done()
        self.assertEqual((0, "Finished with 'dtc: Tidy (v1)'\n", [], []),
                         (ret, out, looked, archived))
        self.assertEqual((0, 'Nothing to finish with\n'), self.done()[:2])

        # -f finishes with it anyway
        self.assertEqual(0, self.done(root, sent=0, force=True)[0])

    def test_issues(self):
        """Issues which the commits close are checked"""
        def cmt(*trailers):
            return maint.Commit('a' * 40, [], ME, 'sjg@chromium.org', 'Fix',
                                list(trailers))

        url = 'https://github.com/dgibson/dtc/issues/{}'
        states = {'1': 'closed\n', '2': 'open\n', '3': ''}
        with mock.patch.object(
                maint_done.command, 'output',
                side_effect=lambda *args, **_: states[args[2].split('/')[-1]]):
            self.assertEqual(
                [url.format(2), f'{url.format(3)} (cannot check)'],
                maint_done.open_issues([
                    cmt(('Closes', url.format(1))),
                    cmt(('Closes', url.format(2)), ('Link', url.format(4))),
                    cmt(('Closes', url.format(3)),
                        ('Closes', 'https://example.com/5'))]))
