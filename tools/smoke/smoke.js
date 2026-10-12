// Smoke test: load the page in jsdom against a running server and report what happened.
//   cd tools/smoke && npm install && node smoke.js http://localhost:8080/ [lat,lon,zoom]
// Exits non-zero on page errors or failing feeds (feeds without a key are reported, not failed).
const { JSDOM, VirtualConsole } = require("jsdom");

const base = process.argv[2] || "http://localhost:8080/";
const [lat, lon, zoom] = (process.argv[3] || "").split(",").map(Number);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(String(e.message || e).split("\n")[0]));
  const dom = await JSDOM.fromURL(base, {
    runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = (u, o) => fetch(new URL(u, base).href, o);
      w.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
      // jsdom has no canvas or layout: stub just enough for Leaflet's canvas renderer and bounds
      const ctx = new Proxy({}, { get: (t, k) => (k in t ? t[k] : () => ({ addColorStop() {} })), set: (t, k, v) => ((t[k] = v), true) });
      w.HTMLCanvasElement.prototype.getContext = () => ctx;
      Object.defineProperty(w.HTMLElement.prototype, "clientWidth", { get: () => 1200 });
      Object.defineProperty(w.HTMLElement.prototype, "clientHeight", { get: () => 800 });
    },
  });
  const w = dom.window;
  await wait(25000); // let the first round of feeds land
  if (!Number.isNaN(lat)) {
    w.eval(`map.invalidateSize(); map.setView([${lat}, ${lon}], ${zoom || 10}, { animate: false })`);
    await wait(10000);
  }
  const status = w.eval("JSON.stringify(status)");
  const layers = w.eval("JSON.stringify(Object.fromEntries(Object.entries(G).filter(([, g]) => map.hasLayer(g)).map(([k, g]) => [k, g.getLayers().length])))");
  let failed = 0;
  console.log("Feeds:");
  for (const [k, v] of Object.entries(JSON.parse(status))) {
    const noKey = /key|disabled/i.test(v.note || "");
    if (!v.ok && !noKey) failed++;
    console.log(`  ${v.ok ? "ok  " : noKey ? "skip" : "FAIL"} ${k}${v.note ? `  (${v.note})` : ""}`);
  }
  console.log("Layers on the map (markers):", layers);
  console.log("Page errors:", errors.length ? errors : "none");
  process.exit(errors.length || failed ? 1 : 0);
})();
