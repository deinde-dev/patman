# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for drafting the thanks for series which a maintainer applied"""

from unittest import mock

from u_boot_pylib import terminal

from patman import control
from patman import cseries
from patman import maint
from patman import maint_thanks
from patman.review import common
from patman.test_maint import FRED, ME
from patman.test_maint_apply import LoreTestCase, message

CLOSING = 'Regards,\nSimon'


class TestMaintThanks(LoreTestCase):
    """Test drafting the thanks for series which a maintainer applied"""
    def setUp(self):
        super().setUp()
        # Number of drafts made so far, and the drafts deleted
        self.drafts = 0
        self.deleted = []

    def thank(self, rng='us/main..main-push', drafted=None, series=False,
              **kwargs):
        """Draft the thanks, with Gmail faked

        Args:
            rng (str or None): Commits to thank, or with series, what to
                thank
            drafted (int or None): Number of drafts which Gmail makes, or
                None for all of them
            series (bool): True to use thank(), rather than draft_thanks()
            kwargs: Other arguments for draft_thanks()

        Return:
            tuple: Exit code, output, list of (headers, text) drafted
        """
        made = []

        def create(envelopes, bodies, account=None, sender=None):
            self.assertEqual((None, ME), (account, sender))
            first = self.drafts
            self.drafts += len(envelopes)
            made.extend((envelopes[num], bodies[num])
                        for num in sorted(envelopes))
            return {num: f'draft{first + num}' for num in sorted(envelopes)
                    if drafted is None or num < drafted}

        def delete(_service, draft_id):
            self.deleted.append(draft_id)

        with mock.patch.object(maint_thanks.gmail,
                               'create_drafts_from_envelopes',
                               side_effect=create), \
                mock.patch.object(maint_thanks.gmail, 'get_service'), \
                mock.patch.object(maint_thanks.gmail, 'delete_draft',
                                  side_effect=delete), \
                terminal.capture() as (out, _):
            rules = self.rules(**kwargs.pop('values', {}))
            if series:
                ret = maint_thanks.thank(rules, self.lore_list(), rng,
                                         CLOSING, test_db=self.tmpdir,
                                         git_dir=self.repo, **kwargs)
            else:
                ret = maint_thanks.draft_thanks(
                    rules, self.lore_list(), rng, CLOSING, git_dir=self.repo,
                    **kwargs)
        return ret, out.getvalue(), made

    def test_series_of(self):
        """A patch is traced back to its series"""
        llist = self.lore_list()
        self.make_series()
        self.thread += message('v2-0@x.org', '[PATCH v2 0/2] dtc: Tidy',
                               'Fred <fred@x.org>', 'v2', 'fred-0@x.org')
        self.thread += message('v2-1@x.org', '[PATCH v2 1/2] dtc: Add',
                               'Fred <fred@x.org>', 'x', 'v2-0@x.org')
        self.thread += message('v2-2@x.org', '[PATCH v2 2/2] dtc: Grow',
                               'Fred <fred@x.org>', 'x', 'v2-1@x.org')
        self.thread += message('r1@x.org', 'Re: [PATCH 1/2] dtc',
                               'Ann <ann@x.org>', 'Hmm', 'fred-1@x.org')

        def find(msgid):
            return common.run(llist.series_of(None, f'<{msgid}>'))

        self.assertEqual(
            ['fred-0@x.org', 'fred-0@x.org', 'fred-0@x.org', 'v2-0@x.org',
             'v2-0@x.org', None],
            [find(msgid) for msgid in ('fred-0@x.org', 'fred-1@x.org',
                                       'fred-2@x.org', 'v2-1@x.org',
                                       'v2-2@x.org', 'r1@x.org')])

    def test_thanks(self):
        """The thanks reply to the cover letter, listing the patches"""
        root = self.make_series()
        self.assertEqual(0, self.apply(root)[0])
        commits = self.git('log', '--reverse', '--format=%h',
                           '--abbrev=12', 'us/main..main-push').split()

        ret, out, made = self.thank()
        self.assertEqual(0, ret, out)
        self.assertEqual("Drafted thanks for dtc: Tidy\n", out)
        self.assertEqual(1, len(made))
        env, text = made[0]
        self.assertEqual(('Re: [PATCH 0/2] dtc: Tidy', '<fred-0@x.org>',
                          'fred@x.org'),
                         (env['subject'], env['in_reply_to'], env['to']))
        self.assertEqual(
            'Hi Fred,\n\n'
            'On 2026-10-27T08:38:38, Fred Bloggs <fred@x.org> wrote:\n'
            '> dtc: Tidy\n>\n> Some tidy-ups\n\n'
            'Applied to main, thanks.\n\n'
            f'[1/2] dtc: Add a file\n      {commits[0]}\n'
            f'[2/2] dtc: Grow the file\n      {commits[1]}\n\n'
            'Regards,\nSimon\n', text)

        # A series is thanked once, and what was thanked is noted
        ret, out, made = self.thank()
        self.assertEqual((0, 'No series to thank\n', []), (ret, out, made))
        self.assertEqual(
            {'last': self.git('rev-parse', 'main-push').strip(),
             'thanked': [root], 'drafts': {root: ['draft0']}},
            maint_thanks.read_state(self.repo))

        # Unless it is redrafted, which replaces the drafts
        ret, out, made = self.thank(redraft=True)
        self.assertEqual((0, 'Drafted thanks for dtc: Tidy\n', 1),
                         (ret, out, len(made)))
        self.assertEqual(['draft0'], self.deleted)
        self.assertEqual({root: ['draft1']},
                         maint_thanks.read_state(self.repo)['drafts'])

        # By default, the commits are those which went upstream since
        self.git('update-ref', 'refs/remotes/us/main', 'main-push')
        self.assertEqual((0, 'No series to thank\n', []),
                         self.thank(None))

    def test_series(self):
        """A series is thanked by itself, by default the current branch's,
        even if it is your own"""
        root = self.make_series(ME)
        self.apply(root)
        name = maint.current_branch(self.repo)

        # With no series on the branch, the commits since last time are used
        with self.assertRaises(ValueError) as exc:
            self.thank(None, series=True)
        self.assertIn('No thanks have been drafted here yet', str(exc.exception))

        cser = cseries.Cseries(self.tmpdir)
        cser.open_database()
        cser.db.ser_ver_add(cser.db.series_add(name, 'Tidy'), 1, link=root)
        cser.db.commit()
        cser.close_database()
        found = f"Series '{name}' v1: {root}\n"
        self.assertEqual((1, found + "'dtc: Tidy (v1)' is not in us/main yet\n",
                          []), self.thank(None, series=True))

        self.git('update-ref', 'refs/remotes/us/main', 'main-push')
        commits = self.git('log', '--reverse', '--format=%h', '--abbrev=12',
                           'HEAD..main-push').split()
        ret, out, made = self.thank(None, series=True)
        self.assertEqual((0, found + 'Drafted thanks for dtc: Tidy\n'),
                         (ret, out))
        self.assertEqual(1, len(made))
        # There is no one to greet or thank
        self.assertTrue(made[0][1].startswith(
            'On 2026-10-27T08:38:38, Simon Glass <sjg@chromium.org> wrote:\n'
            '> dtc: Tidy\n>\n> Some tidy-ups\n\n'
            f'Applied to main.\n\n[1/2] dtc: Add a file\n      {commits[0]}\n'
            f'[2/2] dtc: Grow the file\n      {commits[1]}\n'), made[0][1])
        self.assertEqual([root],
                         maint_thanks.read_state(self.repo)['thanked'])

        self.assertEqual({root: ['draft0']},
                         maint_thanks.read_state(self.repo)['drafts'])

        # It is thanked only once; a Message-ID may be given instead
        self.assertEqual((0, "Thanks for 'dtc: Tidy (v1)' were drafted "
                          'already: use --redraft to draft them again\n', []),
                         self.thank(root, series=True))

        # Redrafting replaces the drafts
        ret, out, made = self.thank(root, series=True, redraft=True)
        self.assertEqual((0, 'Drafted thanks for dtc: Tidy\n', 1),
                         (ret, out, len(made)))
        self.assertEqual(['draft0'], self.deleted)
        state = maint_thanks.read_state(self.repo)
        self.assertEqual(([root], {root: ['draft1']}),
                         (state['thanked'], state['drafts']))

    def test_each(self):
        """The thanks may reply to each patch"""
        root = self.make_series(cover=False)
        self.apply(root)
        ret, out, made = self.thank(values={'thanks_to': 'each'})
        self.assertEqual(0, ret, out)
        self.assertEqual(
            [('Re: [PATCH 1/2] dtc: Add a file', '<fred-1@x.org>'),
             ('Re: [PATCH 2/2] dtc: Grow the file', '<fred-2@x.org>')],
            [(env['subject'], env['in_reply_to']) for env, _ in made])
        self.assertIn('[2/2] dtc: Grow the file\n', made[1][1])
        self.assertNotIn('[1/2]', made[1][1])

        # Each reply quotes its patch, with the diffstat
        self.assertIn(
            'wrote:\n> dtc: Grow the file\n>\n> Do it\n>\n'
            f'> Signed-off-by: {FRED}\n>\n'
            '>  a.txt | 1 +\n>  1 file changed, 1 insertion(+)\n\n'
            'Applied to main, thanks.\n', made[1][1])

    def test_dry_run(self):
        """A dry run shows the thanks, and notes nothing"""
        self.apply(self.make_series())
        ret, out, made = self.thank(dry_run=True)
        self.assertEqual((0, []), (ret, made))
        self.assertIn('To: fred@x.org\nCc: \n'
                      'Subject: Re: [PATCH 0/2] dtc: Tidy\n\nHi Fred,\n', out)
        self.assertEqual({'last': None, 'thanked': [], 'drafts': {}},
                         maint_thanks.read_state(self.repo))

    def test_skipped(self):
        """Your own series are not thanked, nor are commits not from the
        list"""
        self.apply(self.make_series(ME))
        self.assertEqual(2, len(self.trailers('main-push')))
        self.assertEqual((0, 'No series to thank\n', []), self.thank())

        self.commit('Direct', ME, [f'Signed-off-by: {ME}'])
        self.assertEqual((0, 'No series to thank\n', []), self.thank(
            'us/main..main'))

    def test_problems(self):
        """The thanks are noted only once they are all drafted"""
        self.apply(self.make_series())
        ret, out, made = self.thank(drafted=0)
        self.assertEqual((1, 'Drafted 0 of 1 thanks\n', 1),
                         (ret, out, len(made)))
        self.assertEqual({'last': None, 'thanked': [], 'drafts': {}},
                         maint_thanks.read_state(self.repo))

        with self.assertRaises(ValueError) as exc:
            self.thank(None)
        self.assertEqual("No thanks have been drafted here yet: give the "
                         "series or commits to thank, e.g. 'us/main~5..us/main'",
                         str(exc.exception))

        # A problem drafting the thanks after publishing is reported
        with mock.patch.object(control, 'maint_list',
                               side_effect=ValueError('no list')), \
                terminal.capture() as (out, _):
            control._thank_published(  # pylint: disable=W0212
                mock.Mock(signoff=None), self.rules(), 'a' * 40, 'b' * 40)
        self.assertEqual('Cannot draft the thanks: no list; to try again: '
                         f"patman maint thanks {'a' * 12}..{'b' * 12}\n",
                         out.getvalue())
