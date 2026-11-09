"""Screensaver of mtv-player: after a long pause (or with nothing playing) the screen goes black and a dim name moves
around it, so an OLED panel never shows the same pixels for hours.

Pure model + renderer (no mpv, no clock of its own): tick(busy) is called every ~0.25 s with "is something happening"
(playing, a menu or a learn screen open) and says what to do with the overlay: "show", "move", "hide" or None.
wake() is called for every remote key: while the screensaver is on the key only closes it (it never does its normal
function, so a press meant to wake the screen cannot pause, skip or stop anything).
"""
import random

RES_X, RES_Y = 1920, 1080
AFTER = 300.0            # s paused / stopped / idle before the screensaver comes up
MOVE = 180.0             # s between two jumps of the text
TEXT = "MTVPlayer"
SIZE = 96                # px
COLOR = "&H606060&"      # dim grey (BGR): readable in a dark room, easy on the panel
# the text is centred on its spot, so these margins keep all of it on the screen
X_MIN, X_MAX, Y_MIN, Y_MAX = 420, RES_X - 420, 140, RES_Y - 140
MIN_JUMP = 400           # px a new spot is at least away from the old one


class Saver:
    def __init__(self, mono, rng=None, after=AFTER, move=MOVE):
        self.mono = mono
        self.rng = rng or random.Random()
        self.after, self.move = after, move
        self.on = False
        self.idle_since = mono()
        self.moved_at = 0.0
        self.woke_at = -1e9
        self.pos = (RES_X // 2, RES_Y // 2)

    def _jump(self):
        old = new = self.pos
        for _ in range(50):
            new = (self.rng.randint(X_MIN, X_MAX), self.rng.randint(Y_MIN, Y_MAX))
            if (new[0] - old[0]) ** 2 + (new[1] - old[1]) ** 2 >= MIN_JUMP ** 2:
                break
        self.pos = new
        self.moved_at = self.mono()

    def tick(self, busy):
        """\"show\" (come up), \"move\" (new spot), \"hide\" (activity again) or None."""
        now = self.mono()
        if busy:
            self.idle_since = now
            if self.on:
                self.on = False
                return "hide"
            return None
        if not self.on:
            if now - self.idle_since >= self.after:
                self.on = True
                self._jump()
                return "show"
            return None
        if now - self.moved_at >= self.move:
            self._jump()
            return "move"
        return None

    def wake(self):
        """A remote key. True when the screensaver was on (the key only closed it)."""
        self.idle_since = self.mono()
        if self.on:
            self.on = False
            self.woke_at = self.mono()
            return True
        return False

    def recently_woke(self, within=2.0):
        """A held key keeps repeating after the press that woke the screen: those repeats do nothing either."""
        return self.mono() - self.woke_at < within

    def render(self):
        """ASS events: a black screen with the name at the current spot."""
        x, y = self.pos
        black = (f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c&H000000&\\1a&H00&\\p1}}"
                 f"m 0 0 l {RES_X} 0 {RES_X} {RES_Y} 0 {RES_Y}{{\\p0}}")
        text = f"{{\\an5\\pos({x},{y})\\fs{SIZE}\\bord0\\shad0\\1c{COLOR}\\b0}}{TEXT}"
        return black + "\n" + text
