# SPDX-License-Identifier: GPL-2.0+

# Copyright 2025 Simon Glass <sjg@chromium.org>
#
"""Tests for 'patman workflow'"""

from datetime import datetime


from u_boot_pylib import terminal
from patman.test_common import CseriesTester
from patman import workflow as wf

#pylint: disable=protected-access


class TestWorkflow(CseriesTester):
    """Tests for 'patman workflow' and the workflow records"""
    def test_workflow_db_methods(self):
        """Test workflow database methods"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', 'my description', allow_unmarked=True)

        ser = cser.get_series_by_name('first')

        # Initially there is no workflow entry
        self.assertIsNone(cser.db.workflow_get('todo', ser.idnum))

        # Add a todo entry
        cser.db.workflow_add('todo', ser.idnum, '2025-03-15 10:00:00')
        cser.commit()

        # Should be able to read it back
        ts = cser.db.workflow_get('todo', ser.idnum)
        self.assertEqual('2025-03-15 10:00:00', ts)

        # Get by type should return it
        entries = cser.db.workflow_get_by_type('todo')
        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual(ser.idnum, entry[0])
        self.assertEqual('first', entry[1])
        self.assertEqual('my description', entry[2])
        self.assertEqual('2025-03-15 10:00:00', entry[3])

        # Get by type with before filter
        entries = cser.db.workflow_get_by_type(
            'todo', before='2025-03-14 00:00:00')
        self.assertEqual(0, len(entries))
        entries = cser.db.workflow_get_by_type(
            'todo', before='2025-03-16 00:00:00')
        self.assertEqual(1, len(entries))

        # Archive it - should no longer be active, but still in the table
        cser.db.workflow_archive('todo', ser.idnum)
        cser.commit()
        self.assertIsNone(cser.db.workflow_get('todo', ser.idnum))
        res = cser.db.execute(
            'SELECT archived FROM workflow WHERE series_id = ?',
            (ser.idnum,))
        self.assertEqual(1, res.fetchone()[0])

    def test_workflow_todo(self):
        """Test setting and clearing a todo"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', 'my description', allow_unmarked=True)

        cser.fake_now = datetime(2025, 3, 1, 12, 0, 0)
        ser = cser.get_series_by_name('first')

        # Set a todo for 7 days
        with terminal.capture() as (out, _):
            wf.todo(cser,'first', 7)
        self.assertIn('2025-03-08 12:00:00', out.getvalue())

        # Check the DB entry
        ts = cser.db.workflow_get('todo', ser.idnum)
        self.assertEqual('2025-03-08 12:00:00', ts)

        # Replacing the todo should work
        with terminal.capture() as (out, _):
            wf.todo(cser,'first', 14)
        self.assertIn('2025-03-15 12:00:00', out.getvalue())
        ts = cser.db.workflow_get('todo', ser.idnum)
        self.assertEqual('2025-03-15 12:00:00', ts)

        # Clear it
        with terminal.capture() as (out, _):
            wf.todo_clear(cser,'first')
        self.assertIn('Todo cleared', out.getvalue())
        self.assertIsNone(cser.db.workflow_get('todo', ser.idnum))

    def test_workflow_todo_list(self):
        """Test listing todos"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', 'my description', allow_unmarked=True)
            cser.add('second', 'board stuff', allow_unmarked=True)

        cser.fake_now = datetime(2025, 3, 10, 12, 0, 0)

        # Set todos: first is due, second is in the future
        with terminal.capture():
            wf.todo(cser,'first', 0)
            wf.todo(cser,'second', 7)

        # Default list shows only due entries
        with terminal.capture() as (out, _):
            wf.todo_list(cser,show_all=False)
        lines = out.getvalue().splitlines()
        self.assertEqual(3, len(lines))
        self.assertIn('first', lines[2])
        self.assertIn('today', lines[2])

        # --all shows all entries
        with terminal.capture() as (out, _):
            wf.todo_list(cser,show_all=True)
        lines = out.getvalue().splitlines()
        self.assertEqual(4, len(lines))
        self.assertIn('first', lines[2])
        self.assertIn('today', lines[2])
        self.assertIn('second', lines[3])
        self.assertIn('in 7d', lines[3])

        # No todos
        with terminal.capture():
            wf.todo_clear(cser,'first')
            wf.todo_clear(cser,'second')
        with terminal.capture() as (out, _):
            wf.todo_list(cser,show_all=False)
        self.assertIn('No todos due', out.getvalue())

    def test_workflow_summary_marker(self):
        """Test that [todo] shows in series summary"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', 'my description', allow_unmarked=True)

        cser.fake_now = datetime(2025, 3, 10, 12, 0, 0)

        # Set a todo that is already due
        with terminal.capture():
            wf.todo(cser,'first', 0)

        # Summary should show [todo]
        with terminal.capture() as (out, _):
            cser.summary(None)
        self.assertIn('[todo]', out.getvalue())

        # Set a todo in the future
        with terminal.capture():
            wf.todo(cser,'first', 14)

        # Summary should NOT show [todo]
        with terminal.capture() as (out, _):
            cser.summary(None)
        self.assertNotIn('[todo]', out.getvalue())

    def test_workflow_todo_cmdline(self):
        """Test todo via the command line"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', 'my description', allow_unmarked=True)

        # Test via command line
        self.db_close()
        with terminal.capture() as (out, _):
            self.run_args('workflow', 'todo', '-s', 'first', '7')
        self.assertIn('marked for todo', out.getvalue())

        with terminal.capture() as (out, _):
            self.run_args('workflow', 'todo', '-s', 'first', '--clear')
        self.assertIn('Todo cleared', out.getvalue())

        with terminal.capture() as (out, _):
            self.run_args('wf', 'todo-list')
        self.assertIn('No todos due', out.getvalue())

    def test_workflow_sent(self):
        """Test that sending a series creates SENT and TODO entries"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', 'my description', allow_unmarked=True)

        cser.fake_now = datetime(2025, 3, 1, 12, 0, 0)
        ser = cser.get_series_by_name('first')

        svid = cser.get_series_svid(ser.idnum, 1)

        # Record a send with ser_ver_id
        wf.sent(cser, ser.idnum, ser_ver_id=svid)

        # Should have a SENT entry with current time
        ts = cser.db.workflow_get('sent', ser.idnum)
        self.assertEqual('2025-03-01 12:00:00', ts)

        # The SENT entry should have the ser_ver_id
        res = cser.db.execute(
            'SELECT ser_ver_id FROM workflow '
            'WHERE type = ? AND series_id = ? AND archived = 0',
            ('sent', ser.idnum))
        self.assertEqual(svid, res.fetchone()[0])

        # Should have a TODO entry 7 days out (no ser_ver_id)
        ts = cser.db.workflow_get('todo', ser.idnum)
        self.assertEqual('2025-03-08 12:00:00', ts)

        # Sending again should archive old entries and create new ones
        cser.fake_now = datetime(2025, 3, 5, 12, 0, 0)
        wf.sent(cser, ser.idnum, ser_ver_id=svid)

        ts = cser.db.workflow_get('sent', ser.idnum)
        self.assertEqual('2025-03-05 12:00:00', ts)

        ts = cser.db.workflow_get('todo', ser.idnum)
        self.assertEqual('2025-03-12 12:00:00', ts)

    def test_workflow_list(self):
        """Test listing all workflow entries"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', 'my description', allow_unmarked=True)

        cser.fake_now = datetime(2025, 3, 1, 12, 0, 0)

        # Record a send (creates SENT + TODO)
        ser = cser.get_series_by_name('first')
        wf.sent(cser, ser.idnum)

        # Default list shows only active entries
        with terminal.capture() as (out, _):
            wf.list_entries(cser, show_all=False)
        lines = out.getvalue().splitlines()
        self.assertEqual(4, len(lines))
        self.assertIn('sent', lines[2])
        self.assertIn('todo', lines[3])

        # Archive the todo
        with terminal.capture():
            wf.todo_clear(cser, 'first')

        # Without --all, only SENT is active; no 'A' column
        with terminal.capture() as (out, _):
            wf.list_entries(cser, show_all=False)
        lines = out.getvalue().splitlines()
        self.assertEqual(3, len(lines))
        self.assertIn('sent', lines[2])
        self.assertNotIn('A', lines[0])

        # With --all, archived entries appear with '*' marker
        with terminal.capture() as (out, _):
            wf.list_entries(cser, show_all=True)
        lines = out.getvalue().splitlines()
        self.assertGreater(len(lines), 3)
        self.assertIn('  A  ', lines[0])
        has_archived = any('*' in line for line in lines[2:])
        self.assertTrue(has_archived)

    def test_friendly_time(self):
        """Test friendly timestamp formatting"""
        now = datetime(2025, 3, 10, 15, 0, 0)  # Monday

        # Same day
        when = datetime(2025, 3, 10, 9, 30, 0)
        self.assertEqual('09:30', wf.friendly_time(now, when))

        # Earlier this week (3 days ago = Friday)
        when = datetime(2025, 3, 7, 14, 20, 0)
        self.assertEqual('Fri 14:20', wf.friendly_time(now, when))

        # 10 days ago
        when = datetime(2025, 2, 28, 10, 0, 0)
        self.assertEqual('10d ago', wf.friendly_time(now, when))

        # 3 weeks ago
        when = datetime(2025, 2, 17, 10, 0, 0)
        self.assertEqual('3w ago', wf.friendly_time(now, when))

        # Future within a week (3 days from now = Thursday)
        when = datetime(2025, 3, 13, 16, 0, 0)
        self.assertEqual('Thu 16:00', wf.friendly_time(now, when))

        # Future 10 days
        when = datetime(2025, 3, 20, 10, 0, 0)
        self.assertEqual('in 10d', wf.friendly_time(now, when))

        # Future 3 weeks
        when = datetime(2025, 3, 31, 10, 0, 0)
        self.assertEqual('in 3w', wf.friendly_time(now, when))
