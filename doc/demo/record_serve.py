#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Record a video of the 'patman serve' page, for the docs and website

Usage: record_serve.py [OUTPUT.webm]

This runs serve_demo.py, which serves made-up series and fakes the jobs, so
nothing is reviewed or sent for real. It then drives Chrome, using Playwright,
through reviewing a series from the inbox, reading and changing a review and
sending the reviews, and records it. A dot shows where each tap goes and a
caption says what is happening.

Chrome runs in an Xvfb display, which ffmpeg records, so this needs
Playwright ('pip install playwright'), Google Chrome, Xvfb and ffmpeg. It
writes OUTPUT.webm (default serve-demo.webm), with an MP4 copy and a poster
image beside it.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.realpath(__file__))
PORT = 8051
URL = f'http://127.0.0.1:{PORT}/'

# The display is the size of the video. The page is scaled up, so that the
# text is easy to read, which makes it this size
VIDEO = {'width': 1280, 'height': 720}
SCALE = 1.2
VIEW = {'width': round(VIDEO['width'] / SCALE),
        'height': round(VIDEO['height'] / SCALE)}
DISPLAY = ':97'

# A dot which follows the mouse, since the recording does not show the
# pointer, and a caption at the bottom of the page
OVERLAY = '''
window.addEventListener('DOMContentLoaded', () => {
  const style = document.createElement('style');
  style.textContent = `
    #demo-dot { position: fixed; width: 22px; height: 22px; margin: -11px;
      border-radius: 50%; background: rgba(255, 166, 0, 0.55);
      border: 2px solid rgba(200, 110, 0, 0.9); pointer-events: none;
      z-index: 100000; left: -40px; top: -40px; transition: transform 0.1s; }
    #demo-dot.down { transform: scale(0.6); }
    #demo-caption { position: fixed; left: 50%; bottom: 88px;
      transform: translateX(-50%); max-width: 80%; padding: 8px 16px;
      border-radius: 8px; background: rgba(20, 30, 50, 0.85); color: #fff;
      font: 600 15px system-ui, sans-serif; text-align: center;
      pointer-events: none; z-index: 100000; opacity: 0;
      transition: opacity 0.3s; }
    #demo-caption.on { opacity: 1; }
    html { scrollbar-width: none; }`;
  document.head.append(style);
  const dot = document.createElement('div');
  dot.id = 'demo-dot';
  const cap = document.createElement('div');
  cap.id = 'demo-caption';
  document.body.append(dot, cap);
  document.addEventListener('mousemove', (ev) => {
    dot.style.left = `${ev.clientX}px`;
    dot.style.top = `${ev.clientY}px`;
  }, true);
  document.addEventListener('mousedown', () => dot.classList.add('down'), true);
  document.addEventListener('mouseup', () => dot.classList.remove('down'), true);
  window.demoCaption = (text) => {
    cap.classList.remove('on');
    if (text) {
      setTimeout(() => {
        cap.textContent = text;
        cap.classList.add('on');
      }, 300);
    }
  };
});
'''


