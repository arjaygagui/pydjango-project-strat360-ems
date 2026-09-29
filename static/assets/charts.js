/*
 * Horizontal-scroll bar charts: every bar keeps a readable width and every label stays at
 * full size (wrapped onto lines instead of rotated or squeezed). When there are more bars
 * than fit, the chart scrolls sideways inside its card.
 *
 * Markup:  <div class="chart-wrap scroll-x"><div class="chart-inner"><canvas id="x"></canvas></div></div>
 * Usage:   const L = labels.map(l => wrapLabel(l));     // multi-line labels
 *          sizeScrollChart('x', L, 56);                // before `new Chart(...)`, then labels: L
 */
(function () {
  window.wrapLabel = function (text, max) {
    max = max || 10;
    const words = String(text).split(/\s+/), lines = [];
    let line = '';
    words.forEach(w => {
      if (line && (line + ' ' + w).length > max) { lines.push(line); line = w; }
      else line = line ? line + ' ' + w : w;
    });
    if (line) lines.push(line);
    return lines.length > 1 ? lines : lines[0] || '';
  };

  const sized = [];
  function apply(s) {
    const inner = s.canvas.parentElement, wrap = inner.parentElement;
    const want = s.count * s.perBar + 90;                // + room for the axes
    inner.style.width = Math.max(wrap.clientWidth, want) + 'px';
    wrap.classList.toggle('is-scrolling', want > wrap.clientWidth);
  }

  // `labels` are the (wrapped) chart labels; each bar gets room for the widest label line
  // at full font size, but never less than `minPerBar`.
  window.sizeScrollChart = function (canvasId, labels, minPerBar) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    ctx.font = '500 11px ' + ((window.Chart && Chart.defaults.font.family) || 'sans-serif');
    const widest = Math.max(0, ...labels.flat().map(l => ctx.measureText(String(l)).width));
    const s = { canvas: canvas, count: labels.length, perBar: Math.max(minPerBar || 56, widest + 14) };
    sized.push(s);
    apply(s);
  };

  // Keep filling the card when the window grows, and scrolling when it shrinks.
  let timer;
  window.addEventListener('resize', () => {
    clearTimeout(timer);
    timer = setTimeout(() => sized.forEach(apply), 120);
  });
})();
