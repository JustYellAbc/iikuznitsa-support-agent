// T33. Run from the repository with the backend dependencies, Playwright and Chromium installed.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const { mkdtemp, rm } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "../..");
const boot = `
import socket, sys, uvicorn
from pathlib import Path
sys.path.insert(0, str(Path("backend").resolve()))
from fastapi.staticfiles import StaticFiles
from app.main import app
app.mount("/chat", StaticFiles(directory="frontend/chat", html=True))
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1], flush=True)
uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])
`;
const limit = 500;
// Exactly n Unicode code points. An emoji is two UTF-16 units, so text.length would count it twice.
const samples = {
  "латиница": n => "a".repeat(n),
  "кириллица": n => "ж".repeat(n),
  "эмодзи": n => "😀".repeat(n),
  "переносы строк": n => "👍\n".repeat((n - 1) >> 1) + "ё".repeat(n - 2 * ((n - 1) >> 1)),
};

test("Чат считает 500 символов как сервер и не теряет длинный текст", { timeout: 60000 }, async t => {
  const dataDir = await mkdtemp(path.join(os.tmpdir(), "t33-test-"));
  const server = spawn(process.env.PYTHON || "python", ["-u", "-c", boot], {
    cwd: root, env: { ...process.env, DATA_DIR: dataDir, MOCK_LLM: "true", OPERATOR_PASSWORD: "" },
    stdio: ["ignore", "pipe", "inherit"]
  });
  const stopped = once(server, "exit");
  let browser;
  t.after(async () => {
    await browser?.close();
    server.kill();
    await stopped;
    await rm(dataDir, { recursive: true, force: true });
  });
  const port = await Promise.race([
    once(server.stdout, "data").then(([line]) => Number(line.toString().trim())),
    stopped.then(() => { throw new Error("Локальный сервер завершился при запуске"); })
  ]);
  browser = await chromium.launch({ channel: process.env.T09_BROWSER_CHANNEL || undefined });
  const page = await browser.newPage({ baseURL: "http://127.0.0.1:" + port });
  const errorLog = [];
  page.on("pageerror", e => errorLog.push(e.message));
  const sent = [];
  page.on("request", r => { if (r.method() === "POST" && new URL(r.url()).pathname.endsWith("/actions")) sent.push(r.postDataJSON()); });
  await page.goto("/chat/");
  const created = page.waitForResponse(r => new URL(r.url()).pathname === "/api/v1/conversations" && r.status() === 201);
  await page.getByRole("button", { name: /Чат с поддержкой/ }).click();
  await created;
  const message = page.getByRole("textbox", { name: "Ваше сообщение" });
  const send = page.getByRole("button", { name: "Отправить сообщение" });
  const counter = page.locator("#counter");
  const feedback = page.locator("#feedback");
  assert.equal(await counter.isVisible(), false);

  for (const [name, make] of Object.entries(samples)) {
    await t.test(name, async () => {
      const over = make(limit + 1);
      await message.fill(over);
      assert.equal(await counter.isVisible(), true);
      assert.equal(await counter.innerText(), "501 / 500");
      assert.equal(await send.isDisabled(), true);
      assert.equal(await message.getAttribute("aria-invalid"), "true");
      assert.match(await feedback.innerText(), /длиннее 500 символов \(сейчас 501\)\. Сократите/);
      await message.press("Enter");
      assert.equal(await message.inputValue(), over);
      await message.fill(make(limit));
      assert.equal(await counter.innerText(), "500 / 500");
      assert.equal(await send.isDisabled(), false);
      assert.equal(await feedback.innerText(), "");
    });
  }
  assert.deepEqual(sent, []);

  const id = await page.evaluate(() => JSON.parse(sessionStorage.getItem("softline.t09.conversation.v1")).id);
  const journal = () => page.evaluate(async id => (await (await fetch("/api/v1/conversations/" + id + "/events?view=buyer")).json()).events, id);
  assert.deepEqual(await journal(), []);

  await t.test("Отказ сервера сохраняет текст и показывает подсказку", async () => {
    const hint = "Сообщение длиннее 500 символов (сейчас 501). Сократите его и отправьте снова.";
    await page.route(url => url.pathname.endsWith("/actions"), route => route.fulfill({ status: 422, json: { code: "invalid_input", message: hint } }), { times: 1 });
    const text = samples["эмодзи"](limit);
    await message.fill(text);
    await send.click();
    await page.getByText(hint, { exact: true }).waitFor();
    assert.equal(await message.inputValue(), text);
  });

  await t.test("Прямой запрос на 501 символ не создаёт событие и не занимает ключ", async () => {
    const post = (text, key) => page.evaluate(async ({ id, text, key }) => {
      const res = await fetch("/api/v1/conversations/" + id + "/actions?view=buyer", {
        method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key },
        body: JSON.stringify({ type: "message", text })
      });
      return { status: res.status, body: await res.json() };
    }, { id, text, key });
    for (const make of Object.values(samples)) {
      const rejected = await post(make(limit + 1), "t33-direct");
      assert.equal(rejected.status, 422);
      assert.deepEqual(rejected.body, {
        code: "invalid_input", message: "Сообщение длиннее 500 символов (сейчас 501). Сократите его и отправьте снова."
      });
    }
    assert.deepEqual(await journal(), []);
    const accepted = await post(samples["кириллица"](limit), "t33-direct");
    assert.equal(accepted.status, 202);
    assert.equal(accepted.body.conversationId, id);
  });

  await t.test("Сообщение ровно из 500 символов уходит и попадает в историю", async () => {
    await page.locator("#log .entry.agent").first().waitFor(); // the accepted direct message has been answered
    const text = samples["переносы строк"](limit);
    sent.length = 0;
    await message.fill(text);
    await send.click();
    await page.waitForFunction(() => document.querySelector("#message").value === "");
    assert.equal(sent.length, 1);
    assert.equal(Array.from(sent[0].text).length, limit);
    const inputs = (await journal()).filter(e => e.data.author === "customer").map(e => e.data.text);
    assert.deepEqual(inputs.map(text => Array.from(text).length), [limit, limit]);
    assert.equal(await counter.innerText(), "0 / 500");
  });
  assert.deepEqual(errorLog, []);
});
