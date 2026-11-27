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

test("Виджет Т09 на примерах контракта", async t => {
  const preview = spawn(process.env.PYTHON || "python", [path.join(__dirname, "../preview.py"), "--port", "0"],
    { windowsHide: true, stdio: ["ignore", "pipe", "inherit"] });
  const lines = createInterface({ input: preview.stdout });
  t.after(() => { lines.close(); preview.kill(); });
  const [url] = await Promise.race([
    once(lines, "line"),
    once(preview, "exit").then(() => { throw new Error("Предпросмотр завершился до запуска"); }),
  ]);
  await t.test("На мобильном экране видны три состояния цены и источники", async t => {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    t.after(() => page.close());
    const steps = cases.find(item => item.id === "c-price").steps;
    const request = steps.find(step => step.operationId === "submitAction").body;
    await page.goto(url);
    await page.locator("#launch").click();
    await page.locator("#message").fill(request.text);
    await page.locator("#send").click();
    const cards = page.locator("#log .result");
    await cards.nth(2).waitFor();
    const card = id => cards.filter({ has: page.locator('a[href*="-' + id + '/"]') });
    assert.match((await card("61557").innerText()).replace(/\s/g, " "), /4 900 ₽ за единицу по карточке/);
    assert.match(await card("61557").innerText(), /Срок лицензии неизвестен/);
    assert.match(await card("48093").innerText(), /Наличие требует подтверждения/);
    assert.match(await card("504").innerText(), /Цена по запросу/);
    assert.doesNotMatch(await card("504").innerText(), /₽/);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  });
  await t.test("Подтверждение из чата открывает форму с обеими выбранными позициями", async t => {
    const page = await browser.newPage();
    t.after(() => page.close());
    const selection = [{offerId: "office", priceState: "on_request"}, {offerId: "av", priceState: "on_request"}];
    await page.route("**/quotes/current?*", route => route.fulfill({json: {
      revision: 0, requiresReselection: false, customer: null, items: [], selection,
      choices: [...selection, {offerId: "old"}].map(p => ({offerId: p.offerId, name: p.offerId}))
    }}));
    await page.route("**/actions?*", route => {
      const cid = new URL(route.request().url()).pathname.split("/")[4];
      return route.fulfill({status: 202, headers: {"X-Chat-Action": "open-quote-form"},
        json: {conversationId: cid, actionId: "accepted-quote"}});
    });
    await page.goto(url);
    await page.locator("#launch").click();
    await page.locator("#message").fill("Да");
    await page.locator("#send").click();
    const form = page.locator("#quote-dialog[open]");
    await form.getByText("av · ID av · Цена по запросу").waitFor();
    assert.deepEqual(await form.locator("input[type=checkbox]:checked").evaluateAll(nodes => nodes.map(n => n.value)),
      ["office", "av"]);
    await page.reload();
    await page.locator("#launch").click();
    assert.equal(await page.locator("#quote-dialog").isVisible(), false);
  });
  await t.test("После обрыва потока виджет дочитывает историю с последнего события", async t => {
    const page = await browser.newPage();
    t.after(() => page.close());
    const [first, second] = events;
    const cursors = [];
    await page.route("**/events?*", route => {
      const cursor = Number(new URL(route.request().url()).searchParams.get("after"));
      cursors.push(cursor);
      const batch = cursor === first.seq ? [second] : [];
      return route.fulfill({ json: { events: batch, nextAfter: batch.at(-1)?.seq ?? cursor, hasMore: false } });
    });
    await page.route("**/events/stream?*", route => {
      const frame = "event: event\ndata: " + JSON.stringify(first) + "\n\n";
      return route.fulfill({ contentType: "text/event-stream", body: frame + frame });
    });
    await page.goto(url);
    await page.locator("#launch").click();
    await page.getByText(second.data.text, { exact: true }).waitFor();
    assert.equal(await page.getByText(first.data.text, { exact: true }).count(), 1);
    assert.deepEqual(cursors.slice(0, 2), [0, first.seq]);
  });
  await t.test("Просроченный диалог можно заменить в виджете одним нажатием", async t => {
    const page = await browser.newPage();
    t.after(() => page.close());
    await page.addInitScript(() => sessionStorage.setItem("softline.t09.conversation.v1",
      JSON.stringify({ id: "old", pending: { body: {type: "message", text: "старый запрос"}, uncertain: true } })));
    await page.route("**/conversations/old/events?*", route =>
      route.fulfill({ status: 404, json: {code: "not_found", message: "Диалог недоступен"} }));
    const actions = [];
    page.on("request", req => { if (new URL(req.url()).pathname.endsWith("/actions")) actions.push(req.postDataJSON()); });
    await page.goto(url);
    await page.locator("#launch").click();
    const created = page.waitForResponse(res => new URL(res.url()).pathname === api && res.status() === 201);
    await page.getByRole("button", {name: "Начать новый диалог"}).click();
    assert.equal(new URL((await created).url()).searchParams.get("view"), "buyer");
    await page.locator("#message").fill("Позовите оператора.");
    await page.locator("#send").click();
    await page.getByText("Подскажите, с каким вопросом нужна помощь?", {exact: true}).waitFor();
    assert.deepEqual(actions, [{type: "message", text: "Позовите оператора."}]);
  });
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
        if (!["index.html", "chat.css", "chat.js", "render.js", "quote.js"].includes(asset)) return route.abort();
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