class Demo:
    """Drive the page, moving the dot to each thing before tapping it

    Properties:
        page (playwright.sync_api.Page): Page being recorded
    """
    def __init__(self, page):
        self.page = page

    @staticmethod
    def pause(secs):
        """Wait, so that the viewer can see what happened"""
        time.sleep(secs)

    def caption(self, text, secs=0.0):
        """Show a caption, then wait"""
        self.page.evaluate('(t) => window.demoCaption(t)', text)
        self.pause(secs)

    def show(self, loc, block='center'):
        """Scroll smoothly so that something is in view"""
        loc.wait_for()
        loc.evaluate(f"el => el.scrollIntoView({{behavior: 'smooth', "
                     f"block: '{block}'}})")
        self.pause(0.9)

    def point(self, loc):
        """Move the dot to the middle of something, scrolling to it if needed

        The top bar, the tabs at the bottom and the caption cover part of the
        page, so anything near them is scrolled to the middle first.

        Return:
            tuple: (x, y) of the middle
        """
        box = loc.bounding_box()
        if box['y'] < 60 or box['y'] + box['height'] > VIEW['height'] - 140:
            self.show(loc)
            box = loc.bounding_box()
        x = box['x'] + box['width'] / 2
        y = box['y'] + box['height'] / 2
        self.page.mouse.move(x, y, steps=25)
        self.pause(0.3)
        return x, y

    def tap(self, loc, after=0.8, away=False):
        """Move to something and tap it

        Args:
            loc (Locator): What to tap
            after (float): Seconds to wait afterwards
            away (bool): True to move off it afterwards, e.g. for a link,
                since Chrome shows where a link goes while the mouse is
                over it
        """
        loc.wait_for()
        x, y = self.point(loc)
        self.page.mouse.click(x, y)
        if away:
            # The margin to the right of the page has no links
            self.page.mouse.move(VIEW['width'] - 40, VIEW['height'] / 2,
                                 steps=15)
        self.pause(after)

    def confirm(self, loc, after=1.0):
        """Tap a button which needs a second tap to confirm

        The button's label changes after the first tap, so the second tap
        goes to the same place
        """
        loc.wait_for()
        x, y = self.point(loc)
        self.page.mouse.click(x, y)
        self.pause(0.8)
        self.page.mouse.click(x, y)
        self.pause(after)

    def type(self, text):
        """Type, a character at a time"""
        self.page.keyboard.type(text, delay=45)
        self.pause(0.5)


