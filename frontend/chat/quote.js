const form = document.getElementById("quote-form");
const dialog = document.getElementById("quote-dialog");
const list = document.getElementById("quote-items");
const status = document.getElementById("quote-status");
let openingRevision = 0, retry = null, busy = false;
let openForm, latestRevision = 0;
let refreshNotes, refreshTimer;

export function resetQuotes() {
  clearTimeout(refreshTimer);
  retry = null;
  latestRevision = 0;
  dialog.close(); form.reset();
}

export function quoteEvent(data, row) {
  let offers, revision, label;
  if (data.type === "products" && data.products.length) {
    offers = new Set(data.products.map(product => product.offerId));
    label = "Подготовить КП";
  } else if (data.type === "quote") {
    revision = latestRevision = data.quote.revision;
    const sameQuote = document.querySelectorAll(`[data-quote-status="${CSS.escape(data.quote.quoteId)}"]`);
    for (const status of sameQuote) {
      status.textContent = Number(status.dataset.version) < revision ? "Предыдущая версия" : "Ранее в этой версии";
    }
    const status = document.createElement("small");
    status.dataset.quoteStatus = data.quote.quoteId;
    status.dataset.version = revision;
    status.textContent = "Последняя версия в чате";
    row.firstElementChild.after(status);
    if (revision > 1) {
      const notes = document.createElement("div");
      notes.dataset.noteFor = data.quote.quoteId;
      notes.dataset.version = revision;
      status.after(notes);
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(refreshNotes, 0);
    }
    for (const previous of document.querySelectorAll(".quote-open[data-revision]")) {
      if (Number(previous.dataset.revision) < revision) previous.disabled = true;
    }
    label = "Изменить КП";
  } else if (data.type === "handoff" && data.handoff.quoteLine && data.handoff.status === "open") {
    revision = data.handoff.quoteLine.revision;
    label = "Изменить количество в КП";
  } else if (latestRevision && data.type === "message" && data.author === "agent" && data.text.includes("«Изменить КП»")) {
    revision = latestRevision;
    label = "Изменить КП";
  } else return;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "quote-open";
  button.textContent = label;
  if (revision !== undefined) {
    button.dataset.revision = revision;
    button.disabled = revision < latestRevision;
  }
  button.onclick = () => openForm({offers, revision});
  row.append(button);
}

export function setupQuotes(conversation, request, newKey) {
  refreshNotes = async () => {
    const cid = conversation();
    const pending = [...document.querySelectorAll("[data-note-for]")].filter(node => !node.childElementCount);
    if (!pending.length) return;
    try {
      const history = await request("/api/v1/conversations/" + encodeURIComponent(cid) + "/quotes/history");
      if (cid !== conversation()) return;
      for (const node of pending) {
        const found = history.revisions.find(entry => entry.quoteId === node.dataset.noteFor && entry.revision === Number(node.dataset.version));
        if (!found) continue;
        const summary = document.createElement("p");
        summary.textContent = "Изменения к версии " + found.previous + ". " + found.changes.join(" ");
        node.replaceChildren(summary);
      }
    } catch {
      if (cid !== conversation()) return;
      for (const node of pending.filter(node => !node.childElementCount)) {
        const retry = document.createElement("button");
        retry.type = "button"; retry.textContent = "Загрузить изменения";
        retry.onclick = () => { node.replaceChildren(); refreshNotes(); };
        node.append(retry);
      }
    }
  };
  let generation = 0;
  openForm = async ({offers, revision, prefillCurrent = false}) => {
    const token = ++generation;
    const cid = conversation();
    if (!cid) return;
    form.querySelector('[type="submit"]').disabled = true;
    status.textContent = "Загрузка…"; list.replaceChildren(); dialog.showModal();
    try {
      const saved = await request("/api/v1/conversations/" + encodeURIComponent(cid) + "/quotes/current");
      if (cid !== conversation() || token !== generation || !dialog.open) return;
      if (revision !== undefined && revision !== saved.revision) {
        status.textContent = "КП изменилось. Откройте последнюю версию расчёта в чате.";
        return;
      }
      openingRevision = saved.revision;
      if (saved.customer) for (const k of ["company","inn","kpp"]) form.elements[k].value = saved.customer[k];
      const items = new Map(saved.items.map(item => [item.offerId, item.packages]));
      const choices = offers ? saved.choices.filter(item => offers.has(item.offerId)) : saved.choices;
      const selection = new Map((saved.selection || []).map(item => [item.offerId, item.priceState]));
      const submit = form.querySelector('[type="submit"]');
      const updateSubmit = () => { submit.disabled = !list.querySelector('input[type="checkbox"]:checked'); };
      for (const {offerId: id, name} of choices) {
        const row = document.createElement("div"), label = document.createElement("label");
        const check = document.createElement("input"), qty = document.createElement("input");
        check.type = "checkbox"; check.value = id;
        check.checked = items.has(id) || (!saved.requiresReselection && (offers || !items.size) &&
          (choices.length === 1 || (prefillCurrent && selection.has(id))));
        qty.type = "number"; qty.min = "1"; qty.max = "100000"; qty.step = "1";
        qty.setAttribute("aria-label", "Количество: " + name);
        qty.value = String(items.get(id) || 1); qty.disabled = !check.checked;
        check.onchange = () => { qty.disabled = !check.checked; updateSubmit(); };
        const priceNote = selection.get(id) === "on_request" ? " · Цена по запросу" : "";
        label.append(check, " " + name + " · ID " + id + priceNote); row.append(label, qty); list.append(row);
      }
      status.textContent = "";
      if (!choices.length) status.textContent = "Для этой подборки нет актуальных позиций. Подберите товар в чате.";
      else if (saved.requiresReselection) status.textContent = "Выберите позиции для новых условий.";
      else if (offers && saved.revision) status.textContent = "Выбранные позиции составят новую версию КП.";
      updateSubmit();
    } catch (error) { status.textContent = error.message; }
  };
  document.getElementById("quote-cancel").onclick = () => dialog.close();
  form.onsubmit = async e => {
    e.preventDefault();
    if (busy) return;
    const items = Array.from(list.children).filter(row => row.querySelector('[type="checkbox"]').checked)
      .map(row => ({offerId:row.querySelector('[type="checkbox"]').value, packages:Number(row.querySelector('[type="number"]').value)}));
    if (!items.length) { status.textContent = "Выберите хотя бы одну позицию."; return; }
    const customer = Object.fromEntries(["company","inn","kpp"].map(k => [k, form.elements[k].value.trim()]));
    const body = {baseRevision:openingRevision,customer,items}, serialized = JSON.stringify(body);
    if (!retry || retry.serialized !== serialized) retry = {serialized,key:newKey()};
    busy = true; const button = form.querySelector('[type="submit"]'); button.disabled = true;
    try {
      await request("/api/v1/conversations/" + encodeURIComponent(conversation()) + "/quotes",retry.key,body);
      retry = null; dialog.close();
    } catch(error) {
      status.textContent = error.code === "stale_revision" ? "КП изменилось. Закройте и снова откройте форму." : error.message;
    } finally { busy=false; button.disabled=false; }
  };
  return () => openForm({prefillCurrent: true});
}
