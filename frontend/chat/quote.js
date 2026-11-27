const form = document.getElementById("quote-form");
const dialog = document.getElementById("quote-dialog");
const list = document.getElementById("quote-items");
const openButton = document.getElementById("quote-open");
const status = document.getElementById("quote-status");
let openingRevision = 0, retry = null, busy = false;

export function resetQuotes() {
  retry = null;
  openButton.textContent = "Подготовить КП";
  openButton.hidden = true; dialog.close(); form.reset();
}

export function quoteEvent(data) {
  if (data.type === "products") {
    openButton.hidden = false;
  }
  if (data.type === "quote") {
    openButton.textContent = "Изменить КП";
    openButton.hidden = false;
  }
}

export function setupQuotes(conversation, request, newKey) {
  let generation = 0;
  openButton.onclick = async () => {
    const token = ++generation;
    const cid = conversation();
    if (!cid) return;
    form.querySelector('[type="submit"]').disabled = true;
    status.textContent = "Загрузка…"; list.replaceChildren(); dialog.showModal();
    try {
      const saved = await request("/api/v1/conversations/" + encodeURIComponent(cid) + "/quotes/current");
      if (cid !== conversation() || token !== generation || !dialog.open) return;
      openingRevision = saved.revision;
      if (saved.customer) for (const k of ["company","inn","kpp"]) form.elements[k].value = saved.customer[k];
      const items = new Map(saved.items.map(item => [item.offerId, item.packages]));
      const submit = form.querySelector('[type="submit"]');
      const updateSubmit = () => { submit.disabled = !list.querySelector('input[type="checkbox"]:checked'); };
      for (const {offerId: id, name} of saved.choices) {
        const row = document.createElement("div"), label = document.createElement("label");
        const check = document.createElement("input"), qty = document.createElement("input");
        check.type = "checkbox"; check.value = id;
        check.checked = items.has(id) || (!saved.requiresReselection && saved.choices.length === 1);
        qty.type = "number"; qty.min = "1"; qty.max = "100000"; qty.step = "1";
        qty.setAttribute("aria-label", "Упаковки: " + name);
        qty.value = String(items.get(id) || 1); qty.disabled = !check.checked;
        check.onchange = () => { qty.disabled = !check.checked; updateSubmit(); };
        label.append(check, " " + name + " · ID " + id); row.append(label, qty); list.append(row);
      }
      status.textContent = "";
      if (saved.requiresReselection) {
        status.textContent = saved.choices.length ? "Выберите позиции для новых условий."
          : "Закройте форму и подберите товар в чате для новых условий.";
      }
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
}