def wait_for_server():
    """Wait until the demo server answers"""
    for _ in range(100):
        try:
            with urllib.request.urlopen(f'{URL}api/version', timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError('The demo server did not start')


def run(demo):
    """Go through the demo, starting with the inbox

    Args:
        demo (Demo): Page to drive
    """
    page = demo.page
    demo.caption('patman serve: review patches from your browser', 2.5)

    # The inbox
    card = page.locator('.card.patch', has_text='Ample AB1234')
    demo.caption('The inbox lists recent series on patchwork', 1.5)
    demo.tap(card.locator('.name'), after=1.5)
    demo.caption('Start a review, with some context for the agent', 0.5)
    demo.tap(card.get_by_role('button', name='Review', exact=True))
    demo.tap(card.locator('textarea'), after=0.3)
    demo.type('Focus on the driver; the docs patch is minor')
    demo.tap(card.get_by_role('button', name='Start the review'), after=1.5)
    demo.caption('The review runs as a job, while the inbox stays open', 2.5)

    # The job
    demo.tap(page.locator('nav a[data-tab="jobs"]'), after=1, away=True)
    demo.caption('Follow the job as the agent reviews each patch', 0.5)
    demo.tap(page.locator('.card', has_text='Review series').first, after=1,
             away=True)
    page.locator('.chip', has_text='ok').wait_for(timeout=60000)
    demo.pause(1.5)

    # The reviews
    demo.caption('Each review is ready in a Gmail draft', 0.3)
    demo.tap(page.locator('.card a[href^="#/reviews/"]', has_text='Reviews'),
             after=2, away=True)
    first = page.locator('.card.review', has_text='Patch 1')
    demo.show(first.locator('.mail'), block='start')
    demo.pause(1.5)
    demo.caption('Read each review next to its patch', 0.3)
    demo.tap(first.get_by_role('button', name='Show the patch'), after=1)
    demo.show(first.locator('.inline-mail .diff, .inline-mail').first)
    demo.pause(2)
    demo.tap(first.get_by_role('button', name='Hide the patch'), after=1)

    # Change a comment
    demo.caption('Change a comment; its Gmail draft is updated too', 0.3)
    edit = first.locator('.chunk-btn', has_text='Edit').nth(1)
    demo.show(edit)
    demo.tap(edit, after=0.5)
    area = first.locator('textarea')
    demo.tap(area, after=0.2)
    page.keyboard.press('Control+End')
    demo.type(' Is 20ms enough?')
    demo.tap(first.get_by_role('button', name='Save'), after=1)
    first.locator('.jobstrip', has_text='done').wait_for(timeout=30000)
    demo.pause(1.5)

    # Send them
    demo.caption('Send the drafts (in this demo, nothing is really sent)', 0.3)
    send = page.get_by_role('button', name='Send 3 drafts')
    demo.show(send, block='start')
    demo.confirm(send)
    page.locator('.card.review .rs-done').first.wait_for(timeout=30000)
    page.wait_for_function(
        "document.querySelectorAll('.card.review .rs-todo').length === 0",
        timeout=30000)
    demo.pause(1)
    demo.show(page.locator('.card.review', has_text='Patch 1'), block='start')
    demo.caption('Sent: replies will show here as they arrive', 2.5)
    demo.tap(page.locator('nav a[data-tab="reviews"]'), after=1, away=True)
    demo.caption('The list of reviews shows what still needs doing', 3)
    demo.caption('')
    demo.pause(0.5)


def main():
    """Record the demo and write the video files"""
    out = os.path.realpath(sys.argv[1] if len(sys.argv) > 1 else
                           'serve-demo.webm')
    tmp = tempfile.mkdtemp(prefix='patman-video-')
    raw = os.path.join(tmp, 'raw.mkv')
    size = f"{VIDEO['width']}x{VIDEO['height']}"
    procs = []
    try:
        procs.append(subprocess.Popen(
            [sys.executable, os.path.join(HERE, 'serve_demo.py'), '--port',
             str(PORT)], stdout=subprocess.DEVNULL))
        procs.append(subprocess.Popen(
            ['Xvfb', DISPLAY, '-screen', '0', f'{size}x24', '-nolisten',
             'tcp']))
        wait_for_server()
        with sync_playwright() as play:
            browser = play.chromium.launch(
                channel='chrome', headless=False,
                env={**os.environ, 'DISPLAY': DISPLAY},
                # The window size is in scaled pixels
                args=[f"--window-size={VIEW['width']},{VIEW['height']}",
                      '--window-position=0,0',
                      f'--force-device-scale-factor={SCALE}'])
            context = browser.new_context(no_viewport=True)
            context.add_init_script(OVERLAY)
            page = context.new_page()

            # Fill the display, without the tabs and address bar
            cdp = context.new_cdp_session(page)
            win = cdp.send('Browser.getWindowForTarget')['windowId']
            cdp.send('Browser.setWindowBounds',
                     {'windowId': win, 'bounds': {'windowState': 'fullscreen'}})
            page.goto(f'{URL}#/inbox')
            page.wait_for_function(
                f"window.innerHeight >= {VIEW['height'] - 1}", timeout=10000)

            # Wait for Chrome's note about leaving full screen to go
            Demo.pause(5)
            grab = subprocess.Popen(
                ['ffmpeg', '-loglevel', 'error', '-y', '-f', 'x11grab',
                 '-draw_mouse', '0', '-framerate', '25', '-video_size', size,
                 '-i', DISPLAY, '-c:v', 'libx264rgb', '-crf', '0', '-preset',
                 'ultrafast', raw], stdin=subprocess.PIPE)
            try:
                run(Demo(page))
            except Exception:
                # Show where it went wrong
                page.screenshot(path=f'{os.path.splitext(out)[0]}-error.png')
                raise
            finally:
                grab.communicate(b'q')
            browser.close()
        subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', raw,
                        '-c:v', 'libvpx-vp9', '-b:v', '0', '-crf', '32',
                        '-row-mt', '1', '-pix_fmt', 'yuv420p', out],
                       check=True)
        mp4 = f'{os.path.splitext(out)[0]}.mp4'
        subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', raw,
                        '-c:v', 'libx264', '-crf', '26', '-preset', 'veryslow',
                        '-tune', 'animation', '-pix_fmt', 'yuv420p',
                        '-movflags', '+faststart', mp4], check=True)
        png = f'{os.path.splitext(out)[0]}.png'
        subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-ss', '30',
                        '-i', raw, '-frames:v', '1', png], check=True)
        print(f'Wrote {out}, {mp4} and {png}')
    finally:
        for proc in procs:
            proc.terminate()
            proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
