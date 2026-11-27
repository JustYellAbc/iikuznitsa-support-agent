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
});
