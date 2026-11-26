const { after, before, test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright");
const { spawn } = require("node:child_process");
const { createInterface } = require("node:readline");
const { once } = require("node:events");
const cases = require("../../contracts/t04/examples.json");

const script = cases.find(item => item.id === "c-tender").steps;
const history = new Map();
for (const step of script.filter(s => s.role === "customer" && s.operationId === "readEvents" && s.status === 200)) {
  for (const event of step.response.events) history.set(event.seq, event);
}
const events = [...history.values()].sort((a, b) => a.seq - b.seq);
const api = "/api/v1/conversations";
let browser;
before(async () => { browser = await chromium.launch({ channel: process.env.T09_BROWSER_CHANNEL }); });
after(async () => { await browser?.close(); });

test("Вызов человека в интерфейсе Т09", async t => {
  const preview = spawn(process.env.PYTHON || "python", [path.join(__dirname, "../preview.py"), "--port", "0"],
    { windowsHide: true, stdio: ["ignore", "pipe", "inherit"] });
  const lines = createInterface({ input: preview.stdout });
  t.after(() => { lines.close(); preview.kill(); });
  const [url] = await Promise.race([
    once(lines, "line"),
    once(preview, "exit").then(() => { throw new Error("Предпросмотр завершился до запуска"); }),
  ]);
  for (const [name, turns] of [
    ["Повтор после ответа", [
      ["Позовите оператора.", "Подскажите, с каким вопросом нужна помощь?"],
      ["Как найти нужную лицензию?", "Попробуйте поиск по названию продукта в каталоге."],
      ["Это не помогло, соедините с человеком.", "Обращение передано оператору"],
    ]],
    ["Повтор сразу после уточнения", [
      ["Позовите оператора.", "Подскажите, с каким вопросом нужна помощь?"],
      ["Позовите оператора.", "Обращение передано оператору"],
    ]],
    ["Тендер сразу", [["Это тендер. Нужны документы.", "Обращение передано оператору"]]],
    ["Предложение помощи", [["Можно перенести эту лицензию на другой сервер?", "Позвать оператора"]]],
  ]) {
    await t.test(name, async t => {
      const page = await browser.newPage();
      t.after(() => page.close());
      await page.goto(url);
      await page.locator("#launch").click();
      await page.getByText("Какой продукт ищете или что хотите решить?").waitFor();
      const button = page.locator("#operator");
      assert.equal(await button.isVisible(), false);
      for (const [input, output] of turns) {
        await page.locator("#message").fill(input);
        await page.locator("#send").click();
        await page.getByText(output, { exact: true }).first().waitFor();
        assert.equal(await button.isVisible(), output === "Позвать оператора");
      }
      if (await button.isVisible()) {
        await page.reload();
        await page.locator("#launch").click();
        await button.click();
        await page.getByText("Обращение передано оператору", { exact: true }).waitFor();
        assert.equal(await button.isVisible(), false);
      }
    });
  }
});

for (const toOperator of [true, false]) {
  for (const eventFirst of [true, false]) {
    test(`${toOperator ? "Оператор" : "Агент"}: сначала ${eventFirst ? "событие" : "квитанция"}`, async t => {
      const page = await browser.newPage();
      t.after(() => page.close());
      const input = events.find(e => e.data.author === "customer" && Boolean(e.data.handoffId) === toOperator);
      let visible = input.seq - 1;
      let received;
      const actionSent = new Promise(resolve => { received = resolve; });
      let release;
      const receipt = new Promise(resolve => { release = resolve; });
      t.after(() => release());
      await page.route("http://127.0.0.1/**", async route => {
        const url = new URL(route.request().url());
        if (url.pathname === api) return route.fulfill({ status: 201, json: { conversationId: "c-tender" } });
        if (url.pathname.endsWith("/actions")) {
          received(route.request().postDataJSON());
          await receipt;
          return route.fulfill({ status: 202, json: { conversationId: "c-tender", actionId: input.actionId } });
        }
        if (url.pathname.endsWith("/events")) {
          const after = Number(url.searchParams.get("after"));
          const batch = events.filter(e => e.seq > after && e.seq <= visible);
          return route.fulfill({ json: { events: batch, nextAfter: batch.at(-1)?.seq ?? after, hasMore: false } });
        }
        const asset = url.pathname.split("/").pop() || "index.html";
        if (!["index.html", "chat.css", "chat.js", "render.js"].includes(asset)) return route.abort();
        return route.fulfill({ path: path.join(__dirname, "../chat", asset) });
      });
      await page.goto("http://127.0.0.1/chat/");
      await page.locator("#launch").click();
      if (toOperator) await page.getByText("Обращение передано оператору", { exact: true }).waitFor();
      await page.locator("#message").fill(input.data.text);
      await page.locator("#send").click();
      const body = await actionSent;
      assert.deepEqual(body, { type: "message", text: input.data.text, ...(toOperator ? { handoffId: input.data.handoffId } : {}) });

      const status = value => page.waitForFunction(value => document.querySelector("#feedback").textContent === value,
        value, { timeout: 5000 });
      if (!eventFirst) { release(); await status("Ожидаем ответ…"); }
      visible = input.seq;
      await page.locator("#log").getByText(input.data.text, { exact: true }).waitFor();
      if (eventFirst) release();
      if (toOperator) {
        await status("");
      } else {
        await status("Ожидаем ответ…");
        visible = input.seq + 1;
        await status("");
      }
    });
  }
}
