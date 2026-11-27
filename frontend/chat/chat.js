import { buyerUrl, renderEvent } from "./render.js";
import { quoteEvent, resetQuotes, setupQuotes } from "./quote.js";

const byId = id => document.getElementById(id);
const panel = byId("chat"), launch = byId("launch"), message = byId("message");
const log = byId("log"), retry = byId("retry"), operator = byId("operator"), target = byId("target");
const storageKey = "softline.t09.conversation.v1";
const base = "/api/v1/conversations";
const newKey = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
let state;
try { state = JSON.parse(sessionStorage.getItem(storageKey)) || {}; } catch { state = {}; }
function save() { sessionStorage.setItem(storageKey, JSON.stringify(state)); }
const openHandoffs = new Map();
const completedActions = new Set();
// T12's OFFER_HELP is delivered as a clarification in the current T04 wire format.
const operatorQuestion = "Подключить оператора, чтобы он уточнил ваш вопрос?";
const operatorOffer = "В источнике нет подтверждённых сведений. " + operatorQuestion;
let operatorOffered = false;
let cursor = 0, posting = false, polling = false, denied = false, waitUntil = 0;
let timer, wake, actionError = "", historyError = "";
let stream, streamUnavailable = false;
message.value = state.pending?.body.text || "";

async function request(path, key, body) {
  const headers = { Accept: "application/json" };
  if (key) headers["Idempotency-Key"] = key;
  if (body) headers["Content-Type"] = "application/json";
  const res = await fetch(buyerUrl(path), {
    method: key ? "POST" : "GET", credentials: "same-origin", cache: "no-store", headers,
    body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(15000)
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const error = new Error(data.message || "Сервис временно недоступен.");
    Object.assign(error, { status: res.status, code: data.code, retryAfter: Number(res.headers.get("Retry-After")) || 0 });
    throw error;
  }
  if (!res.headers.get("Content-Type")?.includes("application/json")) throw new Error("Стенд пока не предоставил API чата.");
  if (res.headers.get("X-Chat-Action") === "open-quote-form") data.openQuoteForm = true;
  return data;
}

function sync() {
  const locked = posting || denied || !state.id || Date.now() < waitUntil;
  byId("send").disabled = locked || !message.value.trim();
  operator.hidden = denied || !operatorOffered || openHandoffs.size > 0;
  operator.disabled = locked || operator.hidden || Boolean(state.receipt);
  // After an ambiguous network error only replay the exact original command.
  message.readOnly = posting || Boolean(state.pending?.uncertain);
  if (state.pending?.uncertain) {
    operator.disabled = true;
    byId("send").disabled = true;
  }
  retry.hidden = !(actionError || historyError);
  retry.textContent = denied ? "Начать новый диалог" : "Повторить";
  retry.disabled = posting || Date.now() < waitUntil || (denied && polling);
  byId("feedback").textContent = actionError || historyError || (posting ? "Отправляем…" : state.receipt ? "Ожидаем ответ…" : "");
}

function updateHandoffs(handoff) {
  const selected = target.value;
  const hadRecipient = openHandoffs.size > 0;
  if (handoff.status === "open") openHandoffs.set(handoff.handoffId, handoff);
  else openHandoffs.delete(handoff.handoffId);
  target.replaceChildren();
  for (const [id, item] of openHandoffs) target.add(new Option("Менеджеру — " + item.question, id));
  target.add(new Option("Агенту поддержки", ""));
  if (openHandoffs.has(selected) || (hadRecipient && !selected)) target.value = selected;
  byId("target-label").hidden = openHandoffs.size === 0;
  if ([...openHandoffs.values()].some(item => item.quoteLine)) {
    byId("quote-open").textContent = "Изменить количество в КП";
  } else if (handoff.quoteLine) {
    byId("quote-open").textContent = "Изменить КП";
  }
}

function showError(error, isAction) {
  let text = error.message || "Ошибка соединения. Повторите запрос.";
  if (!error.status && ["TypeError", "TimeoutError", "AbortError"].includes(error.name)) text = "Связь прервалась. Повторите запрос.";
  if (error.status === 401 || error.status === 403 || error.status === 404 && !isAction && state.id) {
    denied = true;
    closeStream();
    text = "Доступ к этому диалогу закончился. Вы можете начать новый.";
    state = {}; save();
    log.replaceChildren(); openHandoffs.clear(); resetQuotes(); target.replaceChildren(); byId("target-label").hidden = true;
    byId("welcome").hidden = false; message.value = "";
  }
  if (error.retryAfter > 0) {
    waitUntil = Date.now() + error.retryAfter * 1000;
    text += " Повтор через " + error.retryAfter + " с.";
    clearTimeout(wake); wake = setTimeout(sync, error.retryAfter * 1000);
  }
  if (isAction) actionError = text;
  else historyError = text;
  sync();
}

function display(event) {
  if (event.seq <= cursor) return;
  if (event.seq !== cursor + 1) throw new Error("История пришла не полностью. Повторяем загрузку.");
  const scroller = byId("scroller");
  const atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 72;
  if (event.data.type === "quote") {
    for (const old of log.querySelectorAll("[data-confirm-quote]")) old.disabled = true;
    for (const item of [...openHandoffs.values()]) {
      if (item.quoteLine?.quoteId === event.data.quote.quoteId && item.quoteLine.revision < event.data.quote.revision)
        updateHandoffs({...item,status:"resolved"});
    }
  }
  log.append(renderEvent(event, state.id, submit));
  quoteEvent(event.data);
  byId("welcome").hidden = true;
  cursor = event.seq;
  if (event.data.type === "handoff") updateHandoffs(event.data.handoff);
  const { type, author, purpose, handoffId } = event.data;
  if (type === "message") {
    operatorOffered = author === "agent" && purpose === "clarification" &&
      (event.data.text === operatorOffer || event.data.text.endsWith("\n\n" + operatorQuestion));
  } else if (type === "handoff" || type === "products") {
    operatorOffered = false;
  }
  // A reply to an operator finishes on delivery. The manager answers with a separate actionId.
  const deliveredToOperator = type === "message" && author === "customer" && purpose === "input" && Boolean(handoffId);
  if (deliveredToOperator || type !== "message" || (purpose !== "input" && purpose !== "greeting")) {
    completedActions.add(event.actionId);
    if (state.receipt === event.actionId) { delete state.receipt; save(); }
  }
  if (atBottom) scroller.scrollTop = scroller.scrollHeight;
}

function closeStream() {
  if (stream) stream.close();
  stream = null;
}

function subscribe() {
  if (stream || streamUnavailable || panel.hidden || denied || !window.EventSource) return;
  const source = new EventSource(buyerUrl(base + "/" + encodeURIComponent(state.id) + "/events/stream?after=" + cursor));
  stream = source;
  function fallBack() {
    if (stream !== source) return;
    closeStream();
    streamUnavailable = true;
    poll(); // Recover any missing sequence from the T04 journal.
  }
  source.addEventListener("event", frame => {
    if (stream !== source) return;
    try {
      display(JSON.parse(frame.data));
      sync();
    } catch { fallBack(); }
  });
  source.onerror = fallBack;
}

async function poll() {
  if (stream || polling || panel.hidden || !state.id || denied) return;
  clearTimeout(timer);
  polling = true;
  let delay;
  try {
    let page;
    do {
      page = await request(base + "/" + encodeURIComponent(state.id) + "/events?after=" + cursor);
      if (denied) return;
      const before = cursor;
      for (const event of page.events) display(event);
      if (page.nextAfter !== cursor || page.hasMore && cursor === before) throw new Error("Ошибка курсора истории.");
    } while (page.hasMore && !panel.hidden);
    historyError = "";
    subscribe();
  } catch (error) {
    closeStream();
    showError(error, false);
    delay = Math.max(3000, (error.retryAfter || 0) * 1000);
  } finally {
    polling = false; sync();
    if (!stream && !panel.hidden && !denied) {
      timer = setTimeout(poll, delay ?? (posting || state.receipt ? 1200 : 4000));
    }
  }
}

async function connect() {
  if (state.id) { poll(); return; }
  if (posting || denied) return;
  posting = true; actionError = ""; sync();
  try {
    state.createKey ||= newKey(); save();
    const result = await request(base, state.createKey);
    if (!result.conversationId) throw new Error("Стенд не вернул номер диалога.");
    state.id = result.conversationId; delete state.createKey; save();
    poll();
  } catch (error) { showError(error, true); }
  finally { posting = false; sync(); }
}

async function submit(body) {
  if (!state.id || posting || denied || Date.now() < waitUntil) return;
  if (state.pending?.uncertain && JSON.stringify(state.pending.body) !== JSON.stringify(body)) return;
  if (JSON.stringify(state.pending?.body) !== JSON.stringify(body)) state.pending = { key: newKey(), body };
  state.pending.uncertain = true; save();
  posting = true; actionError = ""; sync();
  try {
    const receipt = await request(base + "/" + encodeURIComponent(state.id) + "/actions", state.pending.key, body);
    if (denied) return;
    if (body.type === "request_document" && receipt.documentId && receipt.kind === body.kind && receipt.quoteId === body.quoteId &&
        receipt.revision === body.revision && /^\/api\/v1\/documents\/[\w.-]+$/.test(receipt.url)) {
      state.receipt = null;
      const link = document.createElement("a"); link.href = buyerUrl(receipt.url); link.download = ""; link.click();
    } else {
      if (!receipt.actionId || receipt.conversationId !== state.id) throw new Error("Не удалось подтвердить отправку.");
      state.receipt = completedActions.has(receipt.actionId) ? null : receipt.actionId;
    }
    delete state.pending; save();
    if (body.type === "message") message.value = "";
    if (receipt.openQuoteForm && !panel.hidden) openChatQuote();
    poll();
  } catch (error) {
    if (error.status && error.status < 500 && state.pending) {
      state.pending.uncertain = false;
      if (error.code === "key_conflict" || error.code === "already_resolved") delete state.pending;
      save();
    }
    showError(error, true);
  } finally { posting = false; sync(); }
}

function sendText() {
  const text = message.value.trim();
  if (!text || byId("send").disabled) return;
  const body = { type: "message", text };
  if (target.value) body.handoffId = target.value;
  submit(body);
}

launch.onclick = () => {
  streamUnavailable = false;
  panel.hidden = false; launch.hidden = true; launch.setAttribute("aria-expanded", "true");
  message.focus(); sync(); connect();
};
function close() {
  panel.hidden = true; launch.hidden = false; launch.setAttribute("aria-expanded", "false");
  closeStream();
  clearTimeout(timer); launch.focus();
}
byId("close").onclick = close;
panel.onkeydown = event => { if (event.key === "Escape") close(); };
byId("compose").onsubmit = event => { event.preventDefault(); sendText(); };
message.oninput = () => {
  if (state.pending?.body.type === "message" && !state.pending.uncertain && message.value.trim() !== state.pending.body.text) {
    delete state.pending;
    actionError = ""; save();
  }
  sync();
};
message.onkeydown = event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendText(); }
};
operator.onclick = () => submit({ type: "request_operator" });
retry.onclick = () => {
  if (denied) {
    denied = false; cursor = 0; operatorOffered = false; streamUnavailable = false;
    completedActions.clear(); actionError = ""; historyError = "";
  }
  if (!state.id) connect();
  else if (state.pending) submit(state.pending.body);
  else poll();
};
if (state.pending) actionError = "Отправка не подтверждена. Можно повторить.";
const openChatQuote = setupQuotes(() => state.id, request, newKey);
