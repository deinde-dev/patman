# SPDX-License-Identifier: GPL-2.0+

# Copyright 2025 Simon Glass <sjg@chromium.org>
#
"""Tests for the status of series on patchwork"""

import asyncio
from unittest import mock

import pygit2

from u_boot_pylib import terminal
from patman import control
from patman import patchstream
from patman.patchwork import Patchwork
from patman.test_common import CseriesTester, Namespace

HASH_RE = r'[0-9a-f]+'
#pylint: disable=protected-access


class TestSeriesStatus(CseriesTester):
    """Tests for gathering and showing the status of series"""
    def check_series_list_patches(self):
        """Test listing the patches for a series"""
        cser = self.get_cser()

        with self.stage('setup'):
            with terminal.capture() as (out, _):
                cser.add(None, '', allow_unmarked=True)
                cser.add('second', allow_unmarked=True)
                target = self.repo.lookup_reference('refs/heads/second')
                self.repo.checkout(
                    target, strategy=pygit2.enums.CheckoutStrategy.FORCE)
                cser.increment('second')

        with self.stage('list first'):
            with terminal.capture() as (out, _):
                yield cser
            itr = iter(out.getvalue().splitlines())
            self.assertEqual("Branch 'first' (total 2): 2:unknown", next(itr))
            self.assertIn('PatchId', next(itr))
            self.assertRegex(next(itr), r'  0 .* i2c: I2C things')
            self.assertRegex(next(itr), r'  1 .* spi: SPI fixes')

        with self.stage('list second2'):
            with terminal.capture() as (out, _):
                yield cser
            itr = iter(out.getvalue().splitlines())
            self.assertEqual(
                "Branch 'second2' (total 3): 3:unknown", next(itr))
            self.assertIn('PatchId', next(itr))
            self.assertRegex(
                next(itr), '  0 .* video: Some video improvements')
            self.assertRegex(next(itr), '  1 .* serial: Add a serial driver')
            self.assertRegex(next(itr), '  2 .* bootm: Make it boot')

        yield None

    def test_series_list_patches(self):
        """Test listing the patches for a series"""
        cor = self.check_series_list_patches()
        cser = next(cor)

        # list first
        cser.list_patches('first', 1)
        cser = next(cor)

        # list second2
        cser.list_patches('second2', 2)
        self.assertFalse(next(cor))
        cor.close()

    def test_series_list_patches_cmdline(self):
        """Test listing the patches for a series using the cmdline"""
        cor = self.check_series_list_patches()
        next(cor)

        # list first
        self.run_args('series',  '-s', 'first', 'patches', pwork=True)
        next(cor)

        # list second2
        self.run_args('series',  '-s', 'second', '-V', '2', 'patches',
                      pwork=True)
        self.assertFalse(next(cor))
        cor.close()

    def test_series_list_patches_detail(self):
        """Test listing the patches for a series"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add(None, '', allow_unmarked=True)
            cser.add('second', allow_unmarked=True)
            target = self.repo.lookup_reference('refs/heads/second')
            self.repo.checkout(
                target, strategy=pygit2.enums.CheckoutStrategy.FORCE)
            cser.increment('second')

        with terminal.capture() as (out, _):
            cser.list_patches('first', 1, show_commit=True)
        expect = r'''Branch 'first' (total 2): 2:unknown
Seq State      Com PatchId Commit     Subject
  0 unknown      -         .* i2c: I2C things

commit .*
Author: Test user <test@email.com>
Date:   .*

    i2c: I2C things

    This has some stuff to do with I2C

 i2c.c | 2 ++
 1 file changed, 2 insertions(+)


  1 unknown      -         .* spi: SPI fixes

commit .*
Author: Test user <test@email.com>
Date:   .*

    spi: SPI fixes

    SPI needs some fixes
    and here they are

    Signed-off-by: Lord Edmund Blackaddër <weasel@blackadder.org>

    Series-to: u-boot
    Commit-notes:
    title of the series
    This is the cover letter for the series
    with various details
    END

 spi.c | 3 +++
 1 file changed, 3 insertions(+)
'''
        itr = iter(out.getvalue().splitlines())
        for seq, eline in enumerate(expect.splitlines()):
            line = next(itr).rstrip()
            if '*' in eline:
                self.assertRegex(line, eline, f'line {seq + 1}')
            else:
                self.assertEqual(eline, line, f'line {seq + 1}')

        # Show just the patch; this should exclude the commit message
        with terminal.capture() as (out, _):
            cser.list_patches('first', 1, show_patch=True)
        chk = out.getvalue()
        self.assertIn('SPI fixes', chk)                 # subject
        self.assertNotIn('SPI needs some fixes', chk)   # commit body
        self.assertIn('make SPI work', chk)             # patch body

        # Show both
        with terminal.capture() as (out, _):
            cser.list_patches('first', 1, show_commit=True, show_patch=True)
        chk = out.getvalue()
        self.assertIn('SPI fixes', chk)                 # subject
        self.assertIn('SPI needs some fixes', chk)   # commit body
        self.assertIn('make SPI work', chk)             # patch body

    def check_series_gather(self):
        """Checker for gathering tags for a series"""
        cser = self.get_cser()
        with self.stage('setup'):
            pwork = Patchwork.for_testing(self._fake_patchwork_cser)
            self.assertFalse(cser.project_get())
            cser.project_set(pwork, 'U-Boot', quiet=True)

            with terminal.capture() as (out, _):
                cser.add('second', 'description', allow_unmarked=True)

            ser = cser.get_series_by_name('second')
            pwid = cser.get_series_svid(ser.idnum, 1)

        # First do a dry run
        with self.stage('gather: dry run'):
            with terminal.capture() as (out, _):
                yield cser, pwork
            lines = out.getvalue().splitlines()
            self.assertEqual(
                f"Updating series 'second' version 1 from link "
                f"'{self.SERIES_ID_SECOND_V1}'",
                lines[0])
            self.assertEqual('3 patches and cover letter updated (8 requests)', lines[1])
            self.assertEqual('Dry run completed', lines[2])
            self.assertEqual(3, len(lines))

            pwc = cser.get_pcommit_dict(pwid)
            self.assertIsNone(pwc[0].state)
            self.assertIsNone(pwc[1].state)
            self.assertIsNone(pwc[2].state)

        # Now try it again, gathering tags
        with self.stage('gather: dry run'):
            with terminal.capture() as (out, _):
                yield cser, pwork
            lines = out.getvalue().splitlines()
            itr = iter(lines)
            self.assertEqual(
                f"Updating series 'second' version 1 from link "
                f"'{self.SERIES_ID_SECOND_V1}'",
                next(itr))
            self.assertEqual('  1 video: Some video improvements', next(itr))
            self.assertEqual('  + Reviewed-by: Fred Bloggs <fred@bloggs.com>',
                             next(itr))
            self.assertEqual('  2 serial: Add a serial driver', next(itr))
            self.assertEqual('  3 bootm: Make it boot', next(itr))

            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual("Processing 3 commits from branch 'second'",
                             next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:       {HASH_RE} as {HASH_RE} '
                'video: Some video improvements')
            self.assertRegex(
                next(itr),
                f"- upd links '1:456': {HASH_RE} as {HASH_RE} "
                'serial: Add a serial driver')
            self.assertRegex(
                next(itr),
                f'-                    {HASH_RE} as {HASH_RE} '
                'bootm: Make it boot')
            self.assertRegex(
                next(itr),
                f'Updating branch second from {HASH_RE} to {HASH_RE}')
            self.assertEqual('3 patches and cover letter updated (8 requests)', next(itr))
            self.assertEqual('Dry run completed', next(itr))
            self.assert_finished(itr)

            # Make sure that no tags were added to the branch
            series = patchstream.get_metadata_for_list('second', self.gitdir,
                                                       3)
            for cmt in series.commits:
                self.assertFalse(cmt.rtags,
                                 'Commit {cmt.subject} rtags {cmt.rtags}')

        # Now do it for real
        with self.stage('gather: real'):
            with terminal.capture() as (out, _):
                yield cser, pwork
            lines2 = out.getvalue().splitlines()
            self.assertEqual(lines2, lines[:-1])

            # Make sure that the tags were added to the branch
            series = patchstream.get_metadata_for_list('second', self.gitdir,
                                                       3)
            self.assertEqual(
                {'Reviewed-by': {'Fred Bloggs <fred@bloggs.com>'}},
                series.commits[0].rtags)
            self.assertFalse(series.commits[1].rtags)
            self.assertFalse(series.commits[2].rtags)

            # Make sure the status was updated
            pwc = cser.get_pcommit_dict(pwid)
            self.assertEqual('accepted', pwc[0].state)
            self.assertEqual('changes-requested', pwc[1].state)
            self.assertEqual('rejected', pwc[2].state)

        yield None

    def test_series_gather(self):
        """Test gathering tags for a series"""
        cor = self.check_series_gather()
        cser, pwork = next(cor)

        # sync (dry_run)
        cser.gather(pwork, 'second', None, False, False, False, dry_run=True)
        cser, pwork = next(cor)

        # gather (dry_run)
        cser.gather(pwork, 'second', None, False, False, True, dry_run=True)
        cser, pwork = next(cor)

        # gather (real)
        cser.gather(pwork, 'second', None, False, False, True)

        self.assertFalse(next(cor))

    def test_series_gather_cmdline(self):
        """Test gathering tags for a series with cmdline"""
        cor = self.check_series_gather()
        _, pwork = next(cor)

        # sync (dry_run)
        self.run_args(
            'series', '-n', '-s', 'second', 'gather', '-G', pwork=pwork)

        # gather (dry_run)
        _, pwork = next(cor)
        self.run_args('series', '-n', '-s', 'second', 'gather', pwork=pwork)

        # gather (real)
        _, pwork = next(cor)
        self.run_args('series', '-s', 'second', 'gather', pwork=pwork)

        self.assertFalse(next(cor))

    def check_series_gather_all(self):
        """Gather all series at once"""
        with self.stage('setup'):
            cser, pwork = self.setup_second(False)

            with terminal.capture():
                cser.add('first', 'description', allow_unmarked=True)
                cser.increment('first')
                cser.increment('first')
                cser.link_set('first', 1, '123', True)
                cser.link_set('first', 2, '1234', True)
                cser.link_set('first', 3, f'{self.SERIES_ID_FIRST_V3}', True)
                cser.link_auto(pwork, 'second', 2, True)

        with self.stage('no options'):
            with terminal.capture() as (out, _):
                yield cser, pwork
            self.assertEqual(
                'Reading 2 series from https://my-fake-url\n'
                "Syncing 'first' v3\n"
                "Syncing 'second' v2\n"
                '\n'
                '5 patches and 2 cover letters updated, 0 missing links '
                '(14 requests)\n'
                'Dry run completed',
                out.getvalue().strip())

        with self.stage('gather'):
            with terminal.capture() as (out, _):
                yield cser, pwork
            lines = out.getvalue().splitlines()
            itr = iter(lines)
            self.assertEqual('Reading 2 series from https://my-fake-url',
                             next(itr))
            self.assertEqual("Syncing 'first' v3", next(itr))
            self.assertEqual('  1 i2c: I2C things', next(itr))
            self.assertEqual(
                '  + Tested-by: Mary Smith <msmith@wibble.com>   # yak',
                next(itr))
            self.assertEqual('  2 spi: SPI fixes', next(itr))
            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual(
                "Processing 2 commits from branch 'first3'", next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:      {HASH_RE} as {HASH_RE} i2c: I2C things')
            self.assertRegex(
                next(itr),
                f"- upd links '3:31': {HASH_RE} as {HASH_RE} spi: SPI fixes")
            self.assertRegex(
                next(itr),
                f'Updating branch first3 from {HASH_RE} to {HASH_RE}')
            self.assertEqual('', next(itr))

            self.assertEqual("Syncing 'second' v2", next(itr))
            self.assertEqual('  1 video: Some video improvements', next(itr))
            self.assertEqual(
                '  + Reviewed-by: Fred Bloggs <fred@bloggs.com>', next(itr))
            self.assertEqual('  2 serial: Add a serial driver', next(itr))
            self.assertEqual('  3 bootm: Make it boot', next(itr))
            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual(
                "Processing 3 commits from branch 'second2'", next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:             {HASH_RE} as {HASH_RE} '
                'video: Some video improvements')
            self.assertRegex(
                next(itr),
                f"- upd links '2:457 1:456': {HASH_RE} as {HASH_RE} "
                'serial: Add a serial driver')
            self.assertRegex(
                next(itr),
                f'-                          {HASH_RE} as {HASH_RE} '
                'bootm: Make it boot')
            self.assertRegex(
                next(itr),
                f'Updating branch second2 from {HASH_RE} to {HASH_RE}')
            self.assertEqual('', next(itr))
            self.assertEqual(
                '5 patches and 2 cover letters updated, 0 missing links '
                '(14 requests)',
                next(itr))
            self.assertEqual('Dry run completed', next(itr))
            self.assert_finished(itr)

        with self.stage('gather, patch comments,!dry_run'):
            with terminal.capture() as (out, _):
                yield cser, pwork
            lines = out.getvalue().splitlines()
            itr = iter(lines)
            self.assertEqual('Reading 5 series from https://my-fake-url',
                             next(itr))
            self.assertEqual("Syncing 'first' v1", next(itr))
            self.assertEqual('  1 i2c: I2C things', next(itr))
            self.assertEqual(
                '  + Tested-by: Mary Smith <msmith@wibble.com>   # yak',
                next(itr))
            self.assertEqual('  2 spi: SPI fixes', next(itr))
            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual(
                "Processing 2 commits from branch 'first'", next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:       {HASH_RE} as {HASH_RE} i2c: I2C things')
            self.assertRegex(
                next(itr),
                f"- upd links '1:123': {HASH_RE} as {HASH_RE} spi: SPI fixes")
            self.assertRegex(
                next(itr),
                f'Updating branch first from {HASH_RE} to {HASH_RE}')
            self.assertEqual('', next(itr))

            self.assertEqual("Syncing 'first' v2", next(itr))
            self.assertEqual('  1 i2c: I2C things', next(itr))
            self.assertEqual(
                '  + Tested-by: Mary Smith <msmith@wibble.com>   # yak',
                next(itr))
            self.assertEqual('  2 spi: SPI fixes', next(itr))
            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual(
                "Processing 2 commits from branch 'first2'", next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:        {HASH_RE} as {HASH_RE} '
                'i2c: I2C things')
            self.assertRegex(
                next(itr),
                f"- upd links '2:1234': {HASH_RE} as {HASH_RE} spi: SPI fixes")
            self.assertRegex(
                next(itr),
                f'Updating branch first2 from {HASH_RE} to {HASH_RE}')
            self.assertEqual('', next(itr))
            self.assertEqual("Syncing 'first' v3", next(itr))
            self.assertEqual('  1 i2c: I2C things', next(itr))
            self.assertEqual(
                '  + Tested-by: Mary Smith <msmith@wibble.com>   # yak',
                next(itr))
            self.assertEqual('  2 spi: SPI fixes', next(itr))
            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual(
                "Processing 2 commits from branch 'first3'", next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:      {HASH_RE} as {HASH_RE} i2c: I2C things')
            self.assertRegex(
                next(itr),
                f"- upd links '3:31': {HASH_RE} as {HASH_RE} spi: SPI fixes")
            self.assertRegex(
                next(itr),
                f'Updating branch first3 from {HASH_RE} to {HASH_RE}')
            self.assertEqual('', next(itr))

            self.assertEqual("Syncing 'second' v1", next(itr))
            self.assertEqual('  1 video: Some video improvements', next(itr))
            self.assertEqual(
                '  + Reviewed-by: Fred Bloggs <fred@bloggs.com>', next(itr))
            self.assertEqual(
                'Review: Fred Bloggs <fred@bloggs.com>', next(itr))
            self.assertEqual('    > This was my original patch', next(itr))
            self.assertEqual('    > which is being quoted', next(itr))
            self.assertEqual(
                '    I like the approach here and I would love to see more '
                'of it.', next(itr))
            self.assertEqual('', next(itr))
            self.assertEqual('  2 serial: Add a serial driver', next(itr))
            self.assertEqual('  3 bootm: Make it boot', next(itr))
            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual(
                "Processing 3 commits from branch 'second'", next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:       {HASH_RE} as {HASH_RE} '
                'video: Some video improvements')
            self.assertRegex(
                next(itr),
                f"- upd links '1:456': {HASH_RE} as {HASH_RE} "
                'serial: Add a serial driver')
            self.assertRegex(
                next(itr),
                f'-                    {HASH_RE} as {HASH_RE} '
                'bootm: Make it boot')
            self.assertRegex(
                next(itr),
                f'Updating branch second from {HASH_RE} to {HASH_RE}')
            self.assertEqual('', next(itr))

            self.assertEqual("Syncing 'second' v2", next(itr))
            self.assertEqual('  1 video: Some video improvements', next(itr))
            self.assertEqual(
                '  + Reviewed-by: Fred Bloggs <fred@bloggs.com>', next(itr))
            self.assertEqual(
                'Review: Fred Bloggs <fred@bloggs.com>', next(itr))
            self.assertEqual('    > This was my original patch', next(itr))
            self.assertEqual('    > which is being quoted', next(itr))
            self.assertEqual(
                '    I like the approach here and I would love to see more '
                'of it.', next(itr))
            self.assertEqual('', next(itr))
            self.assertEqual('  2 serial: Add a serial driver', next(itr))
            self.assertEqual('  3 bootm: Make it boot', next(itr))
            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual(
                "Processing 3 commits from branch 'second2'", next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:             {HASH_RE} as {HASH_RE} '
                'video: Some video improvements')
            self.assertRegex(
                next(itr),
                f"- upd links '2:457 1:456': {HASH_RE} as {HASH_RE} "
                'serial: Add a serial driver')
            self.assertRegex(
                next(itr),
                f'-                          {HASH_RE} as {HASH_RE} '
                'bootm: Make it boot')
            self.assertRegex(
                next(itr),
                f'Updating branch second2 from {HASH_RE} to {HASH_RE}')
            self.assertEqual('', next(itr))
            self.assertEqual(
                '12 patches and 3 cover letters updated, 0 missing links '
                '(32 requests)', next(itr))
            self.assert_finished(itr)

        yield None

    def test_series_gather_all(self):
        """Gather all series at once"""
        cor = self.check_series_gather_all()
        cser, pwork = next(cor)

        # no options
        cser.gather_all(pwork, False, True, False, False, dry_run=True)
        cser, pwork = next(cor)

        # gather
        cser.gather_all(pwork, False, False, False, True, dry_run=True)
        cser, pwork = next(cor)

        # gather, patch comments, !dry_run
        cser.gather_all(pwork, True, False, True, True)

        self.assertFalse(next(cor))

    def test_series_gather_all_cmdline(self):
        """Sync all series at once using cmdline"""
        cor = self.check_series_gather_all()
        _, pwork = next(cor)

        # no options
        self.run_args('series', '-n', '-s', 'second', 'gather-all', '-G',
                      pwork=pwork)
        _, pwork = next(cor)

        # gather
        self.run_args('series', '-n', '-s', 'second', 'gather-all',
                      pwork=pwork)
        _, pwork = next(cor)

        # gather, patch comments, !dry_run
        self.run_args('series',  '-s', 'second', 'gather-all', '-a', '-c',
                      pwork=pwork)

        self.assertFalse(next(cor))

    def setup_gather_two_servers(self):
        """Set up 'first' on a second patchwork server which is down

        'first' has upstream 'other', whose patchwork server raises an error
        for every request. 'second' has no upstream.

        Return: tuple:
            Cseries: New Cseries object
            Patchwork: Patchwork object for the normal server
            Patchwork: Patchwork object for the server which is down
        """
        cser, pwork = self.setup_second(False)

        def _down(_subpath):
            raise ValueError('Server is down')

        bad = Patchwork.for_testing(_down)
        bad.url = 'https://other-pw'
        with terminal.capture():
            cser.link_set('first', 1, f'{self.SERIES_ID_FIRST_V3}', True)
            cser.link_auto(pwork, 'second', 2, True)
            cser.upstream_add('other', 'https://other-repo',
                              patchwork_url='https://other-pw')
            cser.set_upstream('first', 'other')
        return cser, pwork, bad

    def test_series_gather_all_servers(self):
        """Gather series from each upstream's patchwork server"""
        cser, pwork, bad = self.setup_gather_two_servers()
        with mock.patch.object(cser, '_make_patchwork',
                               return_value=bad) as make:
            with terminal.capture() as (out, err):
                with self.assertRaises(ValueError) as exc:
                    cser.gather_all(pwork, False, False, False, False)
        make.assert_called_once_with('https://other-pw')
        self.assertEqual('Cannot read 1 series from patchwork',
                         str(exc.exception))
        self.assertEqual(
            "Cannot read 'first' v1 from https://other-pw: ValueError: "
            'Server is down', err.getvalue().strip())

        # 'second' is still read and updated
        itr = iter(out.getvalue().splitlines())
        self.assertEqual('Reading 1 series from https://other-pw', next(itr))
        self.assertEqual('Reading 1 series from https://my-fake-url',
                         next(itr))
        self.assertEqual("Syncing 'second' v2", next(itr))
        self.assertEqual('', next(itr))
        self.assertEqual(
            '3 patches and 1 cover letter updated, 0 missing links '
            '(9 requests)', next(itr))
        self.assert_finished(itr)

    def test_series_gather_project(self):
        """Each patchwork server's object has its upstream's project"""
        cser, pwork, bad = self.setup_gather_two_servers()
        cser.db.patchwork_update('Other', 7, 'other-proj', 'other')
        cser.commit()
        with mock.patch.object(cser, '_make_patchwork', return_value=bad):
            with terminal.capture():
                with self.assertRaises(ValueError):
                    cser.gather_all(pwork, False, False, False, False)
        self.assertEqual((7, 'other-proj'), (bad.proj_id, bad.link_name))

    def test_series_gather_all_default_ups(self):
        """Series with no upstream are read from the default upstream"""
        cser, pwork, bad = self.setup_gather_two_servers()
        with terminal.capture():
            cser.upstream_set_default('other')
        with mock.patch.object(cser, '_make_patchwork', return_value=bad):
            with terminal.capture() as (out, err):
                with self.assertRaises(ValueError) as exc:
                    cser.gather_all(pwork, False, False, False, False)
        self.assertEqual('Reading 2 series from https://other-pw',
                         out.getvalue().splitlines()[0])
        self.assertEqual(
            'Cannot read any of 2 series from https://other-pw: ValueError: '
            'Server is down', err.getvalue().strip())
        self.assertEqual('Cannot read 2 series from patchwork',
                         str(exc.exception))

    def _check_second(self, itr, show_all):
        """Check output from a 'progress' command

        Args:
            itr (Iterator): Contains the output lines to check
            show_all (bool): True if all versions are being shown, not just
                latest
        """
        self.assertEqual('second: Series for my board (versions: 1 2)',
                         next(itr))
        if show_all:
            self.assertEqual("Branch 'second' (total 3): 3:unknown",
                             next(itr))
            self.assertIn('PatchId', next(itr))
            self.assertRegex(
                next(itr),
                '  0 unknown      -         .* video: Some video improvements')
            self.assertRegex(
                next(itr),
                '  1 unknown      -         .* serial: Add a serial driver')
            self.assertRegex(
                next(itr),
                '  2 unknown      -         .* bootm: Make it boot')
            self.assertEqual('', next(itr))
        self.assertEqual(
            "Branch 'second2' (total 3): 1:accepted 1:changes 1:rejected",
            next(itr))
        self.assertIn('PatchId', next(itr))
        self.assertEqual(
            'Cov              2     139            '
            'The name of the cover letter', next(itr))
        self.assertRegex(
            next(itr),
            '  0 accepted     2     110 .* video: Some video improvements')
        self.assertRegex(
            next(itr),
            '  1 changes            111 .* serial: Add a serial driver')
        self.assertRegex(
            next(itr),
            '  2 rejected     3     112 .* bootm: Make it boot')

    def test_series_progress(self):
        """Test showing progress for a cseries"""
        self.setup_second()
        self.db_close()

        with self.stage('latest versions'):
            args = Namespace(subcmd='progress', series='second',
                             show_all_versions=False, list_patches=True,
                             include_archived=False)
            with terminal.capture() as (out, _):
                control.do_series(args, test_db=self.tmpdir, pwork=True)
            lines = iter(out.getvalue().splitlines())
            self._check_second(lines, False)

        with self.stage('all versions'):
            args.show_all_versions = True
            with terminal.capture() as (out, _):
                control.do_series(args, test_db=self.tmpdir, pwork=True)
            lines = iter(out.getvalue().splitlines())
            self._check_second(lines, True)

    def _check_first(self, itr):
        """Check output from the progress command

        Args:
            itr (Iterator): Contains the output lines to check
        """
        self.assertEqual('first:  (versions: 1)', next(itr))
        self.assertEqual("Branch 'first' (total 2): 2:unknown", next(itr))
        self.assertIn('PatchId', next(itr))
        self.assertRegex(
            next(itr),
            '  0 unknown      -        .* i2c: I2C things')
        self.assertRegex(
            next(itr),
            '  1 unknown      -        .* spi: SPI fixes')
        self.assertEqual('', next(itr))

    def test_series_progress_all(self):
        """Test showing progress for all cseries"""
        self.setup_second()
        self.db_close()

        with self.stage('progress with patches'):
            args = Namespace(subcmd='progress', series=None,
                             show_all_versions=False, list_patches=True,
                             include_archived=False)
            with terminal.capture() as (out, _):
                control.do_series(args, test_db=self.tmpdir, pwork=True)
            lines = iter(out.getvalue().splitlines())
            self._check_first(lines)
            self._check_second(lines, False)

        with self.stage('all versions'):
            args.show_all_versions = True
            with terminal.capture() as (out, _):
                control.do_series(args, test_db=self.tmpdir, pwork=True)
            lines = iter(out.getvalue().splitlines())
            self._check_first(lines)
            self._check_second(lines, True)

    def test_series_progress_all_archived(self):
        """Test showing progress for all cseries including archived ones"""
        self.setup_second()
        with terminal.capture():
            self.cser.archive('first')

        with self.stage('progress without archived'):
            with terminal.capture() as (out, _):
                self.run_args('series', 'progress', pwork=True)
            itr = iter(out.getvalue().splitlines())
            self.assertEqual(
                'Name             Description                               Count  Status',
                next(itr))
            self.assertTrue(next(itr).startswith('--'))
            self.assertEqual(
                'second2          The name of the cover letter              '
                '    3  1:accepted 1:changes 1:rejected', next(itr))

        with self.stage('progress with archived'):
            with terminal.capture() as (out, _):
                self.run_args('series', 'progress', '--include-archived',
                              pwork=True)
            lines = out.getvalue().splitlines()
            self.assertEqual(
                'first                                                      '
                '    2  2:unknown', lines[2])
            self.assertEqual(
                'second2          The name of the cover letter              '
                '    3  1:accepted 1:changes 1:rejected', lines[3])

    def test_series_progress_no_patches(self):
        """Test showing progress for all cseries without patches"""
        self.setup_second()

        with terminal.capture() as (out, _):
            self.run_args('series', 'progress', pwork=True)
        itr = iter(out.getvalue().splitlines())
        self.assertEqual(
            'Name             Description                               '
            'Count  Status', next(itr))
        self.assertTrue(next(itr).startswith('--'))
        self.assertEqual(
            'first                                                      '
            '    2  2:unknown', next(itr))
        self.assertEqual(
            'second2          The name of the cover letter              '
            '    3  1:accepted 1:changes 1:rejected', next(itr))
        self.assertTrue(next(itr).startswith('--'))
        self.assertEqual(
            ['2', 'series', '5', '2:unknown', '1:accepted', '1:changes',
             '1:rejected'],
            next(itr).split())
        self.assert_finished(itr)

    def test_series_progress_all_no_patches(self):
        """Test showing progress for all cseries versions without patches"""
        self.setup_second()

        with terminal.capture() as (out, _):
            self.run_args('series', 'progress', '--show-all-versions',
                          pwork=True)
        itr = iter(out.getvalue().splitlines())
        self.assertEqual(
            'Name             Description                               '
            'Count  Status', next(itr))
        self.assertTrue(next(itr).startswith('--'))
        self.assertEqual(
            'first                                                      '
            '    2  2:unknown', next(itr))
        self.assertEqual(
            'second           Series for my board                       '
            '    3  3:unknown', next(itr))
        self.assertEqual(
            'second2          The name of the cover letter              '
            '    3  1:accepted 1:changes 1:rejected', next(itr))
        self.assertTrue(next(itr).startswith('--'))
        self.assertEqual(
            ['3', 'series', '8', '5:unknown', '1:accepted', '1:changes',
             '1:rejected'],
            next(itr).split())
        self.assert_finished(itr)

    def test_series_summary(self):
        """Test showing a summary of series status"""
        self.setup_second()

        self.db_close()
        args = Namespace(subcmd='summary', series=None)
        with terminal.capture() as (out, _):
            control.do_series(args, test_db=self.tmpdir, pwork=True)
        lines = out.getvalue().splitlines()
        self.assertEqual(
            'Name               Status  Description',
            lines[0])
        self.assertEqual(
            '-----------------  ------  ------------------------------',
            lines[1])
        # The status column lines up with the 'Status' heading
        self.assertEqual('first                 -/2  ', lines[2])
        self.assertEqual('second                1/3  Series for my board',
                         lines[3])
        self.assertEqual(lines[0].index('Status') + len('Status'),
                         lines[3].index('1/3') + len('1/3'))

    def _check_status(self, out, has_comments, has_cover_comments):
        """Check output from the status command

        Args:
            itr (Iterator): Contains the output lines to check
        """
        itr = iter(out.getvalue().splitlines())
        if has_cover_comments:
            self.assertEqual('Cov The name of the cover letter', next(itr))
            self.assertEqual(
                'From: A user <user@user.com>: Sun 13 Apr 14:06:02 MDT 2025',
                next(itr))
            self.assertEqual('some comment', next(itr))
            self.assertEqual('', next(itr))

            self.assertEqual(
                'From: Ghenkis Khan <gk@eurasia.gov>: Sun 13 Apr 13:06:02 '
                'MDT 2025',
                next(itr))
            self.assertEqual('another comment', next(itr))
            self.assertEqual('', next(itr))

        self.assertEqual('  1 video: Some video improvements', next(itr))
        self.assertEqual('  + Reviewed-by: Fred Bloggs <fred@bloggs.com>',
                         next(itr))
        if has_comments:
            self.assertEqual(
                'Review: Fred Bloggs <fred@bloggs.com>', next(itr))
            self.assertEqual('    > This was my original patch', next(itr))
            self.assertEqual('    > which is being quoted', next(itr))
            self.assertEqual(
                '    I like the approach here and I would love to see more '
                'of it.', next(itr))
            self.assertEqual('', next(itr))

        self.assertEqual('  2 serial: Add a serial driver', next(itr))
        self.assertEqual('  3 bootm: Make it boot', next(itr))
        self.assertEqual(
            '1 new response available in patchwork (use -d to write them to '
            'a new branch)', next(itr))

    def test_series_status(self):
        """Test getting the status of a series, including comments"""
        cser, pwork = self.setup_second()

        # Use single threading for easy debugging, but the multithreaded
        # version should produce the same output
        with self.stage('status second2: single-threaded'):
            with terminal.capture() as (out, _):
                cser.status(pwork, 'second', 2, False)
            self._check_status(out, False, False)
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

        with self.stage('status second2 (normal)'):
            with terminal.capture() as (out2, _):
                cser.status(pwork, 'second', 2, False)
            self.assertEqual(out.getvalue(), out2.getvalue())
            self._check_status(out, False, False)

        with self.stage('with comments'):
            with terminal.capture() as (out, _):
                cser.status(pwork, 'second', 2, show_comments=True)
            self._check_status(out, True, False)

        with self.stage('with comments and cover comments'):
            with terminal.capture() as (out, _):
                cser.status(pwork, 'second', 2, show_comments=True,
                            show_cover_comments=True)
            self._check_status(out, True, True)

    def test_series_status_gather_archived(self):
        """status and gather should still work on an archived series"""
        cser, pwork = self.setup_second()
        with terminal.capture():
            cser.archive('second')

        # status reads the patches from the archive tag, since archiving
        # deletes the branch
        with terminal.capture() as (out, _):
            cser.status(pwork, 'second', 2, False)
        self._check_status(out, False, False)

        # gather works from patchwork and the database, with no branch
        with terminal.capture() as (out, _):
            cser.gather(pwork, 'second', 2, False, False, True, dry_run=True)
        self.assertIn('updated', out.getvalue())

    def test_series_status_cmdline(self):
        """Test getting the status of a series, including comments"""
        cser, pwork = self.setup_second()

        with self.stage('status second2'):
            with terminal.capture() as (out, _):
                self.run_args('series', '-s', 'second', '-V', '2', 'status',
                              pwork=pwork)
            self._check_status(out, False, False)

        with self.stage('status second2 (normal)'):
            with terminal.capture() as (out, _):
                cser.status(pwork, 'second', 2, show_comments=True)
            self._check_status(out, True, False)

        with self.stage('with comments and cover comments'):
            with terminal.capture() as (out, _):
                cser.status(pwork, 'second', 2, show_comments=True,
                                   show_cover_comments=True)
            self._check_status(out, True, True)
