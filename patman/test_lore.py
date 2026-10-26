# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for reading mailing-list threads from lore"""

import asyncio
import gzip
import os
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from u_boot_pylib import terminal

from patman import lore
from patman import lorelist
from patman import pwcache
from patman import status
from patman.patchwork import Patchwork
from patman.review import lookup


def run(coro):
    """Run a coroutine in a loop of its own, leaving the thread's loop alone"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# pylint: disable-next=R0913
def message(msgid, subject, sender, body, reply_to=None, refs=None,
            list_id=None):
    """Make the text of an email, as it appears in an mbox file"""
    lines = ['From mboxrd@z Thu Jan  1 00:00:00 1970',
             f'From: {sender}',
             f'Subject: {subject}',
             'Date: Tue, 14 Oct 2026 10:38:38 +0200',
             f'Message-ID: <{msgid}>']
    if reply_to:
        lines.append(f'In-Reply-To: <{reply_to}>')
    if refs:
        lines.append('References: ' + ' '.join(f'<{ref}>' for ref in refs))
    if list_id:
        lines.append(f'List-Id: <{list_id}>')
    return '\n'.join(lines) + '\n\n' + body + '\n\n'


FRED = 'Fred Bloggs <fred@example.com>'
SIMON = 'Simon Glass <sjg@chromium.org>'

# A series with a cover letter and two patches, and the replies to it
THREAD = ''.join([
    message('cover@x', '[PATCH 0/2] dm: Tidy up', FRED, 'Some tidy-ups'),
    message('p1@x', '[PATCH 1/2] dm: First', FRED, 'First',
            'cover@x', ['cover@x']),
    message('p2@x', '[PATCH 2/2] dm: Second', FRED, 'Second',
            'cover@x', ['cover@x']),
    message('r1@x', 'Re: [PATCH 1/2] dm: First', SIMON,
            '> First\n\nReviewed-by: Simon Glass <sjg@chromium.org>',
            'p1@x', ['cover@x', 'p1@x']),
    message('r2@x', 'Re: [PATCH 1/2] dm: First', FRED, 'Thanks',
            'r1@x', ['cover@x', 'p1@x', 'r1@x']),
    message('r3@x', 'Re: [PATCH 0/2] dm: Tidy up', SIMON,
            'For the series:\n\nTested-by: Simon Glass <sjg@chromium.org>',
            'cover@x', ['cover@x']),
    # A reply to a message which lore does not have, found by References
    message('r4@x', 'Re: [PATCH 2/2] dm: Second', SIMON, 'Nit',
            'gone@x', ['cover@x', 'p2@x', 'gone@x']),
    # A new version sent in reply to the old one, which is not a comment
    message('v2@x', '[PATCH v2 1/2] dm: First', FRED, 'First, again',
            'p1@x', ['cover@x', 'p1@x']),
    # Nothing to do with the series
    message('other@x', 'Hello', FRED, 'Hi'),
]).encode('utf-8')

def feed_entry(msgid, subject, date, parent=None, name='Ann'):
    """Make an entry in lore's Atom feed

    Args:
        msgid (str): Message-ID, without angle brackets
        subject (str): Subject
        date (str): When it was sent, e.g. '2026-10-01T10:00:00Z'
        parent (str or None): Message-ID of the message it replies to
        name (str): Name of who sent it

    Returns:
        str: Entry, as XML
    """
    reply = (f'<thr:in-reply-to ref="urn:x" href="{LIST}/{parent}/"/>'
             if parent else '')
    return (f'<entry><author><name>{name}</name><email>{name.lower()}@x'
            f'</email></author><title>{subject}</title><updated>{date}'
            f'</updated><link href="{LIST}/{msgid}/"/>{reply}</entry>')


LIST = 'https://lore.kernel.org/my-list'

# A page of the feed: a series with a cover letter, its next version sent in
# reply to it, a patch on its own, a series whose cover letter is too old to
# be in the page, and a reply which lore's search missed
FEED = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:thr="http://purl.org/syndication/thread/1.0">' + ''.join([
            feed_entry('v2c@x', '[PATCH v2 0/2] dm: Tidy up',
                       '2026-10-05T10:00:00Z', 'c@x'),
            feed_entry('v2p1@x', '[PATCH v2 1/2] dm: First',
                       '2026-10-05T10:00:01Z', 'v2c@x'),
            feed_entry('v2p2@x', '[PATCH v2 2/2] dm: Second',
                       '2026-10-05T10:00:02Z', 'v2c@x'),
            feed_entry('one@x', '[PATCH] net: Fix it', '2026-10-04T10:00:00Z',
                       name='Bob'),
            feed_entry('old2@x', '[RFC PATCH 2/3] fs: Two',
                       '2026-10-03T10:00:00Z', 'old@x'),
            feed_entry('p2@x', '[PATCH 2/2] dm: Second', '2026-10-01T10:00:02Z',
                       'c@x'),
            feed_entry('p1@x', '[PATCH 1/2] dm: First', '2026-10-01T10:00:01Z',
                       'c@x'),
            feed_entry('c@x', '[PATCH 0/2] dm: Tidy up', '2026-10-01T10:00:00Z'),
            feed_entry('r@x', 'Re: thanks', '2026-10-02T10:00:00Z', 'c@x'),
        ]) + '</feed>').encode()


SERIES = {
    'id': 7, 'received_all': True,
    'cover_letter': {'id': 70, 'name': 'dm: Tidy up', 'msgid': '<cover@x>'},
    'patches': [{'id': 71, 'name': 'dm: First', 'msgid': '<p1@x>'},
                {'id': 72, 'name': 'dm: Second', 'msgid': '<p2@x>'}],
}


class Response:
    """Pretend response from lore"""
    def __init__(self, status, content=b''):
        self.status = status
        self.content = content

    async def read(self):
        """Get the content"""
        return self.content

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Client:  # pylint: disable=R0903
    """Pretend aiohttp session, giving each response in turn"""
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, **_kwargs):
        """Start a request"""
        self.urls.append(url)
        return self.responses.pop(0)


class TestLore(unittest.TestCase):
    """Test reading threads from lore"""
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='patman.')

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_comments_for(self):
        """Replies go to the message they reply to, however deep"""
        messages = lore.parse_mbox(THREAD)
        self.assertEqual(9, len(messages))
        found = lore.comments_for(messages, ['<cover@x>', '<p1@x>', 'p2@x'])
        self.assertEqual(
            {'cover@x': ['r3@x'], 'p1@x': ['r1@x', 'r2@x'],
             'p2@x': ['r4@x']},
            {mid: [bare(c['msgid']) for c in comments]
             for mid, comments in found.items()})

    def test_to_comment(self):
        """A message gives the fields of a patchwork comment"""
        comment = lore.comments_for(lore.parse_mbox(THREAD),
                                    ['<p1@x>'])['p1@x'][0]
        self.assertEqual(
            (None, '<r1@x>', '2026-10-14T08:38:38',
             {'name': 'Simon Glass', 'email': 'sjg@chromium.org'},
             'Re: [PATCH 1/2] dm: First',
             'https://lore.kernel.org/all/r1@x/'),
            (comment['id'], comment['msgid'], comment['date'],
             comment['submitter'], comment['subject'], comment['web_url']))
        self.assertIn('Reviewed-by: Simon Glass', comment['content'])
        self.assertEqual('<p1@x>', comment['headers']['In-Reply-To'])

    def test_get_thread(self):
        """A thread is read, and one which lore lacks is remembered"""
        lre = lore.Lore()
        client = Client(Response(200, gzip.compress(THREAD)))
        self.assertEqual(9, len(run(lre.get_thread(client, '<cover@x>'))))
        self.assertEqual(['https://lore.kernel.org/all/cover@x/t.mbox.gz'],
                         client.urls)

        cache = pwcache.Cache(os.path.join(self.tmpdir, 'pw.db'))
        with mock.patch.object(pwcache, 'get', return_value=cache):
            client = Client(Response(404))
            self.assertIsNone(run(lre.get_thread(client, 'gone@x')))
            self.assertIsNone(run(lre.get_thread(client, 'gone@x')))
            self.assertEqual(1, len(client.urls))

            # It is asked for again when rechecking
            client = Client(Response(200, gzip.compress(THREAD)))
            self.assertEqual(9, len(run(lre.get_thread(client, 'gone@x',
                                                       recheck=True))))
            self.assertEqual(1, len(client.urls))

            # A failure is tried again, but not remembered
            client = Client(Response(503), Response(503),
                            Response(200, gzip.compress(THREAD)))
            with mock.patch.object(lore.asyncio, 'sleep'):
                self.assertIsNone(run(lre.get_thread(client, 'busy@x')))
                self.assertEqual(9, len(run(lre.get_thread(client,
                                                           'busy@x'))))
            self.assertEqual(3, len(client.urls))

    def fake_patchwork(self, reads):
        """Make a Patchwork object with a fake server, recording the reads

        Each patch's comments on patchwork are the replies to it in THREAD
        """
        replies = {'patches/71/comments/': ['r1@x', 'r2@x'],
                   'patches/72/comments/': ['r4@x'],
                   'covers/70/comments/': ['r3@x']}

        def fake(subpath):
            reads.append(subpath)
            if subpath == 'series/7/':
                return SERIES
            if subpath.endswith('/comments/'):
                return [{'msgid': f'<{mid}>', 'content': 'From patchwork',
                         'submitter': {'name': 'Pat', 'email': 'pat@x'},
                         'date': ''} for mid in replies[subpath]]
            return {'id': int(subpath.split('/')[1]), 'state': 'new',
                    'project': {'list_id': 'u-boot.example.org'}}

        pwork = Patchwork.for_testing(fake)
        fake_lore = mock.Mock()
        fake_lore.thread = lore.parse_mbox(THREAD)

        async def get_thread(_client, msgid):
            fake_lore.msgid = msgid
            return fake_lore.thread
        fake_lore.get_thread = get_thread
        pwork.lore = fake_lore
        return pwork

    @staticmethod
    def sync(pwork, reads):
        """Sync series 7, giving the comments and the comment lists read"""
        reads.clear()
        cover, patches = run(pwork.series_get_state(None, '7', True, True))
        return ([c['content'].strip() for c in cover.comments],
                [[c['content'].strip()[:5] for c in pat.comments]
                 for pat in patches],
                sorted(r for r in reads if r.endswith('comments/')))

    def test_sync_use(self):
        """With 'use', a sync reads the comments from lore, else patchwork"""
        reads = []
        pwork = self.fake_patchwork(reads)
        with mock.patch.object(lore, 'MODE', 'use'):
            # The thread is read once, from its cover letter
            self.assertEqual((['For the series:\n\nTested-by: Simon Glass '
                               '<sjg@chromium.org>'],
                              [['> Fir', 'Thank'], ['Nit']], []),
                             self.sync(pwork, reads))
            self.assertEqual('<cover@x>', pwork.lore.msgid)

            # If lore does not have the thread, patchwork is asked
            all_three = ['covers/70/comments/', 'patches/71/comments/',
                         'patches/72/comments/']
            pwork.lore.thread = None
            self.assertEqual(all_three, self.sync(pwork, reads)[2])

            # So it is if lore is missing a patch, e.g. because it is behind
            pwork.lore.thread = [msg for msg in lore.parse_mbox(THREAD)
                                 if 'p2@x' not in str(msg['Message-ID'])]
            self.assertEqual(all_three, self.sync(pwork, reads)[2])

    def test_sync_hint(self):
        """With 'hint', comments come from patchwork when lore has new ones"""
        reads = []
        pwork = self.fake_patchwork(reads)
        now = [1000.0]
        pwork.cache = pwcache.Cache(os.path.join(self.tmpdir, 'pw.db'),
                                    clock=lambda: now[0])
        all_three = ['covers/70/comments/', 'patches/71/comments/',
                     'patches/72/comments/']
        with mock.patch.object(lore, 'MODE', 'hint'):
            # The comments come from patchwork, and the first time all are
            # read
            result = self.sync(pwork, reads)
            self.assertEqual((['From patchwork'],
                              [['From ', 'From '], ['From ']], all_three),
                             result)

            # Lore has no new replies, so none is read again
            self.assertEqual([], self.sync(pwork, reads)[2])

            # A new reply to patch 2 on lore means that only it is read
            extra = message('r5@x', 'Re: [PATCH 2/2] dm: Second', SIMON,
                            'Reviewed-by: Simon Glass <sjg@chromium.org>',
                            'p2@x', ['cover@x', 'p2@x'],
                            'u-boot.example.org')
            pwork.lore.thread = lore.parse_mbox(THREAD + extra.encode())
            self.assertEqual(['patches/72/comments/'],
                             self.sync(pwork, reads)[2])

            # Patchwork does not have that reply, but it is only read once
            self.assertEqual([], self.sync(pwork, reads)[2])

            # A reply sent to another list is not on patchwork, so is left
            # out
            other = message('r6@x', 'Re: [PATCH 1/2] dm: First', SIMON, 'Hi',
                            'p1@x', ['cover@x', 'p1@x'], 'linux.example.org')
            pwork.lore.thread = lore.parse_mbox(THREAD + other.encode())
            self.assertEqual([], self.sync(pwork, reads)[2])

            # After a while, all are read again, in case lore lacks some
            now[0] += lore.RECHECK + 1
            self.assertEqual(all_three, self.sync(pwork, reads)[2])

        # Without lore, all are read each time
        pwork.lore = False
        self.assertEqual(all_three, self.sync(pwork, reads)[2])

    def test_patch_info(self):
        """The place of a patch in its series comes from its subject"""
        msgs = {str(m['Message-ID']).strip('<>'): m
                for m in lore.parse_mbox(THREAD)}
        self.assertEqual((1, 2, 2, '[2/2] dm: Second'),
                         lore.patch_info(msgs['p2@x']))
        self.assertEqual((2, 1, 2, '[v2,1/2] dm: First'),
                         lore.patch_info(msgs['v2@x']))
        self.assertIsNone(lore.patch_info(msgs['cover@x']))
        self.assertIsNone(lore.patch_info(msgs['other@x']))

    def test_fill_from_lore(self):
        """Patches which patchwork lacks are taken from lore"""
        incomplete = {
            'id': 8, 'version': 1, 'total': 2, 'received_total': 1,
            'received_all': False,
            'cover_letter': {'id': 80, 'name': 'dm: Tidy up',
                             'msgid': '<cover@x>'},
            'patches': [{'id': 81, 'name': '[1/2] dm: First',
                         'msgid': '<p1@x>'}]}
        reads = []

        def fake(subpath):
            reads.append(subpath)
            if subpath == 'series/8/':
                return incomplete
            return {'id': 81, 'headers': {}, 'content': 'pw'}

        pwork = Patchwork.for_testing(fake)
        fake_lore = mock.Mock()
        fake_lore.thread = lore.parse_mbox(THREAD)

        async def get_thread(_client, _msgid):
            return fake_lore.thread
        fake_lore.get_thread = get_thread
        pwork.lore = fake_lore

        data = run(pwork.fill_from_lore(None, incomplete))
        self.assertEqual(['[1/2] dm: First', '[2/2] dm: Second'],
                         [p['name'] for p in data['patches']])
        self.assertEqual(['lore:p2@x'], data['lore_patches'])
        self.assertNotIn('lore_patches', incomplete)

        # The patch from lore can be read like any other, with its replies
        patch = run(pwork.get_patch(None, 'lore:p2@x'))
        self.assertEqual(('<p2@x>', 'Second', '', 'new'),
                         (patch['headers']['Message-ID'],
                          patch['content'].strip(), patch['diff'],
                          patch['state']))
        self.assertEqual(['<r4@x>'], [c['msgid'] for c in run(
            pwork.get_patch_comments(None, 'lore:p2@x'))])

        # The mbox has both patches, from lore, in order
        mbox = pwork.lore_mbox(data)
        self.assertEqual(
            ['[PATCH 1/2] dm: First', '[PATCH 2/2] dm: Second'],
            [str(m['Subject']) for m in lore.parse_mbox(mbox)])
        self.assertIsNone(pwork.lore_mbox(incomplete))

        # A review fills in the series, and goes ahead
        with mock.patch.object(lookup.tout, 'notice') as notice:
            data, _, _, count = lookup.fetch_series(pwork, '8')
        self.assertEqual(2, count)
        self.assertIn('Patchwork lacks [2/2] dm: Second; using the copy on '
                      'lore', [c.args[0] for c in notice.call_args_list])

        # If lore lacks the patch, the review cannot go ahead
        fake_lore.thread = [m for m in lore.parse_mbox(THREAD)
                            if 'p2@x' not in str(m['Message-ID'])]
        with mock.patch.object(lookup.tout, 'notice'):
            with self.assertRaises(ValueError) as exc:
                lookup.fetch_series(pwork, '8')
        self.assertIn('https://my-fake-url has 1 of 2', str(exc.exception))

        # A complete series is left alone, and lore is not asked
        fake_lore.get_thread = mock.Mock()
        done = dict(SERIES)
        self.assertIs(done, run(pwork.fill_from_lore(None, done)))
        fake_lore.get_thread.assert_not_called()

    def lore_list(self, thread=THREAD):
        """Make a LoreList which reads a thread given by the test"""
        llist = lorelist.LoreList('https://lore.kernel.org/my-list/')
        reads = []

        async def get_thread(_client, msgid, recheck=False):
            self.assertTrue(recheck)
            reads.append(msgid)
            messages = lore.parse_mbox(thread)
            if msgid not in {lorelist.msgid_of(msg) for msg in messages}:
                return None
            return messages
        llist.lore.get_thread = get_thread
        return llist, reads

    def test_lorelist_series(self):
        """A series is read from its thread on lore"""
        llist, reads = self.lore_list()
        self.assertEqual(('https://lore.kernel.org/my-list', 'my-list'),
                         (llist.url, llist.link_name))
        data = run(llist.get_series(None, '<cover@x>'))
        self.assertEqual(
            ('cover@x', 'dm: Tidy up', 1, 2, 2, True),
            (data['id'], data['name'], data['version'], data['total'],
             data['received_total'], data['received_all']))
        self.assertEqual('lore:cover@x', data['cover_letter']['id'])

        # The new version sent in reply to the old one is left out
        self.assertEqual(
            [('lore:p1@x', '[1/2] dm: First', '<p1@x>'),
             ('lore:p2@x', '[2/2] dm: Second', '<p2@x>')],
            [(p['id'], p['name'], p['msgid']) for p in data['patches']])
        self.assertEqual(['new', 'new'],
                         run(llist.get_series_patch_states(None, 'cover@x')))

        # Each reply goes to what it replies to, not to the cover letter
        def replies(func, item_id):
            return [c['msgid'] for c in run(func(None, item_id))]
        self.assertEqual(['<r3@x>'],
                         replies(llist.get_cover_comments, 'lore:cover@x'))
        self.assertEqual(['<r1@x>', '<r2@x>'],
                         replies(llist.get_patch_comments, 'lore:p1@x'))
        self.assertEqual(['<r4@x>'],
                         replies(llist.get_patch_comments, 'lore:p2@x'))

        patch = run(llist.get_patch(None, 'lore:p2@x'))
        self.assertEqual(('lore:p2@x', 'Second', '<p2@x>'),
                         (patch['id'], patch['content'].strip(),
                          patch['headers']['Message-ID']))
        cover = run(llist.get_cover(None, 'lore:cover@x'))
        self.assertEqual(('lore:cover@x', '[0/2] dm: Tidy up', 'Some tidy-ups'),
                         (cover['id'], cover['name'],
                          cover['content'].strip()))

        # The mbox has the patches in order, without the cover letter
        mbox = run(llist.get_series_mbox(None, 'cover@x'))
        self.assertEqual(
            ['[PATCH 1/2] dm: First', '[PATCH 2/2] dm: Second'],
            [str(m['Subject']) for m in lore.parse_mbox(mbox)])
        self.assertIsNone(llist.lore_mbox(data))
        self.assertIs(data, run(llist.fill_from_lore(None, data)))

        # The thread is read just once, unless asked to read it again
        self.assertEqual(['cover@x'], reads)
        self.assertEqual(data, run(llist.get_series(None, 'cover@x',
                                                    max_age=0)))
        self.assertEqual(['cover@x', 'cover@x'], reads)
        reads.pop()

        # A review reads the series the same way
        with mock.patch.object(lookup.tout, 'notice'):
            data, name, version, count = lookup.fetch_series(llist, 'cover@x')
        self.assertEqual(('dm: Tidy up', 1, 2), (name, version, count))

    def test_lorelist_status(self):
        """The status of a series which you sent is read from lore"""
        llist, reads = self.lore_list()
        cover, patches = run(llist.series_get_state(None, 'cover@x', True,
                                                    True))
        self.assertEqual(('lore:cover@x', 1, '[0/2] dm: Tidy up', ['<r3@x>']),
                         (cover.id, cover.num_comments, cover.name,
                          [cmt['msgid'] for cmt in cover.comments]))
        self.assertEqual(
            [('lore:p1@x', 'new', '[1/2] dm: First', ['<r1@x>', '<r2@x>']),
             ('lore:p2@x', 'new', '[2/2] dm: Second', ['<r4@x>'])],
            [(pat.id, pat.state, pat.series_data['name'],
              [cmt['msgid'] for cmt in pat.comments]) for pat in patches])

        # The review tag in a reply to the first patch is found
        new_rtags, _ = status.process_reviews(patches[0].data['content'],
                                              patches[0].comments, {})
        self.assertEqual({'Reviewed-by': {SIMON}}, new_rtags)

        # Messages say where the patches are from
        with terminal.capture() as (out, err):
            status.check_patch_count(1, 2, llist.SOURCE)
        self.assertIn('Warning: Lore reports 2 patches, series has 1',
                      out.getvalue() + err.getvalue())

        # Without the comments, only the series is read
        cover, patches = run(llist.series_get_state(None, 'cover@x', False,
                                                    False))
        self.assertIsNone(cover)
        self.assertEqual(['[1/2] dm: First', '[2/2] dm: Second'],
                         [pat.raw_subject for pat in patches])
        self.assertIsNone(patches[0].comments)

        # The thread is read again each time, to see what has arrived
        self.assertEqual(['cover@x', 'cover@x'], reads)

        self.assertEqual('https://lore.kernel.org/my-list/cover@x/',
                         run(llist.get_series_url('cover@x')))
        with llist.collect_stats() as stats:
            llist.lore.request_count += 3
        self.assertEqual(3, stats.request_count)

    def test_lorelist_single(self):
        """A patch on its own is a series, and only a series is accepted"""
        thread = ''.join([
            message('one@x', '[PATCH v2] libfdt: Fix it', FRED, 'Fix',
                    list_id='my-list.vger.kernel.org'),
            message('re@x', 'Re: [PATCH v2] libfdt: Fix it', SIMON, 'Nice',
                    'one@x', ['one@x']),
            # A patch sent in reply, e.g. a suggested change, is not part of
            # the series
            message('fix@x', '[PATCH] libfdt: Fix it better', SIMON, 'Fix',
                    'one@x', ['one@x']),
        ]).replace('Subject: [PATCH v2]',
                   'To: Fred <My-List@vger.kernel.org>\nSubject: [PATCH v2]',
                   1).encode('utf-8')
        llist, _ = self.lore_list(thread)
        data = run(llist.get_series(
            None, 'https://lore.kernel.org/my-list/one@x/T/#u'))
        self.assertEqual(('one@x', '[v2] libfdt: Fix it', 2, 1, None),
                         (data['id'], data['name'], data['version'],
                          data['total'], data['cover_letter']))
        self.assertEqual(['lore:one@x'], [p['id'] for p in data['patches']])

        # The patch is named as in its series, without 'PATCH', and links to
        # the list
        patch = run(llist.get_patch(None, 'lore:one@x'))
        self.assertEqual(('[v2] libfdt: Fix it', f'{LIST}/one@x/'),
                         (patch['name'], patch['web_url']))
        self.assertEqual(
            {'name': 'my-list', 'link_name': 'my-list',
             'list_id': 'my-list.vger.kernel.org',
             'list_email': 'My-List@vger.kernel.org'}, data['project'])
        self.assertEqual(['<re@x>'], [c['msgid'] for c in run(
            llist.get_patch_comments(None, 'lore:one@x'))])

        llist, _ = self.lore_list()
        for msgid, msg in (
                ('p2@x', "Message 'p2@x' is patch 2 of 2; give the cover "
                 'letter or the first patch'),
                ('r1@x', "Message 'r1@x' is not a patch or cover letter"),
                ('gone@x', "Cannot read message 'gone@x' from "
                 'https://lore.kernel.org/my-list')):
            with self.assertRaises(ValueError) as exc:
                run(llist.get_series(None, msgid))
            self.assertEqual(msg, str(exc.exception))

    def test_lorelist_feed_complete(self):
        """A series which is only partly in the feed is read from its thread"""
        llist, reads = self.lore_list()
        part = lorelist.feed_series(lorelist.parse_feed(FEED), LIST)[3]
        part['patches'] = part['patches'][1:]
        part['received_all'] = False
        part['id'] = 'cover@x'
        full = run(llist.complete_series(None, part))
        self.assertEqual(['cover@x'], reads)
        self.assertEqual(
            ('cover@x', 'dm: Tidy up', True, 'lore:cover@x',
             ['lore:p1@x', 'lore:p2@x']),
            (full['id'], full['name'], full['received_all'],
             full['cover_letter']['id'],
             [pat['id'] for pat in full['patches']]))

        # A complete series is left alone
        reads.clear()
        whole = lorelist.feed_series(lorelist.parse_feed(FEED), LIST)[0]
        self.assertIs(whole, run(llist.complete_series(None, whole)))
        self.assertEqual([], reads)

    def test_lorelist_feed(self):
        """The newest series on a list are read from lore's feed"""
        series = lorelist.feed_series(lorelist.parse_feed(FEED), LIST)
        self.assertEqual(
            [('v2c@x', '[v2] dm: Tidy up', 2, 2, True, 'lore:v2c@x',
              ['lore:v2p1@x', 'lore:v2p2@x']),
             ('one@x', 'net: Fix it', 1, 1, True, None, ['lore:one@x']),
             ('old@x', '[RFC] fs: Two', 1, 3, False, None, ['lore:old2@x']),
             ('c@x', 'dm: Tidy up', 1, 2, True, 'lore:c@x',
              ['lore:p1@x', 'lore:p2@x'])],
            [(ser['id'], ser['name'], ser['version'], ser['total'],
              ser['received_all'], (ser['cover_letter'] or {}).get('id'),
              [pat['id'] for pat in ser['patches']]) for ser in series])
        one = series[1]
        self.assertEqual(
            ('2026-10-04T10:00:00', {'name': 'Bob', 'email': 'bob@x'},
             f'{LIST}/one@x/', 'net: Fix it'),
            (one['date'], one['submitter'], one['web_url'],
             one['patches'][0]['name']))

        # Each page is read once, then taken from the cache
        llist = lorelist.LoreList(LIST)
        self.assertEqual('?q=s%3APATCH+AND+NOT+s%3ARe&x=A',
                         llist.recent_series_path(None, 1, 50))
        self.assertEqual('?q=s%3APATCH+AND+NOT+s%3ARe&x=A&o=200',
                         llist.recent_series_path(None, 2, 50))
        cache = pwcache.Cache(os.path.join(self.tmpdir, 'pw.db'))
        # The series whose cover letter is not in the page is looked for in
        # its thread, which lore does not have here
        client = Client(Response(200, FEED), Response(404), Response(404))
        with mock.patch.object(pwcache, 'get', return_value=cache):
            self.assertIsNone(llist.read_time(llist.recent_series_path(
                None, 1, 50)))
            got = run(llist.get_recent_series(client, None, max_age=60))
            self.assertEqual(series, got)
            self.assertEqual(got, run(llist.get_recent_series(client, None,
                                                              max_age=60)))
            self.assertEqual([f'{LIST}/?q=s%3APATCH+AND+NOT+s%3ARe&x=A',
                              f'{LIST}/old@x/t.mbox.gz'], client.urls)
            self.assertIsNotNone(llist.read_time(llist.recent_series_path(
                None, 1, 50)))
            self.assertLess(llist.read_age(llist.recent_series_path(
                None, 1, 50)), 60)

            # A page which cannot be read says so
            with self.assertRaises(ValueError) as exc:
                run(llist.get_recent_series(client, None, page=2))
        self.assertEqual(f'Cannot read the newest series from {LIST}',
                         str(exc.exception))
        self.assertEqual({}, llist.get_states(['lore:p1@x']))
        self.assertIsNone(run(llist.follow_events(client)))
        self.assertEqual('p1@x/', llist.patch_path('lore:p1@x'))

    def test_lorelist_search(self):
        """A list on lore can be searched for series by their subjects"""
        self.assertEqual(
            's:"make_fit" AND s:"compress" AND s:PATCH AND NOT s:Re',
            lorelist.search_query('  make_fit\t"compress" '))
        self.assertEqual('(dfn:scripts/make_fit.py) AND s:PATCH AND NOT s:Re',
                         lorelist.search_query('dfn:scripts/make_fit.py'))
        self.assertEqual('(fit f:dianders) AND s:PATCH AND NOT s:Re',
                         lorelist.search_query('fit f:dianders'))

        # The colon of a subject's prefix does not make it a query for lore
        self.assertEqual(
            's:"arm64:" AND s:"Add" AND s:"fit" AND s:PATCH AND NOT s:Re',
            lorelist.search_query('arm64: Add fit'))
        for text in ('', ' ', '""'):
            with self.assertRaises(ValueError) as exc:
                lorelist.search_query(text)
            self.assertEqual('Please give some text to search for',
                             str(exc.exception))

        # The series are not completed from their threads, and the results
        # are kept for a while
        llist = lorelist.LoreList(LIST)
        cache = pwcache.Cache(os.path.join(self.tmpdir, 'pw.db'))
        client = Client(Response(200, FEED), Response(404))
        with mock.patch.object(pwcache, 'get', return_value=cache):
            got = run(llist.search_series(client, None, 'dm'))
            self.assertEqual(
                lorelist.feed_series(lorelist.parse_feed(FEED), LIST), got)
            self.assertEqual(got, run(llist.search_series(client, None,
                                                          'dm')))
            self.assertEqual([f'{LIST}/?q=s%3A%22dm%22+AND+s%3APATCH+AND+NOT'
                              '+s%3ARe&x=A'], client.urls)

            # Lore says when nothing matches
            self.assertEqual([], run(llist.search_series(client, None,
                                                         'nothing')))

            # A search which cannot be read says so
            with mock.patch.object(lore, 'RETRIES', 0), \
                    self.assertRaises(ValueError) as exc:
                run(llist.search_series(Client(Response(500)), None, 'x'))
        self.assertEqual(f'Cannot search {LIST}', str(exc.exception))

    def test_lorelist_find_series(self):
        """A series which you send can be found on lore by its title"""
        llist = lorelist.LoreList(LIST)

        def find(desc, version, first='dm: First', searches=1):
            ser = SimpleNamespace(desc=desc,
                                  commits=[SimpleNamespace(subject=first)])
            client = Client(*[Response(200, FEED)] * searches)
            with mock.patch.object(pwcache, 'get', return_value=None), \
                    terminal.capture():
                _, _, link, options = run(
                    llist._find_series(  # pylint: disable=W0212
                        client, 1, 2, version, ser))
            self.assertEqual(searches, len(client.urls))
            return link, options

        # Each version is found, with or without a cover letter
        self.assertEqual(('v2c@x', None), find('dm: Tidy up', 2))
        self.assertEqual(('c@x', None), find('dm: Tidy up', 1))
        self.assertEqual(('one@x', None), find('net: Fix it', 1,
                                               first='net: Fix it'))
        self.assertEqual(3, llist.request_count)

        # A version which is not on lore gives the others, after looking for
        # the first patch too
        link, options = find('dm: Tidy up', 3, searches=2)
        self.assertIsNone(link)
        self.assertEqual(
            [('v2c@x', 'dm: Tidy up', 2), ('c@x', 'dm: Tidy up', 1)],
            [(opt['id'], opt['name'], opt['version']) for opt in options])

        # Lore is searched even when the cache has the search, since the
        # series may just have been sent
        cache = pwcache.Cache(os.path.join(self.tmpdir, 'pw.db'))
        with mock.patch.object(pwcache, 'get', return_value=cache), \
                terminal.capture():
            run(llist.search_series(Client(Response(200, FEED)), None,
                                    'dm: Tidy up'))
            client = Client(Response(200, b''))
            self.assertEqual([], run(llist.query_series(client, 'dm: Tidy up')))
        self.assertEqual(1, len(client.urls))

    def test_find_list(self):
        """The list which has an address can be found on lore"""
        def config(name, addr):
            return (f'; Example public-inbox config snippet\n'
                    f'[publicinbox "{name}"]\n'
                    f'\tinboxdir = /path/to/top-level-inbox\n'
                    f'\taddress = {addr}\n'
                    f'\tlistid = {name}.example\n').encode()

        self.assertEqual(('lkml', ['linux-kernel@vger.kernel.org']),
                         lorelist.parse_list_config(
                             config('lkml', 'Linux-Kernel@vger.kernel.org')))
        self.assertEqual((None, []), lorelist.parse_list_config(b'junk'))

        def find(addr, *responses):
            client = Client(*responses)
            url = run(lorelist.find_list(client, addr, base='https://lore'))
            return url, client.urls

        # The list is named for the address, or lore may know it by another
        # name
        self.assertEqual(
            ('https://lore/dtc', ['https://lore/dtc/_/text/config/raw']),
            find('DTC <dtc@vger.kernel.org>',
                 Response(200, config('dtc', 'dtc@vger.kernel.org'))))
        self.assertEqual(
            'https://lore/lkml',
            find('linux-kernel@vger.kernel.org', Response(
                200, config('lkml', 'linux-kernel@vger.kernel.org')))[0])

        # A list with that name but another address is not the one
        self.assertIsNone(find('dtc@example.com', Response(
            200, config('dtc', 'dtc@vger.kernel.org')))[0])

        # Lore does not have the list
        self.assertIsNone(find('none@example.com', Response(404))[0])

        # Something which is not an address is not looked up
        self.assertEqual((None, []), find('u-boot'))

    def test_strip_signature(self):
        """The signature at the end of a patch is not part of its diff"""
        diff = '@@ -1,2 +1 @@\n-a\n+b\n'
        for sig in ('-- \n2.43.0\n', '-- \nFred Bloggs <f@x>\n\n',
                    '--  \nFred Bloggs <f@x>\n'):
            self.assertEqual(diff, lore.strip_signature(diff + sig))

        # A removed line holding '- ' is kept
        for kept in ('@@ -1,2 +1 @@\n-a\n-- \n+b\n', '@@ -1 +0 @@\n-- \n',
                     diff):
            self.assertEqual(kept, lore.strip_signature(kept))

    def test_lorelist_names(self):
        """A link to a series on lore is tidied into its Message-ID"""
        for link in ('a@b', '<a@b>', ' a@b ',
                     'https://lore.kernel.org/dtc/a@b/',
                     'https://lore.kernel.org/all/a@b/T/#u'):
            self.assertEqual('a@b', lorelist.parse_link(link))

    def test_set_mode(self):
        """The mode must be one which is known"""
        with mock.patch.object(lore, 'MODE', 'hint'):
            lore.set_mode('use')
            self.assertEqual('use', lore.MODE)
            with self.assertRaises(ValueError) as exc:
                lore.set_mode('sometimes')
            self.assertIn("Unknown lore mode 'sometimes'", str(exc.exception))

def bare(msgid):
    """Remove the angle brackets from a Message-ID"""
    return msgid.strip('<>')
