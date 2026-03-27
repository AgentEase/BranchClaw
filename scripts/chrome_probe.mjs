#!/usr/bin/env node

import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { spawn } from "node:child_process";

function parseArgs(argv) {
  const args = {
    url: "",
    outDir: "",
    label: "probe",
    preset: "desktop",
    chromeBin: process.env.CHROME_BIN || "",
    port: 9333,
    waitMs: 3000,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (key === "--url") args.url = value;
    if (key === "--out-dir") args.outDir = value;
    if (key === "--label") args.label = value;
    if (key === "--preset") args.preset = value;
    if (key === "--chrome-bin") args.chromeBin = value;
    if (key === "--port") args.port = Number(value);
    if (key === "--wait-ms") args.waitMs = Number(value);
  }
  if (!args.url || !args.outDir) {
    throw new Error("chrome_probe.mjs requires --url and --out-dir");
  }
  return args;
}

function findChrome(explicitPath) {
  if (explicitPath) return explicitPath;
  return "/usr/bin/google-chrome";
}

function presetMetrics(preset) {
  if (preset === "mobile") {
    return { width: 390, height: 844, mobile: true, deviceScaleFactor: 2 };
  }
  return { width: 1440, height: 900, mobile: false, deviceScaleFactor: 1 };
}

async function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchJson(url, timeoutMs = 1000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) {
      throw new Error(`Request failed: ${response.status}`);
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

async function waitForDebugger(port, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      return await fetchJson(`http://127.0.0.1:${port}/json/version`, 1000);
    } catch (_error) {
      await delay(250);
    }
  }
  throw new Error("Timed out waiting for Chrome remote debugging endpoint");
}

class CDPClient {
  constructor(wsUrl) {
    this.ws = new WebSocket(wsUrl);
    this.nextId = 0;
    this.pending = new Map();
    this.events = [];
  }

  async connect() {
    await new Promise((resolve, reject) => {
      this.ws.addEventListener("open", resolve, { once: true });
      this.ws.addEventListener("error", reject, { once: true });
    });
    this.ws.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result || {});
        return;
      }
      this.events.push(message);
    });
  }

  send(method, params = {}, sessionId = "") {
    const id = ++this.nextId;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    this.ws.send(JSON.stringify(payload));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
  }

  async waitForEvent(method, sessionId, timeoutMs = 15000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const index = this.events.findIndex(
        (event) => event.method === method && event.sessionId === sessionId,
      );
      if (index >= 0) {
        return this.events.splice(index, 1)[0];
      }
      await delay(100);
    }
    throw new Error(`Timed out waiting for event ${method}`);
  }

  close() {
    this.ws.close();
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const metrics = presetMetrics(args.preset);
  const chromeBin = findChrome(args.chromeBin);
  const outDir = args.outDir;
  await fs.mkdir(outDir, { recursive: true });
  const logPath = path.join(outDir, `${args.label}.chrome.log`);
  const userDataDir = await fs.mkdtemp(path.join(os.tmpdir(), "branchclaw-chrome-"));
  const chrome = spawn(
    chromeBin,
    [
      "--headless=new",
      `--remote-debugging-port=${args.port}`,
      `--user-data-dir=${userDataDir}`,
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "about:blank",
    ],
    { stdio: ["ignore", "pipe", "pipe"] },
  );
  const logHandle = await fs.open(logPath, "w");
  chrome.stdout.on("data", (chunk) => {
    logHandle.write(chunk);
  });
  chrome.stderr.on("data", (chunk) => {
    logHandle.write(chunk);
  });

  let client;
  try {
    const version = await waitForDebugger(args.port);
    client = new CDPClient(version.webSocketDebuggerUrl);
    await client.connect();

    const target = await client.send("Target.createTarget", { url: "about:blank" });
    const attached = await client.send("Target.attachToTarget", {
      targetId: target.targetId,
      flatten: true,
    });
    const sessionId = attached.sessionId;

    await client.send("Page.enable", {}, sessionId);
    await client.send("Runtime.enable", {}, sessionId);
    await client.send("Log.enable", {}, sessionId);
    await client.send("Emulation.setDeviceMetricsOverride", metrics, sessionId);
    await client.send("Page.navigate", { url: args.url }, sessionId);
    await client.waitForEvent("Page.loadEventFired", sessionId, 30000);
    await delay(args.waitMs);

    const evalResult = await client.send(
      "Runtime.evaluate",
      {
        expression: `JSON.stringify({
          title: document.title,
          bodyTextSample: (document.body?.innerText || "").trim().slice(0, 800)
        })`,
        returnByValue: true,
      },
      sessionId,
    );
    const domInfo = JSON.parse(evalResult.result?.value || "{}");
    const screenshot = await client.send(
      "Page.captureScreenshot",
      { format: "png", captureBeyondViewport: true },
      sessionId,
    );
    const screenshotPath = path.join(outDir, `${args.label}.png`);
    await fs.writeFile(screenshotPath, Buffer.from(screenshot.data, "base64"));

    const consoleEvents = client.events.filter(
      (event) =>
        event.sessionId === sessionId &&
        (event.method === "Runtime.consoleAPICalled" ||
          event.method === "Log.entryAdded" ||
          event.method === "Runtime.exceptionThrown"),
    );
    const consolePath = path.join(outDir, `${args.label}.console.json`);
    await fs.writeFile(consolePath, JSON.stringify(consoleEvents, null, 2));

    const warningCount = consoleEvents.filter((event) => {
      if (event.method === "Runtime.consoleAPICalled") {
        return event.params?.type === "warning";
      }
      return event.params?.entry?.level === "warning";
    }).length;
    const errorCount = consoleEvents.filter((event) => {
      if (event.method === "Runtime.exceptionThrown") {
        return true;
      }
      if (event.method === "Runtime.consoleAPICalled") {
        return event.params?.type === "error";
      }
      return event.params?.entry?.level === "error";
    }).length;
    const payload = {
      ok: true,
      url: args.url,
      screenshotPath,
      consolePath,
      consoleCount: consoleEvents.length,
      warningCount,
      errorCount,
      title: domInfo.title || "",
      bodyTextSample: domInfo.bodyTextSample || "",
    };
    process.stdout.write(`${JSON.stringify(payload)}\n`);
  } finally {
    try {
      client?.close();
    } catch (_error) {
      // ignore close errors
    }
    chrome.kill("SIGTERM");
    await delay(200);
    if (chrome.exitCode === null) {
      chrome.kill("SIGKILL");
    }
    await logHandle.close();
    await fs.rm(userDataDir, { recursive: true, force: true });
  }
}

main().catch((error) => {
  process.stderr.write(`${String(error.stack || error)}\n`);
  process.exit(1);
});
