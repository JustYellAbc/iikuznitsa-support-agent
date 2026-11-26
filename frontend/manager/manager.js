"use strict";

const $ = id => document.getElementById(id);
const reasons = {
  customer_request: "Запрос оператора", tender: "Тендерная закупка", price_request: "Запрос цены",
  availability: "Проверка доступности", outside_catalog: "Вне каталога", special_terms: "Особые условия",
  technical: "Технический вопрос", insufficient_evidence: "Недостаточно сведений"
};
const authors = { customer: "Клиент", agent: "ИИ-агент", manager: "Менеджер" };
const currency = new Intl.NumberFormat("ru-RU", { style: "currency", currency: "RUB" });
const clock = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" });
const drafts = new Map();
let items = [], selected = null, cursor = 0, generation = 0;
let authenticated = false, timer, refreshNumber = 0, historyNumber = 0, retryAt = 0;

function element(tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

async function request(path, body, key) {
  const options = { credentials: "same-origin", cache: "no-store" };
  if (body) Object.assign(options, {
    method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key },
    body: JSON.stringify(body)
  });
  const response = await fetch("/api/v1" + path, options);
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const message = data?.message || ("Ошибка сервера (" + response.status + ").");
    if (response.status === 429) retryAt = Date.now() + (Number(response.headers.get("Retry-After")) || 5) * 1000;
    throw Object.assign(new Error(message), { status: response.status, code: data?.code });
  }
  return data;
}

