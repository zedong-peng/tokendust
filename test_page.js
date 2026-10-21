// Node harness for the board logic. Run: node test_page.js
// Loads the script extracted from index.html into a stub DOM and checks the
// accounting and the card, which is where a silent wrong number would live.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const here = __dirname;
// The template, not the built page: the built one also carries the vendored QR
// encoder, and the largest block there would be the encoder rather than the logic.
const prices = fs.readFileSync(path.join(here, "prices.json"), "utf8");
const body = fs.readFileSync(path.join(here, "page.html"), "utf8")
  .match(/<script>\n([\s\S]*?)\n<\/script>/g)
  .map((block) => block.slice(9, -10))
  .sort((a, b) => b.length - a.length)[0]
  .replace("__PRICES__", prices)
  .replace("\nrefresh();", "\n");

function stubNode(tag) {
  const node = {
    tagName: tag, children: [], style: {}, dataset: {}, hidden: false,
    textContent: "", innerHTML: "", className: "", width: 0, height: 0,
    classList: { add() {}, remove() {}, toggle() {} },
    append(...kids) { this.children.push(...kids); },
    appendChild(kid) { this.children.push(kid); return kid; },
    replaceChildren(...kids) { this.children = kids; },
    addEventListener() {},
    setAttribute() {},
    getContext() { return stubCtx(); },
    toBlob(cb) { cb(null); },
    getBoundingClientRect() { return { left: 0, top: 0, width: 960, height: 260 }; },
    querySelectorAll() { return []; },
    closest() { return null; },
    clientWidth: 960,
  };
  return node;
}

const calls = [];
function stubCtx() {
  const noop = (name) => (...args) => calls.push([name, args]);
  return new Proxy({
    canvas: { width: 0, height: 0 },
    measureText: (t) => ({ width: String(t).length * 10 }),
  }, {
    get(target, key) {
      if (key in target) return target[key];
      if (typeof key !== "string") return undefined;
      return noop(String(key));
    },
    set(target, key, value) { target[key] = value; return true; },
  });
}

