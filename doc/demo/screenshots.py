#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Take the pictures of the 'patman serve' page for the guide

Usage: screenshots.py [OUTDIR]

This runs serve_demo.py, which serves made-up series and fakes the jobs, so
nothing is reviewed or sent for real. It then drives Chrome, using Playwright,
through reviewing a series from the inbox, working on the reviews, responding
to comments and sending the reviews, taking a picture at each step, at the
width of a small tablet.

This needs Playwright ('pip install playwright') and Google Chrome. The
pictures go in OUTDIR (default doc/guide/images). If Pillow is installed,
each one is reduced to a palette of colours, which makes it much smaller.
"""

import os
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.realpath(__file__))
PORT = 8052
URL = f'http://127.0.0.1:{PORT}/'

# The size of a small tablet's screen, which is wide enough for the lines of
# a review, with each pixel drawn as two, so that the text is sharp
VIEW = {'width': 660, 'height': 800}
SCALE = 2

# Space to leave around a card, in its picture
PAD = 8

# Hide the scroll bar, and patman's version, which changes with each commit.
# Hide the bars at the top and bottom of the page while taking a picture of
# part of it, since they cover part of the page
STYLE = '''
window.addEventListener('DOMContentLoaded', () => {
  const style = document.createElement('style');
  style.textContent = `
    html { scrollbar-width: none; }
    .version { visibility: hidden; }
    html.crop header, html.crop nav { visibility: hidden; }`;
  document.head.append(style);
});
'''


class Shots:
    """Drive the page and take pictures of it

    Properties:
        page (playwright.sync_api.Page): Page being driven
        outdir (str): Directory to write the pictures to
        names (list of str): Pictures taken so far
    """
    def __init__(self, page, outdir):
        self.page = page
        self.outdir = outdir
        self.names = []

    def _path(self, name):
        """Get the path of a picture, recording its name"""
        self.names.append(name)
        return os.path.join(self.outdir, f'{name}.png')

    def screen(self, name):
        """Take a picture of the screen, as it is"""
        time.sleep(0.5)
        self.page.screenshot(path=self._path(name), animations='disabled')

    def crop(self, loc, name):
        """Take a picture of one part of the page, with a margin around it

        Args:
            loc (Locator): Part of the page, e.g. a card
            name (str): Name of the picture, without '.png'
        """
        page = self.page
        loc.scroll_into_view_if_needed()
        time.sleep(0.5)
        page.evaluate("document.documentElement.classList.add('crop')")
        box = loc.bounding_box()
        top = page.evaluate('window.scrollY')
        clip = {'x': max(0, box['x'] - PAD), 'y': box['y'] + top - PAD,
                'width': min(box['width'] + 2 * PAD, VIEW['width']),
                'height': box['height'] + 2 * PAD}
        page.screenshot(path=self._path(name), full_page=True, clip=clip,
                        animations='disabled')
        page.evaluate("document.documentElement.classList.remove('crop')")

    def part(self, loc, name):
        """Take a picture of one part of the page, just as it is shown

        Args:
            loc (Locator): Part of the page, e.g. the tabs
            name (str): Name of the picture, without '.png'
        """
        time.sleep(0.5)
        loc.screenshot(path=self._path(name), animations='disabled')

    def go(self, hash_path):
        """Show a screen of the page, e.g. '#/inbox'"""
        self.page.goto(f'{URL}{hash_path}')
        self.page.locator('#main .spinner').wait_for(state='detached')

    def confirm(self, loc):
        """Tap a button which needs a second tap to confirm

        The button's label changes after the first tap, so the second goes to
        the same element
        """
        handle = loc.element_handle()
        handle.click()
        handle.click()


def wait_for_server():
    """Wait until the demo server answers"""
    for _ in range(100):
        try:
            with urllib.request.urlopen(f'{URL}api/version', timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError('The demo server did not start')


def run(shots):
    """Go through reviewing a series, taking the pictures

    Args:
        shots (Shots): Page to drive
    """
    page = shots.page
    done = 'done'

    # The inbox, then starting a review
    shots.go('#/inbox')
    card = page.locator('.card.patch', has_text='Ample AB1234')
    card.wait_for()
    shots.screen('serve-inbox')

    # The series which are applied, one opened to show its patches
    page.get_by_role('button', name='Applied', exact=True).click()
    applied = page.locator('.card.patch', has_text='Demo board')
    applied.locator('.name').click()
    applied.locator('.chip', has_text='us/next').first.wait_for()
    shots.crop(applied, 'serve-inbox-applied')
    applied.locator('.name').click()

    # A new version, opened to show the review of the earlier one
    page.get_by_role('button', name='New versions', exact=True).click()
    newver = page.locator('.card.patch', has_text='Example SoC Ethernet')
    newver.locator('.name').click()
    newver.locator('.section', has_text='Earlier version').wait_for()
    shots.crop(newver, 'serve-inbox-earlier')
    newver.locator('.name').click()
    page.get_by_role('button', name='To review', exact=True).click()

    card.locator('.name').click()

    # The command to try the series in your tree
    card.get_by_role('button', name='Try it', exact=True).click()
    card.locator('.trycmd').wait_for()
    shots.crop(card, 'serve-try')
    card.get_by_role('button', name='Try it', exact=True).click()

    card.get_by_role('button', name='Review', exact=True).click()
    card.locator('textarea').fill(
        'Focus on the driver; the docs patch is minor')
    shots.crop(card, 'serve-inbox-review')
    card.get_by_role('button', name='Start the review').click()
    card.locator('.chip', has_text='review running').wait_for()
    shots.crop(card, 'serve-inbox-busy')
    page.locator('nav a[data-tab="jobs"] .badge', has_text='1').wait_for()
    shots.part(page.locator('nav'), 'serve-tabs')

    # The job
    shots.go('#/jobs')
    page.locator('.card', has_text='Review series').first.click()
    page.locator('.chip', has_text='ok').wait_for(timeout=60000)
    # Show it again, so that the count of jobs on the tab is up to date
    page.reload()
    page.locator('.chip', has_text='ok').wait_for()
    shots.screen('serve-job')

    # The reviews
    page.locator('.card a[href^="#/reviews/"]', has_text='Reviews').click()
    first = page.locator('.card.review', has_text='Patch 1')
    first.wait_for()
    shots.screen('serve-review')
    first.get_by_role('button', name='Show the patch').click()
    first.locator('.inline-mail .diff').first.wait_for()
    shots.crop(first.locator('.inline-mail'), 'serve-patch')
    first.get_by_role('button', name='Hide the patch').click()

    # Change a comment, then cancel it
    first.locator('.chunk-btn', has_text='Edit').nth(1).click()
    area = first.locator('textarea')
    area.press('Control+End')
    area.type(' Is 20ms enough?')
    shots.crop(first, 'serve-edit')
    first.get_by_role('button', name='Cancel').click()

    # Have the agent revise a comment
    first.locator('.chunk-btn', has_text='Revise this').nth(1).click()
    first.locator('textarea').fill('Ask whether a shorter delay would do')
    shots.crop(first, 'serve-revise')
    first.get_by_role('button', name='Revise', exact=True).click()
    first.locator('.jobstrip', has_text=done).wait_for(timeout=30000)
    shots.crop(first, 'serve-revised')
    first.get_by_role('button', name='Dismiss').click()

    # Respond to a comment on patchwork
    second = page.locator('.card.review', has_text='Patch 2')
    second.get_by_role('button', name='Comments').click()
    second.get_by_role('button', name='Respond').click()
    second.locator('textarea').fill('Agree, and ask Alex to add it')
    shots.crop(second, 'serve-comments')
    second.get_by_role('button', name='Suggest a response').click()
    second = page.locator('.card.review', has_text='Patch 2')
    response = second.locator('.comments', has_text='Your response')
    response.wait_for(timeout=30000)
    shots.crop(response, 'serve-response')

    # Send the reviews
    send = page.get_by_role('button', name='Send 3 drafts').element_handle()
    send.click()
    shots.crop(page.locator('#main > .card').first, 'serve-send')
    send.click()
    page.wait_for_function(
        "document.querySelectorAll('.card.review .rs-todo').length === 0",
        timeout=30000)
    shots.go('#/reviews')
    page.locator('.card', has_text='Ample AB1234').wait_for()
    shots.screen('serve-reviews')

    # A reply to an earlier review
    page.locator('.card', has_text='Example SoC Ethernet').click()
    reply = page.locator('.comments', has_text='Jo Placeholder')
    reply.wait_for()
    shots.confirm(reply.get_by_role('button', name='Suggest a response'))
    reply.locator('.section', has_text='Your response').wait_for(
        timeout=30000)
    shots.crop(page.locator('.card.review').first, 'serve-reply')


def shrink(paths):
    """Reduce each picture to a palette of colours, if Pillow is installed

    Args:
        paths (list of str): Pictures to change
    """
    try:
        # pylint: disable=C0415
        from PIL import Image
    except ImportError:
        print('Pillow is not installed, so the pictures are not reduced')
        return
    for path in paths:
        with Image.open(path) as img:
            out = img.convert('RGB').quantize(colors=128)
        out.save(path, optimize=True)


def main():
    """Take the pictures"""
    outdir = os.path.realpath(sys.argv[1] if len(sys.argv) > 1 else
                              os.path.join(HERE, '..', 'guide', 'images'))
    server = subprocess.Popen(
        [sys.executable, os.path.join(HERE, 'serve_demo.py'), '--port',
         str(PORT)], stdout=subprocess.DEVNULL)
    try:
        wait_for_server()
        with sync_playwright() as play:
            browser = play.chromium.launch(channel='chrome')
            context = browser.new_context(
                viewport=VIEW, device_scale_factor=SCALE,
                color_scheme='light')
            context.add_init_script(STYLE)
            page = context.new_page()
            shots = Shots(page, outdir)
            try:
                run(shots)
            except Exception:
                # Show where it went wrong
                page.screenshot(path=os.path.join(outdir, 'error.png'))
                raise
            browser.close()
        paths = [os.path.join(outdir, f'{name}.png') for name in shots.names]
        shrink(paths)
        print(f"Wrote {len(paths)} pictures to {outdir}")
    finally:
        server.terminate()
        server.wait()


if __name__ == '__main__':
    main()
