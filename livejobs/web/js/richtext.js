// A job description is plain text, with any pictures the company pasted into
// it carried inline as [data:image/png;base64,...]. This splits a description
// into text and picture parts so the page can show the pictures where they
// were. Nothing is parsed as HTML: text goes in as text nodes, and a picture
// is only used if it is a real, small set of raster formats (never SVG).
const DATA = /\[?[ \t]*(data:[\w.+/-]+;base64,[A-Za-z0-9+/=_-]+)[ \t]*\]?/g;
const IMAGE = /^data:image\/(?:png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=_-]+$/;

export function splitDescription(text) {
  const out = [];
  let pos = 0;
  const s = text || '';
  for (const m of s.matchAll(DATA)) {
    if (m.index > pos) out.push({ text: s.slice(pos, m.index) });
    if (IMAGE.test(m[1])) out.push({ image: m[1] });
    else out.push({ text: '[image]' });
    pos = m.index + m[0].length;
  }
  if (pos < s.length) out.push({ text: s.slice(pos) });
  return out;
}

// DOM nodes for a description: text nodes and <img> elements.
export function renderDescription(text, h) {
  return splitDescription(text).map((part) => (part.image
    ? h('img', { class: 'desc-img', src: part.image, alt: 'Image from the job description', loading: 'lazy' })
    : part.text));
}
