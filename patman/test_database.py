# SPDX-License-Identifier: GPL-2.0+

# Copyright 2025 Simon Glass <sjg@chromium.org>
#
"""Tests for 'patman database' and migrating the database"""

from datetime import datetime, timedelta, timezone
import os
from unittest import mock


from u_boot_pylib import terminal
from u_boot_pylib import tools
from patman import cmdline
from patman import control
from patman import cseries
from patman.database import Pcommit
from patman import database
from patman import times
from patman import traffic
from patman.test_common import CseriesTester

#pylint: disable=protected-access


class TestDatabase(CseriesTester):
    """Tests for 'patman database' and schema migration"""
    def open_db_in(self, dirname):
        """Open (creating if needed) the database in a directory

        Args:
            dirname (str): Directory to hold .patman.db

        Return:
            Cseries: Instance with the database open
        """
        os.makedirs(dirname, exist_ok=True)
        cser = cseries.Cseries(self.tmpdir, terminal.COLOR_NEVER,
                               db_dir=dirname)
        with terminal.capture():
            cser.open_database()
        return cser

    @staticmethod
    def add_db_series(db, name, versions, review=False, todo=False):
        """Add a series with some versions, patches and reviews

        Args:
            db (Database): Database to update
            name (str): Series name
            versions (list of int): Versions to add, each with one patch and,
                for a review, one review
            review (bool): True to make it a series being reviewed
            todo (bool): True to add a todo for the series

        Return:
            int: Series ID
        """
        sid = db.series_add(name, f'{name} desc', None)
        if review:
            db.series_set_source(sid, 'review')
        for version in versions:
            svid = db.ser_ver_add(sid, version, link=f'{sid}{version}')
            db.pcommit_add_list(svid, [Pcommit(None, 0, f'{name} patch',
                                               None, None, None, None, None)])
            if review:
                db.review_add(svid, 1, f'{name} v{version} review', True,
                              '2026-10-01 10:00:00')
        if todo:
            db.execute('INSERT INTO workflow (type, series_id, timestamp, '
                       'archived) VALUES (?, ?, ?, 0)',
                       ('todo', sid, '2026-11-01 10:00:00'))
        db.commit()
        return sid

    @staticmethod
    def db_summary(db):
        """Summarise the series in a database

        Return:
            dict: key (str): series name
                value (tuple): (source, list of versions, number of reviews,
                    number of patches, number of todos)
        """
        out = {}
        for sid, name, source in db.execute(
                'SELECT id, name, source FROM series').fetchall():
            vers = [row[0] for row in db.execute(
                'SELECT version FROM ser_ver WHERE series_id = ? '
                'ORDER BY version', (sid,)).fetchall()]
            svids = '(SELECT id FROM ser_ver WHERE series_id = ?)'
            nrev = db.execute(f'SELECT COUNT(*) FROM review WHERE svid IN '
                              f'{svids}', (sid,)).fetchone()[0]
            npat = db.execute(f'SELECT COUNT(*) FROM pcommit WHERE svid IN '
                              f'{svids}', (sid,)).fetchone()[0]
            ntodo = db.execute('SELECT COUNT(*) FROM workflow WHERE '
                               'series_id = ?', (sid,)).fetchone()[0]
            out[name] = (source, vers, nrev, npat, ntodo)
        return out

    def run_db(self, *argv):
        """Run a 'database' command, returning its output

        Return:
            str: stdout and stderr together
        """
        args = cmdline.parse_args(['db', *argv], config_fname=False)
        with terminal.capture() as (out, err):
            control.do_database(args, self.tmpdir)
        return out.getvalue() + err.getvalue()

    def test_db_split_join(self):
        """Reviews can be moved to a separate database and back"""
        main = self.get_database()
        self.add_db_series(main.db, 'mine', [1, 2], todo=True)
        self.add_db_series(main.db, 'rev1', [1, 2], review=True)
        self.add_db_series(main.db, 'clash', [1], review=True)
        self.add_db_series(main.db, 'dup', [1, 2], review=True)
        self.add_db_series(main.db, 'twice', [1, 1], review=True)

        # A response to a comment on patchwork belongs to its version
        svid = main.db.ser_ver_get_for_series(
            main.db.series_find_by_name('rev1'))[0].idnum
        main.db.reply_add_comment(svid, 0, 77, '<c1@x>', '', 'Alex <a@x>', '',
                                  '', 'Re: x', 'today', 'Why?', 'now')
        main.commit()
        main.close_database()

        # The review database has one of your own series with the same name
        # as a review, and a review with one version in common
        rdir = os.path.join(self.tmpdir, 'reviews')
        rcser = self.open_db_in(rdir)
        self.add_db_series(rcser.db, 'clash', [1])
        self.add_db_series(rcser.db, 'dup', [2], review=True)
        rcser.close_database()

        # A dry run changes nothing
        out = self.run_db('-n', '--review-db-dir', rdir, 'split')
        self.assertIn('Moved 1 series: 2 versions, 2 patches, 2 reviews',
                      out)
        self.assertIn('Dry run completed', out)
        main = self.get_database()
        self.assertEqual(5, len(self.db_summary(main.db)))
        main.close_database()

        out = self.run_db('--review-db-dir', rdir, 'split')
        self.assertIn('Already present, not copied: dup v2', out)
        self.assertIn('Already present, not copied: twice v1', out)
        self.assertIn("Kept apart: 'clash'", out)
        self.assertIn('3 series left in place', out)
        self.assertTrue(os.path.exists(
            os.path.join(self.tmpdir, '.patman.db.bak')))
        self.assertTrue(os.path.exists(os.path.join(rdir, '.patman.db.bak')))

        main = self.get_database()
        self.assertEqual(
            {'mine': (None, [1, 2], 0, 2, 1),
             'clash': ('review', [1], 1, 1, 0),
             'dup': ('review', [1, 2], 2, 2, 0),
             'twice': ('review', [1, 1], 2, 2, 0)},
            self.db_summary(main.db))
        main.close_database()
        rcser = self.open_db_in(rdir)
        self.assertEqual(
            {'clash': (None, [1], 0, 1, 0),
             'dup': ('review', [2], 1, 1, 0),
             'rev1': ('review', [1, 2], 2, 2, 0)},
            self.db_summary(rcser.db))
        svr = rcser.db.ser_ver_get_for_series(
            rcser.db.series_find_by_name('rev1'))[0]
        self.assertEqual([(0, 77, 'Why?')],
                         [(rep.seq, rep.comment_id, rep.body)
                          for rep in rcser.db.reply_get_comments(svr.idnum)])
        rcser.close_database()

        # Moving back brings 'rev1' home; 'dup' is in both so stays put
        out = self.run_db('--review-db-dir', rdir, 'join')
        self.assertIn('Moved 1 series', out)
        main = self.get_database()
        self.assertEqual(('review', [1, 2], 2, 2, 0),
                         self.db_summary(main.db)['rev1'])
        main.close_database()

    def test_db_split_envelope(self):
        """Moving a review keeps the headers for its reply"""
        main = self.get_database()
        self.add_db_series(main.db, 'rev1', [1], review=True)
        rev = main.db.review_get_for_version(main.db.ser_ver_get_for_series(
            main.db.series_find_by_name('rev1'))[0].idnum)[0]
        main.db.review_set_envelope(rev.idnum, {'subject': 'Re: x'})
        main.commit()
        main.close_database()

        rdir = os.path.join(self.tmpdir, 'reviews')
        self.open_db_in(rdir).close_database()
        self.run_db('--review-db-dir', rdir, 'split')
        rcser = self.open_db_in(rdir)
        rev = rcser.db.review_get_for_version(rcser.db.ser_ver_get_for_series(
            rcser.db.series_find_by_name('rev1'))[0].idnum)[0]
        self.assertEqual({'subject': 'Re: x'},
                         rcser.db.review_get_envelope(rev.idnum))
        rcser.close_database()

    def test_db_errors(self):
        """Problems with the review database are reported"""
        self.get_database().close_database()
        with self.assertRaises(ValueError) as exc:
            self.run_db('split')
        self.assertIn('No review database', str(exc.exception))
        with self.assertRaises(ValueError) as exc:
            self.run_db('--review-db-dir', '/no/such/dir', 'split')
        self.assertIn('does not exist', str(exc.exception))
        with self.assertRaises(ValueError) as exc:
            self.run_db('--review-db-dir', self.tmpdir, 'split')
        self.assertIn('same as the main one', str(exc.exception))
        with self.assertRaises(ValueError) as exc:
            self.run_db('merge', os.path.join(self.tmpdir, 'nothing'))
        self.assertIn('No database found', str(exc.exception))

    def test_db_merge(self):
        """Series can be copied from another database, by kind"""
        main = self.get_database()
        self.add_db_series(main.db, 'mine', [1])
        main.db.upstream_add('us', 'https://us', 'https://pw.us')
        main.db.upstream_set_default('us')
        main.commit()
        main.close_database()

        other = os.path.join(self.tmpdir, 'other')
        ocser = self.open_db_in(other)
        self.add_db_series(ocser.db, 'mine', [1, 2], todo=True)
        self.add_db_series(ocser.db, 'theirs', [3], todo=True)
        self.add_db_series(ocser.db, 'rev', [1], review=True)
        ocser.db.upstream_add('us', 'https://elsewhere')
        ocser.db.upstream_add('ci', 'https://ci', 'https://pw.ci',
                              lore_url='https://lore.ci')
        ocser.db.upstream_set_default('ci')
        ocser.db.patchwork_update('U-Boot', 1, 'uboot', 'ci')
        ocser.commit()
        ocser.close_database()
        other_db = os.path.join(other, '.patman.db')
        before = tools.read_file(other_db)

        out = self.run_db('merge', '--own', other)
        self.assertIn('Copied 1 series: 2 versions, 2 patches, 0 reviews, '
                      '1 todos', out)
        self.assertIn('Added 1 upstreams', out)
        self.assertIn('Already present, not copied: mine v1', out)
        self.assertEqual(before, tools.read_file(other_db))

        main = self.get_database()
        self.assertEqual({'mine': (None, [1, 2], 0, 2, 0),
                          'theirs': (None, [3], 0, 1, 1)},
                         self.db_summary(main.db))
        ups = main.db.upstream_get_dict()
        self.assertEqual(('https://us', True), ups['us'][:2])
        self.assertEqual(('https://ci', False, 'https://pw.ci'),
                         ups['ci'][:3])
        self.assertEqual('https://lore.ci', ups['ci'][7])
        self.assertEqual('uboot', main.project_get('ci')[2])
        main.close_database()

        # Reviews can go straight into the review database
        rdir = os.path.join(self.tmpdir, 'reviews')
        self.open_db_in(rdir).close_database()
        out = self.run_db('--review-db-dir', rdir, 'merge', '--reviews', '-R',
                          other_db)
        self.assertIn('Copied 1 series: 1 versions, 1 patches, 1 reviews',
                      out)
        rcser = self.open_db_in(rdir)
        self.assertEqual({'rev': ('review', [1], 1, 1, 0)},
                         self.db_summary(rcser.db))
        self.assertEqual({}, rcser.db.upstream_get_dict())
        rcser.close_database()

    def test_series_is_own(self):
        """A series with a link is found if it is yours, not a review"""
        db = self.get_database().db
        mine = self.add_db_series(db, 'mine', [1])
        rev = self.add_db_series(db, 'rev', [1], review=True)
        self.assertEqual((True, False, False),
                         (db.series_is_own(f'{mine}1'),
                          db.series_is_own(int(f'{rev}1')),
                          db.series_is_own('999')))

    def test_series_remove_if_empty(self):
        """A series is removed only if it has no versions"""
        db = self.get_database().db
        rev = self.add_db_series(db, 'rev', [1], review=True)
        empty = self.add_db_series(db, 'empty', [], review=True)
        self.assertEqual((False, True),
                         (db.series_remove_if_empty(rev),
                          db.series_remove_if_empty(empty)))
        self.assertEqual([rev], [ser.idnum for ser in db.series_get_dict(
            include_archived=True, reviews_only=True).values()])

    def test_series_remove_empty_reviews(self):
        """Review series with no versions are removed, but not your own"""
        db = self.get_database().db
        mine = self.add_db_series(db, 'mine', [])
        rev = self.add_db_series(db, 'rev', [1], review=True)
        self.add_db_series(db, 'empty', [], review=True)
        self.add_db_series(db, 'empty2', [], review=True)
        self.assertEqual(2, db.series_remove_empty_reviews())
        self.assertEqual([mine, rev], [ser.idnum for ser in db.series_get_dict(
            include_archived=True, include_reviews=True).values()])

    def test_series_get_max_reviewed_versions(self):
        """Only the versions with a review count as reviewed"""
        db = self.get_database().db
        rev = self.add_db_series(db, 'rev', [1, 2], review=True)
        applied = self.add_db_series(db, 'applied', [], review=True)
        db.ser_ver_add(applied, 1, link='a1')
        svid = db.ser_ver_add(rev, 3, link='r3')
        self.assertEqual(
            [(rev, 2)], [(sid, version) for _, sid, version in
                         db.series_get_max_reviewed_versions()])
        self.assertIn((svid, rev, 3), db.series_get_all_max_versions())

    def test_migrate(self):
        """Test migration to later schema versions"""
        db = database.Database(f'{self.tmpdir}/.patman.db')
        with terminal.capture() as (out, err):
            db.open_it()
        self.assertEqual(
            f'Creating new database {self.tmpdir}/.patman.db',
            err.getvalue().strip())

        self.assertEqual(0, db.get_schema_version())

        for version in range(1, database.LATEST + 1):
            with terminal.capture() as (out, _):
                db.migrate_to(version)
            self.assertTrue(os.path.exists(
                f'{self.tmpdir}/.patman.dbold.v{version - 1}'))
            self.assertEqual(f'Update database to v{version}',
                             out.getvalue().strip())
            self.assertEqual(version, db.get_schema_version())
            if version == 16:
                # Traffic counted before v17 has no kind
                db.execute("INSERT INTO traffic (hour, url, requests, bytes) "
                           "VALUES ('2026-10-18 09', 'https://pw', 5, 100)")
                db.commit()
        self.assertEqual(18, database.LATEST)
        self.assertEqual({('https://pw', ''): (5, 100)},
                         db.traffic_get_kinds('2026-10-18 00'))

    def test_traffic(self):
        """Requests to patchwork are counted by the hour"""
        db = database.Database(f'{self.tmpdir}/.patman.db')
        with terminal.capture():
            db.start()
        db.traffic_add('2026-10-18 09', {'https://pw': (5, 100)})
        db.traffic_add('2026-10-19 08', {'https://pw': (2, 20),
                                         'https://other': (1, 5)})
        db.traffic_add('2026-10-19 08', {'https://pw': (1, 10)})
        self.assertEqual({'https://pw': (8, 130), 'https://other': (1, 5)},
                         db.traffic_get('2026-10-18 00'))
        self.assertEqual({'https://pw': (3, 30), 'https://other': (1, 5)},
                         db.traffic_get('2026-10-19 08'))

        # Old counts are dropped
        db.traffic_add('2026-10-19 09', {}, drop_before='2026-10-19 00')
        self.assertEqual({'https://pw': (3, 30), 'https://other': (1, 5)},
                         db.traffic_get('2000-01-01 00'))

        # At exit, counts are only written if the database is closed, since
        # it may hold changes which a command did not mean to keep
        traffic.take()
        traffic.add('https://pw', requests=4, nbytes=40)
        with mock.patch.object(traffic, '_db_path', db.db_path):
            traffic._save_at_exit()
            self.assertEqual({('https://pw', ''): (4, 40)}, traffic.take())

            traffic.add('https://pw', requests=4, nbytes=40)
            db.close()
            traffic._save_at_exit()
        self.assertEqual({}, traffic.take())
        db.open_it()
        hour = traffic.hour_key()
        self.assertEqual({'https://pw': (4, 40)}, db.traffic_get(hour))
        db.close()

    def test_series_hidden(self):
        """Reviewed series can be hidden from the list of reviews"""
        db = database.Database(f'{self.tmpdir}/.patman.db')
        with terminal.capture():
            db.start()
        first = db.series_add('first', 'First', None)
        second = db.series_add('second', 'Second', None)
        self.assertEqual(set(), db.series_get_hidden())
        db.series_set_hidden(second, True)
        self.assertEqual({second}, db.series_get_hidden())
        db.series_set_hidden(second, False)
        db.series_set_hidden(first, True)
        self.assertEqual({first}, db.series_get_hidden())
        db.close()

    def test_times(self):
        """Times are written in the database's form, local to the machine"""
        self.assertEqual('2026-10-19 08:05:03',
                         times.to_str(datetime(2026, 10, 19, 8, 5, 3)))
        self.assertRegex(times.now_str(), r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$')
        zone = timezone(timedelta(hours=5, minutes=30))
        with mock.patch.object(times, 'datetime') as fake:
            fake.now.return_value.astimezone.return_value = datetime(
                2026, 10, 19, tzinfo=zone)
            self.assertEqual(330, times.utc_offset())
            fake.now.return_value.astimezone.return_value = datetime(
                2026, 10, 19, tzinfo=timezone(timedelta(hours=-6)))
            self.assertEqual(-360, times.utc_offset())

    def test_migrate_future_version(self):
        """Test that a database newer than patman is rejected"""
        db = database.Database(f'{self.tmpdir}/.patman.db')
        with terminal.capture():
            db.start()

        # Set the schema version beyond what patman supports
        db.cur.execute(
            f'UPDATE schema_version SET version = {database.LATEST + 1}')
        db.commit()
        db.close()

        with self.assertRaises(SystemExit):
            with terminal.capture() as (_, err):
                db.start()
        self.assertIn('is too new', err.getvalue())

    def test_migrate_upstream_warning(self):
        """Test that migrating to v5 warns about series without upstream"""
        self.make_git_tree()

        # Set 'first' branch to track a remote-style upstream so that
        # auto-detection can find it
        self.repo.config.set_multivar('branch.first.remote', '', 'origin')
        self.repo.config.set_multivar('branch.first.merge', '',
                                      'refs/heads/main')

        db = database.Database(f'{self.tmpdir}/.patman2.db')
        with terminal.capture():
            db.open_it()

        # Create a v4 database with some series; 'first' has a matching
        # branch with a detectable remote, 'second' and 'third' do not
        with terminal.capture() as (out, _):
            db.migrate_to(4)
        self.assertEqual(
            'Update database to v1\nUpdate database to v2\n'
            'Update database to v3\nUpdate database to v4',
            out.getvalue().strip())
        db.execute(
            "INSERT INTO series (name, desc, archived) "
            "VALUES ('first', 'desc1', 0)")
        db.execute(
            "INSERT INTO series (name, desc, archived) "
            "VALUES ('second', 'desc2', 0)")
        db.execute(
            "INSERT INTO series (name, desc, archived) "
            "VALUES ('third', 'desc3', 0)")
        db.commit()
        db.close()

        cser = cseries.Cseries(self.tmpdir, terminal.COLOR_NEVER)
        cser.topdir = self.tmpdir
        cser.gitdir = self.gitdir

        # Point at our v4 database
        database.Database.instances = {}
        cser.db, _ = database.Database.get_instance(
            f'{self.tmpdir}/.patman2.db')
        with terminal.capture() as (_, err):
            old_version = cser.db.start()
        self.assertEqual(4, old_version)

        # 'first' should be auto-detected, 'second' and 'third' have no
        # matching branch with a remote upstream
        with terminal.capture() as (out, err):
            cser._check_null_upstreams()
        self.assertIn("Set upstream for series 'first' to 'origin'",
                      out.getvalue())
        lines = err.getvalue().strip().splitlines()
        self.assertEqual('2 series without an upstream:', lines[0])
        self.assertEqual('  second', lines[1])
        self.assertEqual('  third', lines[2])

        # Check that 'first' was actually updated in the database
        slist = cser.db.series_get_dict()
        self.assertEqual('origin', slist['first'].upstream)
        self.assertIsNone(slist['second'].upstream)

        cser.db.close()
