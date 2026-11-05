# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for the checks made when patman runs in a strictly confined snap"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from u_boot_pylib import tools

from patman import snap


class TestSnap(unittest.TestCase):
    """Tests for the limits of the patman snap"""
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='patman-snap.')
        self.addCleanup(shutil.rmtree, self.tmpdir)
        self.snap_dir = os.path.join(self.tmpdir, 'snap')
        os.makedirs(os.path.join(self.snap_dir, 'meta'))
        self.set_confinement('strict')
        env = mock.patch.dict(os.environ, {'SNAP': self.snap_dir,
                                           'SNAP_NAME': 'patman'})
        env.start()
        self.addCleanup(env.stop)

    def set_confinement(self, confinement):
        """Write the snap's metadata, with the given confinement"""
        tools.write_file(os.path.join(self.snap_dir, 'meta', 'snap.yaml'),
                         f'name: patman\nconfinement: {confinement}\n',
                         binary=False)

    def test_confinement(self):
        """The confinement comes from the snap's metadata"""
        self.assertEqual('strict', snap.confinement())
        self.assertTrue(snap.is_strict())
        self.set_confinement('classic')
        self.assertEqual('classic', snap.confinement())
        self.assertFalse(snap.is_strict())

        # A snap whose metadata cannot be read is taken to be strict
        os.remove(os.path.join(self.snap_dir, 'meta', 'snap.yaml'))
        self.assertTrue(snap.is_strict())

        with mock.patch.dict(os.environ, {'SNAP': ''}):
            self.assertIsNone(snap.confinement())

    def test_check_cwd(self):
        """Running outside the directories the snap can reach is an error"""
        snap.check_setup()
        with mock.patch('os.getcwd', return_value=snap.VOID):
            with self.assertRaises(ValueError) as exc:
                snap.check_setup()
            self.assertIn('The patman snap cannot reach the directory it is '
                          'run in', str(exc.exception))
            self.assertIn(snap.GUIDE, str(exc.exception))

            # Outside the snap, nothing is checked
            self.set_confinement('classic')
            snap.check_setup()

    def test_check_plugs(self):
        """Settings which the snap is not allowed to read are reported"""
        home = os.path.join(self.tmpdir, 'home')
        os.makedirs(os.path.join(home, '.config', 'git'))
        tools.write_file(os.path.join(home, '.patman'), '', binary=False)
        with mock.patch.dict(os.environ, {'HOME': home}):
            snap.check_setup()
            with mock.patch('patman.snap.open', create=True,
                            side_effect=PermissionError):
                with self.assertRaises(ValueError) as exc:
                    snap.check_setup()
            self.assertIn("cannot read ~/.patman: connect its dot-patman "
                          "interface with 'sudo snap connect "
                          "patman:dot-patman'", str(exc.exception))
            with mock.patch('os.listdir', side_effect=PermissionError):
                with self.assertRaises(ValueError) as exc:
                    snap.check_setup()
            self.assertIn('cannot read ~/.config/git: connect its '
                          'dot-gitconfig interface', str(exc.exception))
