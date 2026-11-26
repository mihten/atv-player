// LTVPlayer: the settings kept on this TV (localStorage of the page's origin), with the Apple TV app's defaults.
// Sound settings (equalizer, Boost, Loudness), Prefer videos and the mixes live on the server; everything here is the
// TV's own: look, list text size, jump steps, keep music playing, the lyrics style, the sound output and lip sync.
// settings.get(key) / settings.set(key, value) (saved at once) / settings.on(fn) (fn(key, value) after every change).

const STORE = "ltv.settings";

export const DEFAULTS = {
  effects: true,         // Theme effects: the backdrop drifts (menus and the lyrics screen)
  textSize: "normal",    // list rows: normal | compact | small
  seekBack: 10, seekForward: 10,
  keepMusic: true,       // leaving the app: music keeps playing, music videos pause
  subsOn: true, subLang: "", subLang2: "",   // lyrics on the video: language "" = the file's default; second = on top
  subSize: 0, subFont: "", subColor: "#ffffff", subBold: false, subOutline: 4, subBox: 0, subMargin: 0,
  lyricsScreen: false,   // the lyrics instead of the picture (hold OK), remembered like on the Apple TV
  headphones: false,     // sound output: false = the TV's own sound, true = the atv-audio headphones
  lipSync: -100,         // ms, the headphones' sound earlier (-) / later (+), -200..400 in 20 ms steps
  mixSubfolders: true,   // Settings → Mixes → Add folder: include its subfolders
};

export const CHOICES = {
  textSize: [["normal", "Normal"], ["compact", "Compact"], ["small", "Small"]],
  seekBack: [5, 10, 15, 30].map((n) => [n, `${n} seconds`]),
  seekForward: [10, 15, 30, 60].map((n) => [n, `${n} seconds`]),
  subLang: [["", "Default (as set in the file)"], ["eng", "English"], ["hrv", "Croatian"]],
  subLang2: [["", "Off"], ["eng", "English"], ["hrv", "Croatian"]],
  subSize: [[0, "Default"], [20, "Small"], [16, "Medium"], [12, "Large"], [9, "Extra large"]],
  subFont: [["", "Default"], ["serif", "Serif"], ["sans", "Sans serif"], ["mono", "Monospace"]],
  subColor: [["#ffffff", "White"], ["#ffff00", "Yellow"], ["#c8c8c8", "Light grey"], ["#00ffff", "Cyan"]],
  subOutline: [[0, "None"], [2, "Thin"], [4, "Normal"], [6, "Thick"]],
  subBox: [[0, "None"], [96, "Light"], [176, "Dark"], [255, "Solid"]],
  subMargin: [[0, "Bottom"], [40, "Slightly higher"], [100, "Higher"]],
  lipSync: Array.from({ length: 31 }, (_, i) => -200 + i * 20).map((ms) => [ms, ms > 0 ? `+${ms} ms` : ms < 0 ? `\u2212${-ms} ms` : "0 ms"]),
};

export const label = (key, value) => (CHOICES[key] || []).find(([v]) => v === value)?.[1] ?? String(value);

class Settings {
  constructor() {
    this.listeners = [];
    this.stored = 0;
    this.v = { ...DEFAULTS };
    try {
      const raw = JSON.parse(localStorage.getItem(STORE) || "{}");
      for (const k of Object.keys(DEFAULTS)) {
        if (k in raw && typeof raw[k] === typeof DEFAULTS[k]) { this.v[k] = raw[k]; this.stored += 1; }
      }
    } catch (e) { /* no storage: defaults for this run */ }
  }

  get(k) { return this.v[k]; }

  set(k, value) {
    if (this.v[k] === value) return;
    this.v[k] = value;
    try { localStorage.setItem(STORE, JSON.stringify(this.v)); } catch (e) { /* kept for this run */ }
    for (const fn of this.listeners) fn(k, value);
  }

  on(fn) { this.listeners.push(fn); }
}

export const settings = new Settings();
