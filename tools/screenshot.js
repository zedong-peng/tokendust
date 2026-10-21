// Drive headless Chrome over CDP: load index.html, inject a snapshot, screenshot.
// Usage: node tools/screenshot.js <snapshot.json> <out.png> [wide|tall] [http-root]
// Node 22+ (global WebSocket/fetch); no npm dependencies.

const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const CHROME = process.env.CHROME ||
  ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/usr/bin/google-chrome", "/usr/bin/chromium"].find((p) => fs.existsSync(p));
if (!CHROME) throw new Error("No Chrome found; set CHROME=/path/to/chrome");

const snapshotPath = process.argv[2];
const outPath = process.argv[3] || "card.png";
const format = process.argv[4] || "wide";
const root = process.argv[5] || ".";
const pageUrl = process.argv[6] || "file://" + path.resolve(root, "index.html");
const snapshot = JSON.parse(fs.readFileSync(snapshotPath, "utf8"));

const port = 9222 + Math.floor(Math.random() * 500);
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "tokendust-chrome-"));
const chrome = spawn(CHROME, [
  "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
  "--no-default-browser-check", "--allow-file-access-from-files",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1400,1600", "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function endpoint() {
  for (let i = 0; i < 60; i += 1) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      const page = list.find((t) => t.type === "page");
      if (page) return page.webSocketDebuggerUrl;
    } catch (error) { /* not up yet */ }
    await sleep(150);
  }
  throw new Error("Chrome did not expose a debugging endpoint");
}

async function main() {
  const ws = new WebSocket(await endpoint());
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  let id = 0;
  const pending = new Map();
  const events = [];
  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      message.error ? reject(new Error(message.error.message)) : resolve(message.result);
    } else if (message.method) {
      events.push(message);
    }
  });
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const messageId = ++id;
      pending.set(messageId, { resolve, reject });
      ws.send(JSON.stringify({ id: messageId, method, params }));
    });

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Log.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1400, height: 1600, deviceScaleFactor: 2, mobile: false });
  await send("Page.navigate", { url: pageUrl });
  await sleep(1200);

  const expression = `
    (() => {
      const raw = ${JSON.stringify(snapshot)};
      state.files = [{ host: raw.host, rows: raw.rows.map(normalizeRow), generatedAt: raw.generated_at }];
      state.prices = null;
      refresh();
      state.format = ${JSON.stringify(format)};
      renderCard();
      const box = document.getElementById("card").getBoundingClientRect();
      return JSON.stringify({ w: box.width, h: box.height,
        tokens: state.view.total.tokens, usd: state.view.total.usd,
        rows: state.rows.length, hosts: state.hosts });
    })()
  `;
  const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  const errors = events
    .filter((e) => e.method === "Runtime.exceptionThrown" || (e.method === "Log.entryAdded" && e.params.entry.level === "error"))
    .map((e) => (e.params.exceptionDetails ? e.params.exceptionDetails.text + " " + (e.params.exceptionDetails.exception || {}).description : e.params.entry.text));
  if (errors.length) console.log("page errors:\n  " + errors.join("\n  "));
  if (result.exceptionDetails) {
    const details = result.exceptionDetails;
    throw new Error("evaluate failed: " + (details.exception && details.exception.description || details.text));
  }
  console.log("state:", result.result.value);

  // Screenshot just the card, at its own size: pin it to the top-left so the
  // clip is the page origin and the page's responsive CSS cannot shrink it.
  const sizes = { wide: [1200, 630], tall: [1080, 1350] };
  const [cardW, cardH] = sizes[format] || sizes.wide;
  await send("Runtime.evaluate", {
    expression: `(() => {
      const canvas = document.getElementById("card");
      document.body.style.cssText = "margin:0;background:#fbfbf9;overflow:hidden";
      document.body.append(canvas);
      canvas.style.cssText = "position:fixed;left:0;top:0;width:${cardW}px;height:${cardH}px;display:block";
      for (const node of document.querySelectorAll(".wrap")) node.style.display = "none";
      return true;
    })()`,
    returnByValue: true,
  });
  await sleep(250);
  await send("Emulation.setDeviceMetricsOverride", {
    width: cardW, height: cardH, deviceScaleFactor: 1, mobile: false,
  });
  const shot = await send("Page.captureScreenshot", {
    format: "png", clip: { x: 0, y: 0, width: cardW, height: cardH, scale: 1 },
    captureBeyondViewport: true,
  });
  fs.writeFileSync(outPath, Buffer.from(shot.data, "base64"));
  console.log("wrote", outPath, cardW + "x" + cardH);
  ws.close();
}

main()
  .catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  })
  .finally(() => {
    chrome.kill();
    setTimeout(() => {
      try { fs.rmSync(profile, { recursive: true, force: true }); } catch (error) { /* Chrome still holds files */ }
    }, 800);
  });
