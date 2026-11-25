// LTVPlayer device probe, part U1: if this module runs, the engine loads ES modules with modern syntax
// (optional chaining, nullish, private fields, async / await, top-level await). probe.js reads window.ltvModern.
class Box {
  #v = 1;
  get v() { return this.#v; }
}
const checks = {
  optional: ({ a: { b: 1 } })?.a?.b === 1,
  nullish: (null ?? 2) === 2,
  arrow: [1, 2].map((x) => x * 2).join() === "2,4",
  spread: [...[1, 2], ...new Set([3])].length === 3,
  privateField: new Box().v === 1,
  at: [1, 2, 3].at(-1) === 3,
  bigint: typeof 1n === "bigint",
  structuredClone: typeof structuredClone === "function",
};
checks.topLevelAwait = (await Promise.resolve(7)) === 7;
const asyncFn = async () => { await null; return true; };
checks.asyncAwait = await asyncFn();
window.ltvModern = { ok: Object.values(checks).every(Boolean), checks };
