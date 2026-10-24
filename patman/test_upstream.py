# SPDX-License-Identifier: GPL-2.0+

# Copyright 2025 Simon Glass <sjg@chromium.org>
#
"""Tests for upstreams and patchwork projects"""

import os
import time
import types
from unittest import mock

from u_boot_pylib import command
from u_boot_pylib import terminal
from patman import control
from patman import database
from patman import pwcache
from patman import traffic
from patman.patchwork import Patchwork
from patman.test_common import CseriesTester

#pylint: disable=protected-access


class TestUpstream(CseriesTester):  # pylint: disable=R0904
    """Tests for 'patman upstream' and 'patman patchwork'"""
    def test_upstream_add(self):
        """Test adding an upsream"""
        cser = self.get_cser()

        with terminal.capture():
            cser.upstream_add('us', 'https://one')
        ulist = cser.get_upstream_dict()
        self.assertEqual(1, len(ulist))
        self.assertEqual(('https://one', None, None, None, None, 0, 0, None), ulist['us'])

        with terminal.capture():
            cser.upstream_add('ci', 'git@two')
        ulist = cser.get_upstream_dict()
        self.assertEqual(2, len(ulist))
        self.assertEqual(('https://one', None, None, None, None, 0, 0, None), ulist['us'])
        self.assertEqual(('git@two', None, None, None, None, 0, 0, None), ulist['ci'])

        # Try to add a duplicate
        with self.assertRaises(ValueError) as exc:
            cser.upstream_add('ci', 'git@three')
        self.assertEqual("Upstream 'ci' already exists", str(exc.exception))

        with terminal.capture() as (out, _):
            cser.upstream_list()
        lines = out.getvalue().splitlines()
        self.assertEqual(4, len(lines))
        self.assertIn('Name', lines[0])
        self.assertIn('https://one', lines[2])
        self.assertIn('git@two', lines[3])

    def test_upstream_add_patchwork_url(self):
        """Test adding an upstream with a patchwork URL"""
        cser = self.get_cser()

        with terminal.capture():
            cser.upstream_add('us', 'https://one',
                              patchwork_url='https://pw.example.com')
        ulist = cser.get_upstream_dict()
        self.assertEqual(1, len(ulist))
        self.assertEqual(
            ('https://one', None, 'https://pw.example.com', None, None, 0, 0,
             None), ulist['us'])

        # Check that the patchwork URL shows in the list
        with terminal.capture() as (out, _):
            cser.upstream_list()
        lines = out.getvalue().splitlines()
        self.assertEqual(3, len(lines))
        self.assertIn('pw:https://pw.example.com', lines[2])

        # Check database lookup
        pw_url = cser.db.upstream_get_patchwork_url('us')
        self.assertEqual('https://pw.example.com', pw_url)

        # Non-existent upstream returns None
        pw_url = cser.db.upstream_get_patchwork_url('nonexistent')
        self.assertIsNone(pw_url)

    def test_upstream_add_cmdline(self):
        """Test adding an upsream with cmdline"""
        with terminal.capture():
            self.run_args('upstream', 'add', 'us', 'https://one')

        with terminal.capture() as (out, _):
            self.run_args('upstream', 'list')
        lines = out.getvalue().splitlines()
        self.assertEqual(3, len(lines))
        self.assertIn('us', lines[2])
        self.assertIn('https://one', lines[2])

    def test_upstream_lore_url(self):
        """An upstream can have its mailing list on lore"""
        lore_url = 'https://lore.kernel.org/devicetree-compiler'
        with terminal.capture() as (out, _):
            self.run_args('upstream', 'add', 'dtc', 'https://dtc.git',
                          '-l', lore_url)
        self.assertIn(f"lore '{lore_url}'", out.getvalue())
        with terminal.capture() as (out, _):
            self.run_args('upstream', 'list')
        self.assertIn(f'lore:{lore_url}', out.getvalue())

        with terminal.capture():
            self.run_args('upstream', 'add', 'us', 'https://one')
            self.run_args('upstream', 'set', 'us', '-l', 'https://lore.x/y')
        cser = self.get_cser()
        self.assertEqual(lore_url, cser.db.upstream_get_lore_url('dtc'))
        self.assertEqual('https://lore.x/y',
                         cser.db.upstream_get_lore_url('us'))
        self.assertIsNone(cser.db.upstream_get_patchwork_url('dtc'))
        self.assertIsNone(cser.db.upstream_get_lore_url('nonexistent'))

    def test_upstream_set(self):
        """Test updating settings on an existing upstream"""
        cser = self.get_cser()

        with terminal.capture():
            cser.upstream_add('us', 'https://one')

        # Set identity and series_to
        with terminal.capture():
            cser.upstream_set('us', identity='chromium', series_to='concept')
        settings = cser.db.upstream_get_send_settings('us')
        self.assertEqual('chromium', settings[0])
        self.assertEqual('concept', settings[1])

        # Set boolean flags
        with terminal.capture():
            cser.upstream_set('us', no_maintainers=True, no_tags=True)
        settings = cser.db.upstream_get_send_settings('us')
        self.assertTrue(settings[2])
        self.assertTrue(settings[3])

        # Clear boolean flags
        with terminal.capture():
            cser.upstream_set('us', no_maintainers=False, no_tags=False)
        settings = cser.db.upstream_get_send_settings('us')
        self.assertFalse(settings[2])
        self.assertFalse(settings[3])

        # Non-existent upstream
        with self.assertRaises(ValueError) as exc:
            cser.upstream_set('nonexistent', identity='x')
        self.assertIn('nonexistent', str(exc.exception))

    def test_upstream_set_cmdline(self):
        """Test upstream set via the command line"""
        with terminal.capture():
            self.run_args('upstream', 'add', 'us', 'https://one')

        with terminal.capture():
            self.run_args('upstream', 'set', 'us', '-I', 'chromium',
                          '-t', 'concept', '-m', '--no-tags')

        with terminal.capture() as (out, _):
            self.run_args('upstream', 'list')
        line = out.getvalue().strip()
        self.assertIn('id:chromium', line)
        self.assertIn('to:concept', line)
        self.assertIn('no-maintainers', line)
        self.assertIn('no-tags', line)
        self.assertNotIn('no-feed', line)

    def test_upstream_no_feed(self):
        """An upstream's inbox can list only the series added to it"""
        with terminal.capture() as (out, _):
            self.run_args('upstream', 'add', 'linux', 'https://linux', '-l',
                          'https://lore.kernel.org/all', '--no-feed')
        self.assertIn('no-feed', out.getvalue())

        def listed():
            with terminal.capture() as (out, _):
                self.run_args('upstream', 'list')
            return 'no-feed' in out.getvalue()

        self.assertTrue(listed())
        with terminal.capture():
            self.run_args('upstream', 'set', 'linux', '--feed')
        self.assertFalse(listed())
        with terminal.capture():
            self.run_args('upstream', 'set', 'linux', '--no-feed')
        self.assertTrue(listed())

    def test_upstream_default(self):
        """Operation of the default upstream"""
        cser = self.get_cser()

        with self.assertRaises(ValueError) as exc:
            cser.upstream_set_default('us')
        self.assertEqual("No such upstream 'us'", str(exc.exception))

        with terminal.capture():
            cser.upstream_add('us', 'https://one')
            cser.upstream_add('ci', 'git@two')

        self.assertIsNone(cser.upstream_get_default())

        with terminal.capture():
            cser.upstream_set_default('us')
        self.assertEqual('us', cser.upstream_get_default())

        with terminal.capture():
            cser.upstream_set_default('us')

        with terminal.capture():
            cser.upstream_set_default('ci')
        self.assertEqual('ci', cser.upstream_get_default())

        with terminal.capture() as (out, _):
            cser.upstream_list()
        lines = out.getvalue().splitlines()
        self.assertEqual(4, len(lines))
        self.assertNotIn('*', lines[2])
        self.assertIn('*', lines[3])

        cser.upstream_set_default(None)
        self.assertIsNone(cser.upstream_get_default())

    def test_upstream_default_cmdline(self):
        """Operation of the default upstream on cmdline"""
        with terminal.capture() as (out, _):
            self.run_args('upstream', 'default', 'us', expect_ret=1)
        self.assertEqual("patman: ValueError: No such upstream 'us'",
                         out.getvalue().strip().splitlines()[-1])

        with terminal.capture():
            self.run_args('upstream', 'add', 'us', 'https://one')
            self.run_args('upstream', 'add', 'ci', 'git@two')

        with terminal.capture() as (out, _):
            self.run_args('upstream', 'default')
        self.assertEqual('unset', out.getvalue().strip())

        with terminal.capture():
            self.run_args('upstream', 'default', 'us')
        with terminal.capture() as (out, _):
            self.run_args('upstream', 'default')
        self.assertEqual('us', out.getvalue().strip())

        with terminal.capture():
            self.run_args('upstream', 'default', 'ci')
        with terminal.capture() as (out, _):
            self.run_args('upstream', 'default')
        self.assertEqual('ci', out.getvalue().strip())

        with terminal.capture() as (out, _):
            self.run_args('upstream', 'default', '--unset')
        self.assertFalse(out.getvalue().strip())

        with terminal.capture() as (out, _):
            self.run_args('upstream', 'default')
        self.assertEqual('unset', out.getvalue().strip())

    def test_upstream_delete(self):
        """Test operation of the default upstream"""
        cser = self.get_cser()

        with self.assertRaises(ValueError) as exc:
            cser.upstream_delete('us')
        self.assertEqual("No such upstream 'us'", str(exc.exception))

        with terminal.capture():
            cser.upstream_add('us', 'https://one')
            cser.upstream_add('ci', 'git@two')

        with terminal.capture():
            cser.upstream_set_default('us')
            cser.upstream_delete('us')
        self.assertIsNone(cser.upstream_get_default())

        with terminal.capture():
            cser.upstream_delete('ci')
        ulist = cser.get_upstream_dict()
        self.assertFalse(ulist)

    def test_upstream_delete_cmdline(self):
        """Test deleting an upstream"""
        with terminal.capture() as (out, _):
            self.run_args('upstream', 'delete', 'us', expect_ret=1)
        self.assertEqual("patman: ValueError: No such upstream 'us'",
                         out.getvalue().strip().splitlines()[-1])

        with terminal.capture():
            self.run_args('us', 'add', 'us', 'https://one')
            self.run_args('us', 'add', 'ci', 'git@two')

        with terminal.capture():
            self.run_args('upstream', 'default', 'us')
            self.run_args('upstream', 'delete', 'us')
        with terminal.capture() as (out, _):
            self.run_args('upstream', 'default', 'us', expect_ret=1)
        self.assertEqual("patman: ValueError: No such upstream 'us'",
                         out.getvalue().strip())

        with terminal.capture():
            self.run_args('upstream', 'delete', 'ci')
        with terminal.capture() as (out, _):
            self.run_args('upstream', 'list')
        lines = out.getvalue().splitlines()
        self.assertEqual(2, len(lines))

    def test_series_upstream(self):
        """Test upstream field in the series table"""
        cser = self.get_cser()

        # Add a series without upstream
        cser.db.series_add('first', 'my desc')
        cser.db.commit()
        slist = cser.db.series_get_dict()
        self.assertIsNone(slist['first'].upstream)

        # Add a series with upstream
        cser.db.series_add('second', 'desc2', ups='us')
        cser.db.commit()
        slist = cser.db.series_get_dict()
        self.assertIsNone(slist['first'].upstream)
        self.assertEqual('us', slist['second'].upstream)

        # Update upstream on existing series
        idnum = cser.db.series_find_by_name('first')
        cser.db.series_set_upstream(idnum, 'ci')
        cser.db.commit()
        slist = cser.db.series_get_dict()
        self.assertEqual('ci', slist['first'].upstream)

        # Clear upstream
        cser.db.series_set_upstream(idnum, None)
        cser.db.commit()
        slist = cser.db.series_get_dict()
        self.assertIsNone(slist['first'].upstream)

    def test_series_set_upstream(self):
        """Test setting upstream via the set-upstream command"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', '', allow_unmarked=True)

        self.db_close()
        with terminal.capture() as (out, _):
            self.run_args('series', '-s', 'first', 'set-upstream',
                          'origin')
        self.assertIn("Set upstream for series 'first' to 'origin'",
                      out.getvalue())

        self.db_open()
        slist = cser.db.series_get_dict()
        self.assertEqual('origin', slist['first'].upstream)

    def test_patchwork_set_project(self):
        """Test setting the project ID"""
        cser = self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_cser)
        with terminal.capture() as (out, _):
            cser.project_set(pwork, 'U-Boot')
        self.assertEqual(
            f"Project 'U-Boot' patchwork-ID {self.PROJ_ID} link-name 'uboot'",
            out.getvalue().strip())

    def test_patchwork_project_get(self):
        """Test setting the project ID"""
        cser = self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_cser)
        self.assertFalse(cser.project_get())
        with terminal.capture() as (out, _):
            cser.project_set(pwork, 'U-Boot')
        self.assertEqual(
            f"Project 'U-Boot' patchwork-ID {self.PROJ_ID} link-name 'uboot'",
            out.getvalue().strip())

        name, pwid, link_name = cser.project_get()
        self.assertEqual('U-Boot', name)
        self.assertEqual(self.PROJ_ID, pwid)
        self.assertEqual('uboot', link_name)

    def test_patchwork_project_get_cmdline(self):
        """Test setting the project ID"""
        cser = self.get_cser()

        self.assertFalse(cser.project_get())

        cser.db.upstream_add('us', 'https://us.example.com')
        cser.db.commit()

        pwork = Patchwork.for_testing(self._fake_patchwork_cser)
        with terminal.capture() as (out, _):
            self.run_args('-P', 'https://url', 'patchwork', 'set-project',
                          'U-Boot', 'us', pwork=pwork)
        self.assertEqual(
            f"Project 'U-Boot' patchwork-ID {self.PROJ_ID} "
            f"link-name 'uboot' remote 'us'",
            out.getvalue().strip())

        name, pwid, link_name = cser.project_get('us')
        self.assertEqual('U-Boot', name)
        self.assertEqual(6, pwid)
        self.assertEqual('uboot', link_name)

        with terminal.capture() as (out, _):
            self.run_args('-P', 'https://url', 'patchwork', 'get-project',
                          'us')
        self.assertEqual(
            f"Project 'U-Boot' patchwork-ID {self.PROJ_ID} "
            f"link-name 'uboot' remote 'us'",
            out.getvalue().strip())

    def test_patchwork_cache(self):
        """Test showing what the patchwork cache holds"""
        self.get_cser()
        now = time.time()
        cache = pwcache.Cache(os.path.join(self.tmpdir, 'patchwork.db'),
                              clock=lambda: now)
        cache.put('https://pw', 'patches/1/', {'name': 'one'})
        cache.put('https://pw', 'patches/2/', {'name': 'two'})
        cache.put('https://pw', 'series/3/', {'id': 3})
        cache.add_hit('https://pw', 1500)
        cache.set_follow('https://pw', 'uboot', now - 7200, '', now - 120)

        self.db_open()
        self.cser.db.traffic_add(traffic.hour_key(), {
            ('https://pw', 'patch'): (4, 2500000),
            ('https://other', 'series'): (1, 300)})
        self.cser.db.commit()

        with mock.patch.object(pwcache, '_cache', cache), \
                terminal.capture() as (out, _):
            self.run_args('patchwork', 'cache')
        lines = out.getvalue().splitlines()
        self.assertRegex(lines[0], r'^Cache: .*patchwork.db, [0-9.]+ KB$')
        self.assertEqual('https://other', lines[2])
        self.assertEqual(
            '  This hour               1     300 B           0       0 B',
            lines[4])
        self.assertEqual('https://pw', lines[8])
        self.assertEqual(['patch', '2'], lines[10].split()[:2])
        self.assertEqual(['series', '1'], lines[11].split()[:2])
        self.assertTrue(lines[11].endswith('  0s ago'))
        self.assertEqual(
            '  This hour               4    2.5 MB           1    1.5 KB',
            lines[13])
        self.assertEqual("  https://pw 'uboot': since 2h ago, checked 2m ago",
                         lines[-1])
        cache.close()

        with mock.patch.object(pwcache, '_cache', False), \
                terminal.capture() as (out, _):
            self.run_args('patchwork', 'cache')
        self.assertEqual('The patchwork cache is not in use',
                         out.getvalue().strip())

    def test_patchwork_default_remote(self):
        """Test choosing the remote when 'set-project' is not given one"""
        def git(*args):
            command.run_one('git', *args, cwd=repo, capture=True)

        def default():
            orig = os.getcwd()
            try:
                os.chdir(repo)
                return control._default_remote()
            finally:
                os.chdir(orig)

        repo = os.path.join(self.tmpdir, 'remotes')
        os.mkdir(repo)
        git('init', '-q', '-b', 'video')
        self.assertEqual((None, []), default())

        # With one remote, use it
        git('remote', 'add', 'us', 'https://us.example.com')
        self.assertEqual(('us', ['us']), default())

        # With several, use the one the branch tracks, if any
        git('remote', 'add', 'ci', 'https://ci.example.com')
        self.assertEqual((None, ['ci', 'us']), default())
        git('config', 'branch.video.remote', 'ci')
        self.assertEqual(('ci', ['ci', 'us']), default())

        # The error lists the remotes to choose from
        git('config', '--unset', 'branch.video.remote')
        orig = os.getcwd()
        try:
            os.chdir(repo)
            with self.assertRaises(ValueError) as exc:
                control._get_remote(types.SimpleNamespace(remote=None))
            self.assertEqual(
                'Please specify the remote name (remotes: ci, us)',
                str(exc.exception))
            self.assertEqual(
                'us', control._get_remote(types.SimpleNamespace(remote='us')))
        finally:
            os.chdir(orig)

    def test_patchwork_list(self):
        """Test listing patchwork project configurations"""
        cser = self.get_cser()

        # No projects configured
        with terminal.capture() as (out, _):
            cser.project_list()
        self.assertEqual('No patchwork projects configured',
                         out.getvalue().strip())

        # Add two remotes for U-Boot and one for Linux
        cser.db.upstream_add('us', 'https://us.example.com')
        cser.db.upstream_add('ci', 'https://ci.example.com')
        cser.db.upstream_add('linus', 'https://linus.example.com')
        cser.db.patchwork_update('U-Boot', 6, 'uboot', 'us')
        cser.db.patchwork_update('U-Boot', 6, 'uboot', 'ci')
        cser.db.patchwork_update('Linux', 10, 'linux', 'linus')
        cser.db.commit()

        with terminal.capture() as (out, _):
            cser.project_list()
        lines = out.getvalue().splitlines()
        self.assertEqual(5, len(lines))
        self.assertIn('Linux', lines[2])
        self.assertIn('linus', lines[2])
        self.assertIn('U-Boot', lines[3])
        self.assertIn('ci', lines[3])
        self.assertIn('us', lines[3])

    def test_patchwork_upstream(self):
        """Test patchwork project with upstream association"""
        cser = self.get_cser()

        # Add two upstreams
        cser.db.upstream_add('us', 'https://us.example.com')
        cser.db.upstream_add('ci', 'https://ci.example.com')
        cser.db.commit()

        # Set project for a specific upstream
        cser.db.patchwork_update('U-Boot', 6, 'uboot', 'us')
        cser.db.commit()

        # Look up by upstream
        info = cser.db.patchwork_get('us')
        self.assertEqual(('U-Boot', 6, 'uboot'), info)

        # Different upstream has no project
        self.assertIsNone(cser.db.patchwork_get('ci'))

        # No upstream arg returns any match
        info = cser.db.patchwork_get()
        self.assertEqual(('U-Boot', 6, 'uboot'), info)

        # Set a different project for ci
        cser.db.patchwork_update('Linux', 10, 'linux', 'ci')
        cser.db.commit()

        self.assertEqual(('Linux', 10, 'linux'), cser.db.patchwork_get('ci'))
        self.assertEqual(('U-Boot', 6, 'uboot'), cser.db.patchwork_get('us'))

    def test_patchwork_rm(self):
        """Test deleting a patchwork project configuration"""
        cser = self.get_cser()

        cser.db.upstream_add('us', 'https://us.example.com')
        cser.db.upstream_add('ci', 'https://ci.example.com')
        cser.db.patchwork_update('U-Boot', 6, 'uboot', 'us')
        cser.db.patchwork_update('Linux', 10, 'linux', 'ci')
        cser.db.commit()

        # Delete by upstream name
        cser.db.patchwork_delete('us')
        cser.db.commit()
        self.assertIsNone(cser.db.patchwork_get('us'))
        self.assertEqual(('Linux', 10, 'linux'), cser.db.patchwork_get('ci'))

        # Delete non-existent raises ValueError
        with self.assertRaises(ValueError):
            cser.db.patchwork_delete('us')

    def test_patchwork_rm_default(self):
        """Test deleting the default (no upstream) patchwork project"""
        cser = self.get_cser()

        cser.db.patchwork_update('U-Boot', 6, 'uboot')
        cser.db.commit()
        self.assertIsNotNone(cser.db.patchwork_get())

        cser.db.patchwork_delete(None)
        cser.db.commit()
        self.assertIsNone(cser.db.patchwork_get())

    def test_migrate_patchwork_upstream(self):
        """Test that migrating to v5 renames settings to patchwork"""
        db = database.Database(f'{self.tmpdir}/.patman3.db')
        with terminal.capture():
            db.open_it()

        # Create a v4 database with an upstream and a patchwork row
        with terminal.capture():
            db.migrate_to(4)
        db.execute(
            "INSERT INTO upstream (name, url, is_default) "
            "VALUES ('us', 'https://us.example.com', 1)")
        db.execute(
            "INSERT INTO settings (name, proj_id, link_name) "
            "VALUES ('U-Boot', 6, 'uboot')")
        db.commit()

        # Migrate to v5
        with terminal.capture():
            db.migrate_to(5)

        # The existing row should now be in 'patchwork' with the default upstream
        res = db.execute(
            'SELECT name, proj_id, link_name, upstream FROM patchwork')
        recs = res.fetchall()
        self.assertEqual(1, len(recs))
        self.assertEqual(('U-Boot', 6, 'uboot', 'us'), recs[0])
        db.close()
