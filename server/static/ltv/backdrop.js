// LTVPlayer: the Aurora background (port of AuroraBackground's pre-18 branch in Shared/Backdrop.swift): base colour +
// four soft colour fields (tones 0-3, 70 % of the long side, at (-0.3,-0.25) (0.3,-0.2) (-0.2,0.3) (0.25,0.25)) + a
// top-to-bottom black gradient 10 % -> 35 % -> 62 % + a black dim of (1 - intensity) * 0.8. The fields drift with long
// CSS transform animations; colours come from the --base / --t0..--t3 variables (they cross-fade on a palette change).
// still(true) pauses the drift (under a playing video and while the player screen is up).

const OFFSETS = [[-0.3, -0.25], [0.3, -0.2], [-0.2, 0.3], [0.25, 0.25]];

export class Backdrop {
  constructor(root, intensity = 0.9) {
    this.root = root;
    root.classList.add("backdrop");
    const side = 1920 * 0.7;
    root.innerHTML = OFFSETS.map(([x, y], i) => {
      const left = 960 + x * 1920 - side / 2, top = 540 + y * 1920 - side / 2;
      return `<div class="bd-field bd-f${i}" style="left:${left}px;top:${top}px;width:${side}px;height:${side}px;` +
             `background:radial-gradient(circle closest-side, var(--t${i}) 0%, var(--t${i}) 30%, transparent 100%)"></div>`;
    }).join("") + `<div class="bd-shade"></div><div class="bd-dim" style="opacity:${Math.max(0, 1 - intensity) * 0.8}"></div>`;
  }

  still(on) {
    this.root.classList.toggle("still", !!on);
  }
}
