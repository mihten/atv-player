// LTVPlayer: inline SVG icons (drawn here, 24x24 grid, stroke = currentColor). icon(name, size) -> markup string.

const P = {
  shuffle: '<path d="M3 7h3.5c2 0 3.2 1 4.3 2.6l2.4 4.8C14.3 16 15.5 17 17.5 17H21"/><path d="M3 17h3.5c1.4 0 2.4-.5 3.2-1.4"/>' +
    '<path d="M14.3 8.4C15 7.5 16 7 17.5 7H21"/><path d="M18.5 4.5 21 7l-2.5 2.5"/><path d="M18.5 14.5 21 17l-2.5 2.5"/>',
  playlists: '<path d="M4 6h11"/><path d="M4 11h11"/><path d="M4 16h7"/><circle cx="17" cy="17.5" r="2.5"/><path d="M19.5 17.5V8l2 1"/>',
  folder: '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4.2l2 2.2h8.8A1.5 1.5 0 0 1 21 9.7v8.8a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5z"/>',
  videos: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M10 9.2v5.6l4.8-2.8z" fill="currentColor"/>',
  queue: '<path d="M9 6h12"/><path d="M9 12h12"/><path d="M9 18h12"/><path d="M4 5v3"/><path d="M3.5 11.2h1.6L3.5 13h1.8"/>' +
    '<path d="M3.5 16.5h1.7l-.9 1.2c.7 0 1 .4 1 .9s-.4.9-1 .9h-.8"/>',
  film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16"/><path d="M17 4v16"/><path d="M3 9h4"/><path d="M3 15h4"/>' +
    '<path d="M17 9h4"/><path d="M17 15h4"/>',
  play: '<path d="M7 4.5v15l12-7.5z" fill="currentColor"/>',
  pause: '<rect x="6" y="4.5" width="4" height="15" rx="1" fill="currentColor"/><rect x="14" y="4.5" width="4" height="15" rx="1" fill="currentColor"/>',
  heart: '<path d="M12 20s-7.5-4.6-7.5-10A4.3 4.3 0 0 1 12 7.4 4.3 4.3 0 0 1 19.5 10c0 5.4-7.5 10-7.5 10z" fill="currentColor"/>',
  note: '<path d="M9 18V5l11-2v13"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>',
  chevronLeft: '<path d="M15 4.5 7.5 12l7.5 7.5"/>',
  chevronRight: '<path d="M9 4.5 16.5 12 9 19.5"/>',
  playOutline: '<path d="M7 4.5v15l12-7.5z"/>',
  drive: '<rect x="3" y="13" width="18" height="7" rx="2"/><path d="M5.5 13 8 5h8l2.5 8"/><circle cx="17" cy="16.5" r=".9" fill="currentColor"/>',
  warning: '<path d="M12 4 2.8 19.5h18.4z"/><path d="M12 10v4.5"/><circle cx="12" cy="17" r=".9" fill="currentColor"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4M5.5 5.5l1.7 1.7M16.8 16.8l1.7 1.7M5.5 18.5l1.7-1.7M16.8 7.2l1.7-1.7"/>' +
    '<circle cx="12" cy="12" r="6.3"/>',
  stop: '<rect x="5.5" y="5.5" width="13" height="13" rx="2" fill="currentColor"/>',
  close: '<path d="M6 6l12 12"/><path d="M18 6 6 18"/>',
  heartOutline: '<path d="M12 20s-7.5-4.6-7.5-10A4.3 4.3 0 0 1 12 7.4 4.3 4.3 0 0 1 19.5 10c0 5.4-7.5 10-7.5 10z"/>',
  speaker: '<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z" fill="currentColor"/><path d="M15.5 9a4.5 4.5 0 0 1 0 6"/><path d="M18 6.5a8 8 0 0 1 0 11"/>',
  speakerOff: '<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z" fill="currentColor"/><path d="M16 9.5l5 5"/><path d="M21 9.5l-5 5"/>',
  headphones: '<path d="M4 15v-3a8 8 0 0 1 16 0v3"/><rect x="3.5" y="14" width="4" height="6" rx="1.5" fill="currentColor"/>' +
    '<rect x="16.5" y="14" width="4" height="6" rx="1.5" fill="currentColor"/>',
  tv: '<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8.5 20.5h7"/>',
  captions: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M7 11.5h4"/><path d="M13 11.5h4"/><path d="M7 15h7"/>',
  sliders: '<path d="M6 4v16M12 4v16M18 4v16"/><rect x="4" y="13" width="4" height="3" rx="1" fill="currentColor"/>' +
    '<rect x="10" y="7" width="4" height="3" rx="1" fill="currentColor"/><rect x="16" y="11" width="4" height="3" rx="1" fill="currentColor"/>',
  wave: '<path d="M3 12h2M7 8v8M11 5v14M15 9v6M19 7v10M21 12h0"/>',
  boost: '<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z" fill="currentColor"/><path d="M16 12h5"/><path d="M18.5 9.5v5"/>',
  layers: '<path d="M12 4 3 8.5l9 4.5 9-4.5z"/><path d="M3 12.5l9 4.5 9-4.5"/><path d="M3 16.5l9 4.5 9-4.5"/>',
  palette: '<path d="M12 3a9 9 0 1 0 0 18c1.2 0 1.8-.8 1.8-1.7 0-1-.9-1.4-.9-2.4 0-1 .8-1.6 1.8-1.6H17a4 4 0 0 0 4-4C21 6.7 17 3 12 3z"/>' +
    '<circle cx="7.5" cy="11" r="1.2" fill="currentColor"/><circle cx="10" cy="7" r="1.2" fill="currentColor"/><circle cx="15" cy="7" r="1.2" fill="currentColor"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6"/><circle cx="12" cy="7.8" r="1" fill="currentColor"/>',
  playCircle: '<circle cx="12" cy="12" r="9"/><path d="M10 8.5v7l6-3.5z" fill="currentColor"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  trash: '<path d="M4.5 7h15"/><path d="M9 7V4.5h6V7"/><path d="M6.5 7l1 13h9l1-13"/>',
  pencil: '<path d="M4 20l1-4.5L15.5 5l3.5 3.5L8.5 19z"/><path d="M13.5 7l3.5 3.5"/>',
  lyrics: '<path d="M4 6h12"/><path d="M4 10.5h9"/><path d="M4 15h6"/><path d="M17 17.5V9l3.5-1"/><circle cx="15" cy="17.5" r="2"/>',
};

export function icon(name, size = 40, extra = "") {
  return `<svg class="ico ${extra}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
         `stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || ""}</svg>`;
}
