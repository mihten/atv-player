"""Screensaver of mtv-player: a black screen with a dim text that moves around, so an OLED panel never shows the same pixels
for hours.

Two texts: "MTVPlayer" after a long pause / idle (AFTER seconds without activity) and "MTV Player stopped" at once when playback
was stopped (Stop). The text jumps to a new spot every MOVE seconds and always stays fully on the screen (the bounds follow the
text's measured width, see text_box()).

Pure model + renderer (no mpv, no clock of its own): tick(busy, stopped) is called every ~0.25 s and says what to do with the
overlay: "show", "move" (also when the text changes), "hide" or None. wake() is called for every remote key: while the
screensaver is on the key only closes it (it never does its normal function).
"""
import random

RES_X, RES_Y = 1920, 1080
AFTER = 300.0            # s paused / idle before the screensaver comes up
MOVE = 180.0             # s between two jumps of the text
IDLE_TEXT, IDLE_SIZE = "MTVPlayer", 240
STOPPED_TEXT, STOPPED_SIZE = "MTV Player stopped", 150
COLOR = "&H606060&"      # dim grey (BGR): readable in a dark room, easy on the panel
WIDTH_PER_EM = 0.52      # measured with libass (sans-serif): 0.49 for "MTVPlayer", 0.47 for "MTV Player stopped"; rounded up
PAD = 60                 # px kept free around the text
MIN_JUMP = 400           # px a new spot is at least away from the old one


def text_box(text, size):
    """(x_min, x_max, y_min, y_max) of the centre of the text so that all of it stays on the screen."""
    half_w = int(WIDTH_PER_EM * len(text) * size / 2) + PAD
    half_h = int(size * 0.5) + PAD
    return half_w, RES_X - half_w, half_h, RES_Y - half_h


class Saver:
    def __init__(self, mono, rng=None, after=AFTER, move=MOVE):
        self.mono = mono
        self.rng = rng or random.Random()
        self.after, self.move = after, move
        self.on = False
        self.stopped = False       # the text on the screen (when on) is the stopped one
        self.was_stopped = False   # last tick's `stopped`, to see the moment of a Stop
        self.idle_since = mono()
        self.moved_at = 0.0
        self.woke_at = -1e9
        self.pos = (RES_X // 2, RES_Y // 2)

    @property
    def text(self):
        return (STOPPED_TEXT, STOPPED_SIZE) if self.stopped else (IDLE_TEXT, IDLE_SIZE)

    def _jump(self):
        x0, x1, y0, y1 = text_box(*self.text)
        old = new = self.pos
        for _ in range(50):
            new = (self.rng.randint(x0, x1), self.rng.randint(y0, y1))
            if (new[0] - old[0]) ** 2 + (new[1] - old[1]) ** 2 >= MIN_JUMP ** 2:
                break
        self.pos = new
        self.moved_at = self.mono()

    def tick(self, busy, stopped=False):
        """\"show\" (come up), \"move\" (new spot or new text), \"hide\" (activity again) or None."""
        now = self.mono()
        edge = stopped and not self.was_stopped   # the moment of a Stop: the stopped screen comes up at once
        self.was_stopped = stopped
        if busy:
            self.idle_since = now
            if self.on:
                self.on = False
                return "hide"
            return None
        if not self.on:
            if edge or now - self.idle_since >= self.after:
                self.on = True
                self.stopped = stopped
                self._jump()
                return "show"
            return None
        if stopped != self.stopped:   # the text changes while it is on (a Stop while the idle text shows)
            self.stopped = stopped
            self._jump()
            return "move"
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
        """ASS events: a black screen with the text at the current spot."""
        x, y = self.pos
        text, size = self.text
        black = (f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c&H000000&\\1a&H00&\\p1}}"
                 f"m 0 0 l {RES_X} 0 {RES_X} {RES_Y} 0 {RES_Y}{{\\p0}}")
        label = f"{{\\an5\\pos({x},{y})\\fs{size}\\bord0\\shad0\\1c{COLOR}\\b0}}{text}"
        return black + "\n" + label
