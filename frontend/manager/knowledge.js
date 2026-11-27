"use strict";

async function loadArchive() {
  const cases = await request("/manager/kb/resolved");
  if (!authenticated) return [];
  const nav = $("resolved");
  nav.replaceChildren();
  for (const item of cases) {
    const button = element("button", item.question);
    button.onclick = () => openCard(item);
    nav.append(button);
  }
  if (!cases.length) nav.append(element("p", "Закрытых обращений пока нет."));
  return cases;
}

$("archive").ontoggle = () => {
  if ($("archive").open) loadArchive().catch(connectionError);
};

let articleRequest = 0;

async function loadArticles(caseId) {
  const attempt = ++articleRequest;
  try {
    const articles = await request("/manager/kb/articles");
    if (selected?.handoffId !== caseId || attempt !== articleRequest) return;
    const list = $("articles");
    list.replaceChildren();
    const active = articles.filter(a => a.status === "approved" && !a.retired_at);
    for (const a of articles.filter(row => row.handoff_id === caseId)) {
      const box = document.createElement("article");
      const state = a.retired_at ? (a.replacement_id ? "Заменена" : "Снята") :
        { proposed: "Черновик", approved: "Опубликована", rejected: "Отклонена" }[a.status];
      box.append(element("strong", a.question), element("p", a.answer), element("p", state));
      if (a.phrasings.length) box.append(element("p", a.phrasings.join("\n")));

      async function decide(button, action, replaces) {
        const suffix = replaces ? "?replaces=" + encodeURIComponent(replaces) : "";
        button.disabled = true;
        try {
          await request("/manager/kb/patches/" + encodeURIComponent(a.id) + "/" + action + suffix, {}, newKey());
          if (selected?.handoffId === caseId) $("kb-result").textContent = "Решение сохранено.";
        } catch (error) {
          if (selected?.handoffId === caseId) $("kb-result").textContent = error.message;
        } finally {
          if (selected?.handoffId === caseId) await loadArticles(caseId);
        }
      }

      if (a.status === "proposed") {
        const replace = document.createElement("select");
        replace.setAttribute("aria-label", "Версия для замены");
        replace.add(new Option("Новая статья", ""));
        for (const other of active) {
          replace.add(new Option("Заменить: " + other.question + " — " + other.answer.slice(0, 55) + " · " + other.id.slice(-6), other.id));
        }
        const publish = element("button", "Подтвердить публикацию"), reject = element("button", "Отклонить");
        publish.onclick = () => decide(publish, "approve", replace.value);
        reject.onclick = () => decide(reject, "reject");
        box.append(replace, publish, reject);
      } else if (a.status === "approved" && !a.retired_at) {
        const link = element("a", "Открыть статью");
        link.href = "/api/v1/knowledge/" + encodeURIComponent(a.id);
        link.target = "_blank";
        link.rel = "noopener";
        const withdraw = element("button", "Снять с публикации");
        withdraw.onclick = () => {
          if (window.confirm("Снять эту статью с публикации?")) decide(withdraw, "withdraw");
        };
        box.append(link, withdraw);
      }
      list.append(box);
    }
  } catch (error) {
    if (selected?.handoffId === caseId) $("kb-result").textContent = error.message;
  }
}

$("article-form").onsubmit = async event => {
  event.preventDefault();
  if (selected?.status !== "resolved") return;
  const caseId = selected.handoffId, form = event.currentTarget, button = form.querySelector("button");
  if (button.disabled) return;
  const fields = new FormData(form);
  const body = {
    handoff_id: caseId, scope: "public_faq", question: fields.get("question").trim(),
    answer: fields.get("answer").trim(), phrasings: fields.get("phrasings").split(/\r?\n/).map(s => s.trim()).filter(Boolean)
  };
  button.disabled = true;
  try {
    await request("/manager/kb/patches", body, newKey());
    if (selected?.handoffId !== caseId) return;
    form.reset();
    $("kb-result").textContent = "Черновик сохранён. Проверьте текст перед публикацией.";
    await loadArticles(caseId);
  } catch (error) {
    if (selected?.handoffId === caseId) $("kb-result").textContent = error.message;
  } finally {
    button.disabled = false;
  }
};
