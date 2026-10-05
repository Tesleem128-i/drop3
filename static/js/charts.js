/* DROP charts — thin, theme-aware wrapper around Chart.js (loaded from cdnjs).
 * Usage: DROPCharts.line("canvasId", {...}) etc. Every helper shows a friendly
 * message instead of an empty chart when there is no data, and re-colours
 * itself when the user toggles light/dark. */
(function () {
  const registry = [];
  const PALETTE = ["#7A5CFF", "#3F86FF", "#16B58A", "#F5A623", "#E8559C", "#EF4444", "#22B8CF", "#8E9AAF"];

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const textColor = () => css("--muted") || "#5F6680";
  const gridColor = () => (document.documentElement.getAttribute("data-theme") === "dark" ? "rgba(255,255,255,0.08)" : "rgba(96,88,190,0.12)");

  function applyTheme(chart) {
    const o = chart.options;
    const c = textColor(), g = gridColor();
    if (o.plugins && o.plugins.legend && o.plugins.legend.labels) o.plugins.legend.labels.color = c;
    Object.keys(o.scales || {}).forEach((k) => {
      const sc = o.scales[k];
      if (sc.ticks) sc.ticks.color = c;
      if (sc.grid) sc.grid.color = g;
      if (sc.title) sc.title.color = c;
    });
    chart.update("none");
  }
  document.addEventListener("drop-theme-change", () => registry.forEach(applyTheme));

  function empty(canvas, msg) {
    const box = canvas.parentElement;
    box.innerHTML = '<div class="chart-empty">' + (msg || "No data yet — this fills in as students study and complete work.") + "</div>";
  }

  function make(id, config, hasData, emptyMsg) {
    const canvas = document.getElementById(id);
    if (!canvas) return null;
    if (!hasData) { empty(canvas, emptyMsg); return null; }
    if (typeof Chart === "undefined") { empty(canvas, "Charts failed to load."); return null; }
    config.options = config.options || {};
    config.options.responsive = true;
    config.options.maintainAspectRatio = false;
    const chart = new Chart(canvas, config);
    registry.push(chart);
    applyTheme(chart);
    return chart;
  }

  const baseScales = (yTitle, xTitle, yMax) => ({
    x: { ticks: { color: textColor(), maxRotation: 0, autoSkip: true }, grid: { display: false }, title: xTitle ? { display: true, text: xTitle, color: textColor() } : { display: false } },
    y: { beginAtZero: true, max: yMax, ticks: { color: textColor() }, grid: { color: gridColor() }, title: yTitle ? { display: true, text: yTitle, color: textColor() } : { display: false } },
  });

  const legend = (show) => ({ display: show, labels: { color: textColor(), usePointStyle: true, boxWidth: 8 } });

  window.DROPCharts = {
    palette: PALETTE,

    /* Study minutes (bars) vs score (line) over time, two y-axes. */
    timeVsScore(id, labels, minutes, scores, minutesLabel) {
      const has = labels.length > 0;
      return make(id, {
        data: {
          labels,
          datasets: [
            { type: "bar", label: minutesLabel || "Study minutes", data: minutes, backgroundColor: "rgba(63,134,255,0.35)", borderColor: "#3F86FF", borderWidth: 1, borderRadius: 8, yAxisID: "y" },
            { type: "line", label: "Average score %", data: scores, borderColor: "#7A5CFF", backgroundColor: "#7A5CFF", tension: 0.35, spanGaps: true, pointRadius: 4, borderWidth: 3, yAxisID: "y1" },
          ],
        },
        options: {
          interaction: { mode: "index", intersect: false },
          plugins: { legend: legend(true) },
          scales: Object.assign(baseScales("Minutes"), {
            y1: { position: "right", min: 0, max: 100, grid: { drawOnChartArea: false }, ticks: { color: textColor() }, title: { display: true, text: "Score %", color: textColor() } },
          }),
        },
      }, has, "Needs a few days of study time and graded work to draw this.");
    },

    /* Scatter with an optional trend line. points: [{x,y,label}], line: [{x,y},{x,y}] */
    scatter(id, points, line, xTitle, yTitle) {
      return make(id, {
        type: "scatter",
        data: {
          datasets: [
            { label: "Students", data: points, backgroundColor: "rgba(122,92,255,0.75)", borderColor: "#fff", borderWidth: 1.5, pointRadius: 7, pointHoverRadius: 9 },
            ...(line && line.length ? [{ type: "line", label: "Trend", data: line, borderColor: "#E8559C", borderDash: [6, 5], borderWidth: 2, pointRadius: 0, fill: false }] : []),
          ],
        },
        options: {
          plugins: {
            legend: legend(false),
            tooltip: { callbacks: { label: (c) => (c.raw.label ? c.raw.label + ": " : "") + c.raw.x + " min, " + c.raw.y + "%" } },
          },
          scales: {
            x: { beginAtZero: true, title: { display: true, text: xTitle, color: textColor() }, ticks: { color: textColor() }, grid: { color: gridColor() } },
            y: { min: 0, max: 100, title: { display: true, text: yTitle, color: textColor() }, ticks: { color: textColor() }, grid: { color: gridColor() } },
          },
        },
      }, points.length >= 2, "Needs at least two students with study time and scores.");
    },

    /* Simple vertical bars; colorByValue colours bars red/amber/green by % value. */
    bar(id, labels, values, yTitle, opts) {
      opts = opts || {};
      const colors = opts.colorByValue
        ? values.map((v) => (v >= 80 ? "rgba(22,181,138,0.75)" : v >= 60 ? "rgba(245,166,35,0.8)" : "rgba(239,68,68,0.75)"))
        : "rgba(122,92,255,0.7)";
      return make(id, {
        type: "bar",
        data: { labels, datasets: [{ label: yTitle || "", data: values, backgroundColor: colors, borderRadius: 8, borderSkipped: false }] },
        options: {
          indexAxis: opts.horizontal ? "y" : "x",
          plugins: { legend: legend(false) },
          scales: opts.horizontal
            ? { x: { min: 0, max: opts.max || 100, ticks: { color: textColor() }, grid: { color: gridColor() } }, y: { ticks: { color: textColor() }, grid: { display: false } } }
            : baseScales(yTitle, null, opts.max),
        },
      }, labels.length > 0, opts.emptyMsg);
    },

    /* Line over time for scores. */
    line(id, labels, values, yTitle, extra) {
      return make(id, {
        type: "line",
        data: { labels, datasets: [{ label: yTitle, data: values, borderColor: "#7A5CFF", backgroundColor: "rgba(122,92,255,0.15)", fill: true, tension: 0.35, pointRadius: 4, pointBackgroundColor: "#7A5CFF", borderWidth: 3 }] },
        options: { plugins: { legend: legend(false), tooltip: { callbacks: { afterLabel: (c) => (extra && extra[c.dataIndex]) || "" } } }, scales: baseScales(yTitle, null, 100) },
      }, labels.length > 0, "Shows up after the first graded quiz or assessment.");
    },

    doughnut(id, labels, values) {
      return make(id, {
        type: "doughnut",
        data: { labels, datasets: [{ data: values, backgroundColor: PALETTE, borderWidth: 2, borderColor: "rgba(255,255,255,0.6)" }] },
        options: { cutout: "62%", plugins: { legend: { position: "bottom", labels: { color: textColor(), usePointStyle: true, boxWidth: 8 } } } },
      }, values.length > 0 && values.some((v) => v > 0), "No mistakes recorded yet — great news, or just not enough data.");
    },

    /* Per-lesson: average minutes (bars) vs quiz success (line). */
    lessonTimeVsScore(id, labels, minutes, scores) {
      return make(id, {
        data: {
          labels,
          datasets: [
            { type: "bar", label: "Avg minutes", data: minutes, backgroundColor: "rgba(63,134,255,0.4)", borderRadius: 8, yAxisID: "y" },
            { type: "line", label: "Avg quiz score %", data: scores, borderColor: "#16B58A", backgroundColor: "#16B58A", tension: 0.3, spanGaps: true, pointRadius: 4, borderWidth: 3, yAxisID: "y1" },
          ],
        },
        options: {
          plugins: { legend: legend(true) },
          scales: Object.assign(baseScales("Minutes"), {
            y1: { position: "right", min: 0, max: 100, grid: { drawOnChartArea: false }, ticks: { color: textColor() } },
          }),
        },
      }, labels.length > 0, "Appears once students open lessons and take lesson quizzes.");
    },
  };
})();