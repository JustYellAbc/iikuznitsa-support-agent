// Run from the repository with the backend dependencies, Playwright and Chromium installed.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const { mkdtemp, rm } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "../..");
const boot = `
import socket, sys, uvicorn
from pathlib import Path
sys.path.insert(0, str(Path("backend").resolve()))
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.main import app
@app.get("/manager")
def manager_without_redirect():
    return FileResponse("frontend/manager/index.html")
app.mount("/manager", StaticFiles(directory="frontend/manager", html=True))
app.mount("/chat", StaticFiles(directory="frontend/chat", html=True))
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
sock.listen()
print(sock.getsockname()[1], flush=True)
uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])
`;

test("Менеджер уточняет и закрывает обращение в исходном чате, повтор безопасен", { timeout: 60000 }, async t => {
  const dataDir = await mkdtemp(path.join(os.tmpdir(), "t13-test-"));
  const password = randomUUID();
  const server = spawn(process.env.PYTHON || "python", ["-u", "-c", boot], {
    cwd: root, env: { ...process.env, DATA_DIR: dataDir, MOCK_LLM: "true", OPERATOR_PASSWORD: password, OPERATOR_USER: "manager" },
    stdio: ["ignore", "pipe", "inherit"]
  });
  const stopped = once(server, "exit");
  let browser;
  t.after(async () => {
    // Close the buyer's event stream before asking uvicorn to drain its connections.
    await browser?.close();
    server.kill();
    await stopped;
    await rm(dataDir, { recursive: true, force: true });
  });
  const port = await Promise.race([
    once(server.stdout, "data").then(([line]) => Number(line.toString().trim())),
    stopped.then(() => { throw new Error("Локальный сервер завершился при запуске"); })
  ]);
  const baseURL = "http://127.0.0.1:" + port;
  browser = await chromium.launch({ channel: process.env.T13_BROWSER_CHANNEL || undefined });
  const customer = await browser.newContext({ baseURL });
  const manager = await browser.newContext({ baseURL });
  await manager.addInitScript(() => Object.defineProperty(crypto, "randomUUID", { value: undefined }));
  const page = await manager.newPage();
  const chat = await customer.newPage();
  const errorLog = [];
  page.on("pageerror", e => errorLog.push(e.message));
  chat.on("pageerror", e => errorLog.push(e.message));
  await chat.goto("/chat/");
  await chat.getByRole("button", { name: /Чат с поддержкой/ }).click();
  const message = chat.getByRole("textbox", { name: "Ваше сообщение" });
  const sendMessage = chat.getByRole("button", { name: "Отправить сообщение" });
  const chatHistory = chat.getByRole("log", { name: "История диалога" });
  await message.fill("Закупка тендерная, нужны документы");
  await sendMessage.click();
  await chatHistory.getByText("Обращение передано оператору", { exact: true }).waitFor();
  const recipient = chat.getByText("Сообщение оператору", { exact: true });
  await recipient.click();
  assert.equal(await chat.getByRole("radio", { name: "Оператор", exact: true }).isChecked(), true);
  await recipient.click();
  await page.goto("/manager");
  assert.equal(new URL(page.url()).pathname, "/manager");
  assert.equal(await page.locator('link[rel="stylesheet"]').evaluate(link => Boolean(link.sheet)), true);
  await page.getByLabel("Логин").fill("manager");
  await page.getByLabel("Пароль").fill(password);
  await page.getByRole("button", { name: "Войти" }).click();
  await page.getByRole("button", { name: /Тендерная закупка/ }).click();
  await page.locator("#history").getByText(/Передано менеджеру/).waitFor();
  const url = "**/manager/handoffs/*/resolution";
  const keys = [];
  let drop = true;
  await page.route(url, async route => {
    keys.push(route.request().headers()["idempotency-key"]);
    if (drop) {
      drop = false;
      const response = await route.fetch();
      assert.equal(response.status(), 202);
      await route.abort();
    } else await route.continue();
  });
  const answer = page.getByLabel("Ответ клиенту");
  const send = page.getByRole("button", { name: "Отправить", exact: true });
  await answer.fill("Сколько пользователей?");
  await send.click();
  await page.getByText(/Доставка не подтверждена/).waitFor();
  await send.click();
  await page.getByText("Ответ отправлен.", { exact: true }).waitFor();
  assert.ok(keys[0]);
  assert.equal(keys[1], keys[0]);
  const clarification = chatHistory.getByText("Сколько пользователей?", { exact: true });
  await clarification.waitFor();
  await message.fill("Десять пользователей");
  await sendMessage.click();
  await page.locator("#history").getByText("Десять пользователей").waitFor();
  const history = page.locator("#history");
  for (const width of [390, 1280]) {
    await page.setViewportSize({ width, height: 844 });
    const logo = page.getByRole("img", { name: "Софтлайн Решения" });
    assert.equal(await logo.getAttribute("src"), "/chat/softline.png");
    assert.ok(await logo.evaluate(img => img.complete && img.naturalWidth > 0));
    for (const [text, role] of [
      ["Десять пользователей", "Покупатель"],
      ["Сколько пользователей?", "Оператор"],
      ["Передано менеджеру", "Система"],
    ]) {
      const row = history.getByRole("listitem").filter({ hasText: text });
      await row.getByText(role, { exact: true }).waitFor();
    }
    await history.getByText("Ассистент", { exact: true }).first().waitFor();
    assert.ok(await send.isDisabled());
    for (const action of await page.locator("#compose button").all()) {
      const bounds = await action.boundingBox();
      assert.ok(bounds && bounds.x >= 0 && bounds.x + bounds.width <= width && bounds.height >= 44);
    }
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  }
  await answer.fill("Подбор для десяти пользователей согласован.");
  await page.getByRole("button", { name: "Ответить и закрыть" }).click();
  await page.getByText("Открытых обращений пока нет.").waitFor();
  await chatHistory.getByText("Подбор для десяти пользователей согласован.", { exact: true }).waitFor();
  await chatHistory.getByText("Обращение закрыто", { exact: true }).waitFor();
  assert.equal(await clarification.count(), 1);
  const question = "Где настроить уведомления в Softline Store?";
  const publicAnswer = "Выберите уведомления в настройках профиля.";
  await page.getByLabel("Общий вопрос").fill(question);
  await page.getByLabel("Ответ для всех покупателей").fill(publicAnswer);
  await page.getByRole("button", { name: "Сохранить черновик" }).click();
  await page.getByRole("button", { name: "Подтвердить публикацию" }).click();
  const published = page.getByRole("link", { name: "Открыть статью" });
  await published.waitFor();
  const articlePath = await published.getAttribute("href");
  await page.reload();
  await page.locator("#archive summary").click();
  await page.locator("#resolved").getByRole("button").click();
  await published.waitFor();
  const second = await browser.newContext({ baseURL });
  const otherChat = await second.newPage();
  await otherChat.goto("/chat/");
  await otherChat.getByRole("button", { name: /Чат с поддержкой/ }).click();
  await otherChat.getByRole("textbox", { name: "Ваше сообщение" }).fill("Как в Softline Store изменить настройки уведомлений?");
  await otherChat.getByRole("button", { name: "Отправить сообщение" }).click();
  await otherChat.getByRole("log").getByText(publicAnswer, { exact: true }).waitFor();
  const source = otherChat.getByRole("link", { name: /База знаний поддержки/ });
  assert.equal(new URL(await source.getAttribute("href")).pathname, articlePath);
  const article = await second.request.get(articlePath);
  assert.equal(await article.text(), question + "\n\n" + publicAnswer);
  assert.deepEqual(errorLog, []);
  await t.test("Две строки одного КП видны в одной закупке, решение уходит на выбранный SKU", async () => {
    const desk = await page.context().newPage();
    try {
      const line = (sku, quoteId, cid="buyer-one") => ({
        handoffId:"h-"+sku, conversationId:cid, status:"open", reason:"price_request",
        question:"Согласовать "+sku, quoteLine:{quoteId,revision:1,offerId:sku},
        products:[{name:"Товар "+sku,offerId:sku,source:{url:"https://example.org/"+sku,label:sku}}]
      });
      let pending = [line("A","q1"),line("B","q1"),line("C","q2","other-buyer")];
      await desk.route("**/manager/handoffs", route => route.fulfill({json:{items:pending}}));
      await desk.route("**/conversations/*/events?*", route => route.fulfill({json:{events:[], nextAfter:0,hasMore:false}}));
      let approved;
      let releaseApproval;
      const slowApproval = new Promise(resolve => { releaseApproval = resolve; });
      await desk.route("**/handoffs/h-B/resolution", route => {
        approved = route.request().postDataJSON();
        pending = [line("A","q1"),pending[2]];
        pending[0].quoteLine.revision=2;
        return slowApproval.then(() => route.fulfill({status:202,json:{}}));
      });
      await desk.goto(baseURL+"/manager");
      const queue = desk.locator("#queue button");
      await queue.nth(1).waitFor();
      assert.equal(await queue.count(),2);
      await queue.filter({hasText:"Товар A"}).click();
      const lines = desk.locator("#purchase-lines button");
      assert.equal(await lines.count(),2);
      await lines.filter({hasText:"Товар B"}).click();
      await desk.locator("#line-price").fill("123,45");
      await desk.getByRole("button",{name:"Подтвердить цену строки"}).click();
      assert.equal(await lines.first().isDisabled(),true);
      assert.equal(await desk.locator("#line-price").isDisabled(),true);
      releaseApproval();
      await desk.locator("#question").getByText("Согласовать A").waitFor();
      assert.deepEqual(approved,{type:"approve_line_price",quoteId:"q1",revision:1,offerId:"B",unitPriceKopecks:12345});
      assert.equal(await lines.count(),1);
    } finally { await desk.close(); }
  });
});