const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, stubNode(id));
    return elements.get(id);
  },
  createElement: (tag) => stubNode(tag),
  querySelectorAll: () => [],
  addEventListener() {},
};
const window = {
  addEventListener() {},
  devicePixelRatio: 2,
  document,
};
const sandbox = {
  window, document, console, Intl, JSON, Math, Date, Map, Set, Array, Object,
  Number, String, Error, URL, setTimeout, clearTimeout, alert() {},
  navigator: { clipboard: { write: async () => {} } },
  qrcode: () => ({ addData() {}, make() {}, getModuleCount: () => 21, isDark: () => true }),
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
// Top-level const/function declarations in a vm context are not global properties,
// so the script hands its internals out explicitly for the harness.
vm.runInContext(
  body + "\n;globalThis.__page = { parseSnapshot, mergeHosts, withCost, summarize, slots, " +
    "yScale, drawChart, drawCard, columns, legendEntries, BRAND, PRICES };\n",
  sandbox,
  { filename: "page.js" },
);
const page = sandbox.__page;
for (const [name, fn] of Object.entries(page)) sandbox[name] = fn;

let failures = 0;
function check(name, run) {
  try {
    run();
    console.log("  ok   " + name);
  } catch (error) {
    failures += 1;
    console.log("  FAIL " + name + ": " + error.message);
  }
}
function eq(actual, expected, what) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${what || "value"}: ${a} !== ${e}`);
}

const row = (date, tool, model, input, read, write, output, cost = null) =>
  ({ date, tool, model, requests: 0, input, cache_read: read, cache_write: write, output, recorded_cost: cost });

console.log("board logic");

check("same machine twice keeps the larger count, never sums it", () => {
  const files = [
    { host: "mac", rows: [row("2026-10-01", "codex", "gpt-5", 100, 0, 0, 10)] },
    { host: "mac", rows: [row("2026-10-01", "codex", "gpt-5", 200, 0, 0, 20)] },
  ];
  const merged = sandbox.mergeHosts(files);
  eq(merged.rows.length, 1, "row count");
  eq(merged.rows[0].input, 200, "tokens");
});

check("different machines sum", () => {
  const files = [
    { host: "mac", rows: [row("2026-10-01", "codex", "gpt-5", 100, 0, 0, 10)] },
    { host: "lab", rows: [row("2026-10-01", "codex", "gpt-5", 300, 0, 0, 30)] },
  ];
  const merged = sandbox.mergeHosts(files);
  eq(merged.hosts, ["mac", "lab"], "hosts");
  eq(merged.rows[0].input, 400, "tokens");
  eq(merged.rows[0].hosts.length, 2, "row hosts");
});

check("recorded cost is dropped when one machine cannot report it", () => {
  const files = [
    { host: "mac", rows: [row("2026-10-01", "cursor", "cursor-auto", 1, 0, 0, 1, 0.5)] },
    { host: "lab", rows: [row("2026-10-01", "cursor", "cursor-auto", 1, 0, 0, 1, null)] },
  ];
  eq(sandbox.mergeHosts(files).rows[0].recorded_cost, null, "cost");
});

check("list price follows the price table and its aliases", () => {
  const rows = sandbox.withCost([
    row("2026-10-01", "claude-code", "claude-opus-5", 1e6, 0, 0, 0),
    row("2026-10-02", "claude-code", "claude-opus-5-thinking", 1e6, 0, 0, 0),
    row("2026-10-03", "codex", "gpt-5.6-sol", 0, 1e6, 0, 0),
    row("2026-10-04", "codex", "some-new-model", 1e6, 0, 0, 0),
  ], null);
  eq(rows[0].api_usd, 5, "opus input");
  eq(rows[1].api_usd, 5, "alias input");
  eq(rows[2].api_usd, 0.4, "cache read");
  eq(rows[3].api_usd, null, "unknown model is unpriced");
});

check("ccusage daily --by-agent output is read", () => {
  const payload = {
    daily: [{
      period: "2026-10-01",
      agents: [{ agent: "claude", modelBreakdowns: [
        { modelName: "[beta] claude-opus-5", inputTokens: 10, cacheReadTokens: 20, cacheCreationTokens: 30, outputTokens: 40, cost: 1.5 },
      ] }],
    }],
  };
  const parsed = sandbox.parseSnapshot(JSON.stringify(payload), "ccusage");
  eq(parsed.rows[0].model, "claude-opus-5", "model");
  eq(parsed.rows[0].tool, "claude-code", "tool");
  eq([parsed.rows[0].input, parsed.rows[0].cache_read, parsed.rows[0].cache_write, parsed.rows[0].output], [10, 20, 30, 40], "tokens");
  eq(parsed.rows[0].recorded_cost, 1.5, "cost");
});

check("the published snapshot schema is read", () => {
  const snapshot = {
    schema_version: 1, updated_at: "2026-10-02T09:00:00+08:00",
    columns: ["date", "tool", "model", "requests", "input", "cache_read", "cache_write", "output", "api_usd"],
    rows: [["2026-10-01", "codex", "gpt-5", 0, 10, 20, 0, 30, 0.5]],
  };
  const parsed = sandbox.parseSnapshot(JSON.stringify(snapshot), "blog");
  eq(parsed.rows[0].recorded_cost, 0.5, "cost mapped");
  eq(parsed.rows[0].cache_read, 20, "cache read");
});

check("summarize totals every column and prices with the table", () => {
  const rows = sandbox.withCost([
    row("2026-10-01", "codex", "gpt-5.6-sol", 1e6, 0, 0, 0),
    row("2026-10-02", "claude-code", "claude-opus-5", 0, 0, 0, 1e6),
  ], null);
  const view = sandbox.summarize(rows, 0, sandbox.slots(rows));
  eq(view.total.tokens, 2e6, "tokens");
  eq(view.total.usd, 29, "usd"); // 5.6-sol input 1M = 4, opus-5 output 1M = 25
  eq(view.start, "2026-10-01", "start");
  eq(view.end, "2026-10-02", "end");
});

check("a year of daily data becomes weekly buckets", () => {
  const rows = [];
  for (let i = 0; i < 200; i += 1) {
    const day = new Date(Date.UTC(2026, 0, 1) + i * 86400000).toISOString().slice(0, 10);
    rows.push(sandbox.withCost([row(day, "codex", "gpt-5", 1e6, 0, 0, 0)], null)[0]);
  }
  const view = sandbox.summarize(rows, 365, sandbox.slots(rows));
  eq(view.weekly, true, "weekly");
  eq(view.total.tokens, 200e6, "tokens");
  const expected = 53; // 365 days from a Sunday is 53 week labels, not 365
  if (view.buckets.length !== expected) throw new Error("expected " + expected + " weekly buckets, got " + view.buckets.length);
});

check("one wild day does not flatten the rest (log scale after the threshold)", () => {
  const values = [1e6, 2e6, 3e6, 600e9];
  const scale = sandbox.yScale(values, 1);
  if (!scale.threshold) throw new Error("expected a threshold");
  const small = scale.at(1e6);
  const big = scale.at(600e9);
  // 1M stays pinned near the baseline; 600B takes the top of the log band.
  if (!(small < 0.01 && big > 0.97)) throw new Error(`small=${small} big=${big}`);
  if (!(scale.at(3e6) > small)) throw new Error("scale is not monotonic");
});

check("chart draws one segment per model and one axis line", () => {
  calls.length = 0;
  const rows = sandbox.withCost([
    row("2026-10-01", "codex", "gpt-5.6-sol", 5e8, 0, 0, 0),
    row("2026-10-02", "claude-code", "claude-opus-5", 3e8, 0, 0, 0),
  ], null);
  const view = sandbox.summarize(rows, 0, sandbox.slots(rows));
  sandbox.drawChart(stubCtx(), { x: 0, y: 0, width: 900, height: 260 }, view, {});
  const fills = calls.filter(([name, args]) => name === "fill" || (name === "fillRect"));
  if (fills.length < 2) throw new Error("expected segments, got " + fills.length);
  if (!calls.some(([name]) => name === "fillText")) throw new Error("no tick labels");
});

check("the share card always prints the brand and the link", () => {
  calls.length = 0;
  const rows = sandbox.withCost([row("2026-10-01", "codex", "gpt-5.6-sol", 1e9, 0, 0, 0)], null);
  const view = sandbox.summarize(rows, 0, sandbox.slots(rows));
  const canvas = stubNode("canvas");
  sandbox.drawCard(canvas, "wide", view, { hostCount: 2 });
  const texts = calls.filter(([name]) => name === "fillText").map(([, args]) => String(args[0]));
  const joined = texts.join(" | ");
  if (!joined.includes("tokendust")) throw new Error("no brand name");
  if (!joined.includes("zedongpeng.com/tokendust")) throw new Error("no link");
  if (!joined.includes("made with")) throw new Error("no made-with line");
  if (!texts.some((t) => /tokens\b/.test(t))) throw new Error("no token line");
  const qr = calls.filter(([name]) => name === "fillRect").length;
  if (qr < 20) throw new Error("QR did not draw");
});

check("the card's tall format is 1080x1350 at 2x", () => {
  const rows = sandbox.withCost([row("2026-10-01", "codex", "gpt-5.6-sol", 1e9, 0, 0, 0)], null);
  const view = sandbox.summarize(rows, 0, sandbox.slots(rows));
  const canvas = stubNode("canvas");
  sandbox.drawCard(canvas, "tall", view, { hostCount: 1 });
  eq([canvas.width, canvas.height], [2160, 2700], "canvas size");
});

console.log(failures ? `\n${failures} failing` : "\nall passing");
process.exit(failures ? 1 : 0);