function sourceLink(source) {
  if (!source?.url?.startsWith("https://")) return element("span", source?.label || "");
  const link = element("a", source.label);
  link.href = source.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function productList(products) {
  const list = document.createElement("ul");
  for (const p of products) {
    const item = element("li", p.name + " · " + (p.unitPriceKopecks == null ? "Цена по запросу" : currency.format(p.unitPriceKopecks / 100)));
    if (p.priceState === "availability_check") item.append(" · доступность требует проверки");
    item.append(" · ", sourceLink(p.source));
    list.append(item);
  }
  return list;
}

function eventRow(event) {
  const d = event.data, row = document.createElement("li");
  const stamp = element("span", (authors[d.author] || "Система") + " · " + clock.format(new Date(event.at)));
  stamp.className = "stamp";
  row.append(stamp);
  switch (d.type) {
    case "message":
      row.append(element("p", d.text));
      for (const source of d.sources) row.append(sourceLink(source), " ");
      break;
    case "products": row.append(productList(d.products)); break;
    case "handoff":
      row.append(element("p", d.handoff.status === "resolved" ? "Обращение закрыто" : "Передано менеджеру · " + reasons[d.handoff.reason]));
      break;
    case "quote": row.append(element("p", "КП · версия " + d.quote.revision)); break;
    case "document": row.append(element("p", d.document.kind === "demo_invoice" ? "Выдан демо-счёт" : "Выдано КП")); break;
    case "action_failed": row.append(element("p", d.error.message)); break;
  }
  return row;
}

function draft(id) {
  if (!drafts.has(id)) drafts.set(id, { text: "", pending: null, busy: false, notice: "" });
  return drafts.get(id);
}

function showComposer() {
  if (!selected) return;
  const state = draft(selected.handoffId);
  if ($("reply").value !== state.text) $("reply").value = state.text;
  $("reply").readOnly = Boolean(state.pending);
  for (const button of $("compose").querySelectorAll("button")) {
    button.disabled = state.busy || !state.text.trim() || Boolean(state.pending && state.pending.body.type !== button.value);
  }
  $("result").textContent = state.notice;
}

function showQueue() {
  const nav = $("queue");
  const focused = nav.contains(document.activeElement) ? document.activeElement.dataset.id : null;
  nav.replaceChildren();
  $("count").textContent = items.length;
  $("queue-empty").hidden = items.length > 0;
  for (const item of items) {
    const button = document.createElement("button");
    button.dataset.id = item.handoffId;
    button.setAttribute("aria-current", String(selected?.handoffId === item.handoffId));
    button.append(element("strong", reasons[item.reason]), element("span", item.question));
    button.onclick = () => openCard(item);
    nav.append(button);
    if (focused === item.handoffId) button.focus({ preventScroll: true });
  }
}

function openCard(item) {
  generation++;
  selected = item;
  cursor = 0;
  $("history").replaceChildren();
  $("result").textContent = "";
  $("card").hidden = !item;
  $("no-selection").hidden = Boolean(item);
  if (item) {
    $("reason").textContent = reasons[item.reason];
    $("question").textContent = item.question;
    $("products").replaceChildren();
    if (item.products.length) $("products").append(element("h3", "Выбранные позиции"), productList(item.products));
    showComposer();
    readHistory();
  }
  showQueue();
}

function connectionError(error) {
  if (error.status === 401 || error.status === 403) {
    authenticated = false;
    refreshNumber++;
    clearTimeout(timer);
    drafts.clear();
    items = [];
    openCard(null);
    $("desk").hidden = true;
    $("login").hidden = false;
    $("connection").textContent = "Требуется вход";
  } else {
    $("connection").textContent = error.status ? error.message : "Нет связи с сервером. Повторяем подключение…";
  }
}

async function readHistory() {
  if (!selected || Date.now() < retryAt) return;
  const id = selected.conversationId, version = generation, attempt = ++historyNumber;
  try {
    for (;;) {
      const page = await request("/conversations/" + encodeURIComponent(id) + "/events?after=" + cursor);
      if (generation !== version || attempt !== historyNumber) return;
      const list = $("history");
      const atEnd = list.scrollTop + list.clientHeight >= list.scrollHeight - 12;
      for (const e of page.events) {
        if (e.seq <= cursor) continue;
        list.append(eventRow(e));
        cursor = e.seq;
      }
      cursor = Math.max(cursor, page.nextAfter);
      if (atEnd) list.scrollTop = list.scrollHeight;
      if (!page.hasMore) break;
    }
  } catch (error) {
    if (generation === version && attempt === historyNumber) connectionError(error);
  }
}

async function refresh() {
  const attempt = ++refreshNumber;
  clearTimeout(timer);
  try {
    if (Date.now() < retryAt) return;
    const { items: incoming } = await request("/manager/handoffs");
    if (attempt !== refreshNumber) return;
    authenticated = true;
    $("login").hidden = true;
    $("desk").hidden = false;
    $("connection").textContent = "Очередь обновлена · " + clock.format(new Date());
    const changed = JSON.stringify(items) !== JSON.stringify(incoming);
    items = incoming;
    if (selected && !items.some(item => item.handoffId === selected.handoffId)) {
      const id = selected.handoffId;
      openCard(null);
      drafts.delete(id);
      $("result").textContent = "Обращение закрыто.";
    } else {
      if (changed) showQueue();
      await readHistory();
    }
  } catch (error) {
    if (attempt === refreshNumber) connectionError(error);
  } finally {
    if (attempt === refreshNumber && ($("login").hidden || authenticated)) {
      timer = setTimeout(refresh, Math.max(5000, retryAt - Date.now()));
    }
  }
}

$("login").onsubmit = async e => {
  e.preventDefault();
  const form = e.currentTarget, button = form.querySelector("button");
  if (button.disabled) return;
  button.disabled = true;
  $("login-error").textContent = "";
  try {
    await request("/auth/manager/login", Object.fromEntries(new FormData(form)), crypto.randomUUID());
    form.reset();
    await refresh();
  } catch (error) {
    $("login-error").textContent = error.status ? error.message : "Не удалось подключиться. Повторите вход.";
  } finally {
    button.disabled = false;
  }
};

$("reply").oninput = () => {
  draft(selected.handoffId).text = $("reply").value;
  showComposer();
};

$("compose").onsubmit = async e => {
  e.preventDefault();
  if (!selected) return;
  const id = selected.handoffId, state = draft(id);
  if (state.busy || !state.text.trim()) return;
  state.pending ??= { key: crypto.randomUUID(), body: { type: e.submitter?.value || "reply", reply: state.text.trim() } };
  state.busy = true;
  state.notice = "Отправка…";
  showComposer();
  try {
    await request("/manager/handoffs/" + encodeURIComponent(id) + "/resolution", state.pending.body, state.pending.key);
    state.text = "";
    state.pending = null;
    state.notice = "Ответ отправлен.";
  } catch (error) {
    if ((error.status === 401 || error.status === 403) && authenticated) return connectionError(error);
    if (error.status && error.status < 500) state.pending = null;
    state.notice = state.pending ? "Доставка не подтверждена. Нажмите ту же кнопку для повтора." : error.message;
  } finally {
    state.busy = false;
    if (selected?.handoffId === id) showComposer();
    if (authenticated) await refresh();
  }
};

refresh();
