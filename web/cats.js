/* Котики: детерминированные SVG-аватары. catSVG(name, kind) -> строка svg. kind: "team" (взрослый кот) | "intern" (котёнок) */
(() => {
  const FUR = ["#f4a259", "#9aa3b2", "#454457", "#f1efe9", "#b07a4f", "#e9d5b8", "#7d8aa5", "#d97b3b", "#c9a7e0", "#8fb8de"];
  const DARK = ["#c97a2e", "#6e7787", "#26262f", "#c9c5bb", "#7f5330", "#c2ab86", "#586380", "#a85a22", "#9b78b3", "#5f8fb6"];
  const EYES = ["#7ed37e", "#ffc857", "#7cc7ff", "#f6e27a", "#9be7c4"];
  const COLLAR = ["#ff5c7a", "#4f8cff", "#34d27b", "#f7931a", "#a99cff"];
  const hash = (s) => { let h = 0; for (const ch of s) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return h; };

  window.catSVG = function catSVG(name, kind = "team", size = 40) {
    const h = hash(name || "cat");
    const fi = h % FUR.length, fur = FUR[fi], dark = DARK[fi], eye = EYES[(h >> 4) % EYES.length], collar = COLLAR[(h >> 8) % COLLAR.length];
    const tabby = (h >> 12) % 3 === 0, patch = (h >> 14) % 4 === 0;
    const kitten = kind === "intern";
    const r = kitten ? 19 : 22, cy = kitten ? 37 : 36;
    const ex = kitten ? 5.2 : 4.2, ey = kitten ? 6 : 5;
    const earL = kitten ? "M16,26 L18,10 L30,20 Z" : "M13,24 L15,5 L31,18 Z";
    const earR = kitten ? "M48,26 L46,10 L34,20 Z" : "M51,24 L49,5 L33,18 Z";
    const earLi = kitten ? "M19,25 L20,14 L28,21 Z" : "M17,23 L18,10 L28,19 Z";
    const earRi = kitten ? "M45,25 L44,14 L36,21 Z" : "M47,23 L46,10 L36,19 Z";
    return `<svg class="cat ${kind}" viewBox="0 0 64 64" width="${size}" height="${size}" aria-label="${name}">
      <path d="${earL}" fill="${fur}"/><path d="${earR}" fill="${fur}"/>
      <path d="${earLi}" fill="#f7b2c4" opacity=".8"/><path d="${earRi}" fill="#f7b2c4" opacity=".8"/>
      <circle cx="32" cy="${cy}" r="${r}" fill="${fur}"/>
      ${patch ? `<path d="M32,${cy - r} a${r},${r} 0 0 1 ${r},${r} L32,${cy} Z" fill="${dark}" opacity=".55"/>` : ""}
      ${tabby ? `<path d="M26,${cy - r + 4} q6,-4 12,0 M23,${cy - r + 9} q9,-4 18,0" stroke="${dark}" stroke-width="2" fill="none" stroke-linecap="round" opacity=".6"/>` : ""}
      ${kitten ? `<circle cx="20" cy="${cy + 6}" r="4" fill="#ff8fa3" opacity=".45"/><circle cx="44" cy="${cy + 6}" r="4" fill="#ff8fa3" opacity=".45"/>` : ""}
      <ellipse cx="24" cy="${cy}" rx="${ex}" ry="${ey}" fill="${eye}"/><ellipse cx="40" cy="${cy}" rx="${ex}" ry="${ey}" fill="${eye}"/>
      <ellipse cx="24" cy="${cy + 0.5}" rx="${kitten ? 2.4 : 1.6}" ry="${ey - 1}" fill="#1a1432"/><ellipse cx="40" cy="${cy + 0.5}" rx="${kitten ? 2.4 : 1.6}" ry="${ey - 1}" fill="#1a1432"/>
      <circle cx="25.5" cy="${cy - 2}" r="1.3" fill="#fff"/><circle cx="41.5" cy="${cy - 2}" r="1.3" fill="#fff"/>
      <path d="M29.5,${cy + 7} L34.5,${cy + 7} L32,${cy + 9.5} Z" fill="#f28ba8"/>
      <path d="M32,${cy + 9.5} q-3,4 -6,1 M32,${cy + 9.5} q3,4 6,1" stroke="#1a1432" stroke-width="1.4" fill="none" stroke-linecap="round" opacity=".7"/>
      <path d="M8,${cy + 5} L22,${cy + 8} M8,${cy + 11} L22,${cy + 10} M56,${cy + 5} L42,${cy + 8} M56,${cy + 11} L42,${cy + 10}" stroke="${kitten ? "#fff" : "#f1efe9"}" stroke-width="${kitten ? 1 : 1.3}" opacity=".75" stroke-linecap="round"/>
      ${!kitten ? `<path d="M14,${cy + 17} q18,10 36,0" stroke="${collar}" stroke-width="4" fill="none" stroke-linecap="round"/><circle cx="32" cy="${cy + 21}" r="2.6" fill="#ffd166"/>` : ""}
    </svg>`;
  };
})();
