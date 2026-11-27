const names = { customer: "Вы", agent: "Ассистент", manager: "Оператор" };
const rubles = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
const money = kopecks => rubles.format(kopecks / 100) + " ₽";

export function buyerUrl(path) {
  const url = new URL(path, location.origin);
  url.searchParams.set("view", "buyer");
  return url.pathname + url.search;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function sourceLink(source, label = source.label) {
  const link = element("a", label + " ↗");
  try {
    const url = new URL(source.url);
    if (url.protocol === "https:") {
      // Local preview may serve this same API over HTTP. Published articles belong to this stand.
      link.href = url.host === window.location.host && url.pathname.startsWith("/api/v1/knowledge/")
        ? url.pathname : url.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
  } catch { /* A malformed source can still be shown as a label. */ }
  return link;
}

function productCard(product) {
  const card = element("div", null, "result");
  const price = product.priceState === "on_request" ? "Цена по запросу" : money(product.unitPriceKopecks) + " за единицу по карточке";
  card.append(element("strong", product.name), element("small", "Артикул " + product.offerId), element("div", price));
  if (product.priceState === "availability_check") card.append(element("small", "Наличие требует подтверждения"));
  const link = sourceLink(product.source, "Открыть в магазине");
  link.setAttribute("aria-label", "Открыть в магазине · " + product.name);
  card.append(element("small", product.licenseTerm ? "Срок: " + product.licenseTerm : "Срок лицензии неизвестен"), link);
  return card;
}

export function renderEvent({ data, at, actionId }, conversationId, submit, cardLinks = new Set()) {
  const row = element("div", null, "entry");
  const time = new Date(at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  switch (data.type) {
    case "message":
      row.classList.add(data.author);
      row.append(element("div", names[data.author] + " · " + time, "meta"), element("div", data.text, "bubble"));
      for (const source of data.sources) {
        const link = sourceLink(source);
        if (!cardLinks.has(link.href)) row.append(link);
      }
      break;
    case "products":
      row.dataset.productsFor = actionId;
      for (const product of data.products) row.append(productCard(product));
      break;
    case "handoff":
      row.classList.add("handoff");
      row.append(element("strong", data.handoff.status === "open" ? "Обращение передано оператору" : "Обращение закрыто"), element("p", data.handoff.question));
      break;
    case "action_failed":
      row.append(element("div", data.error.message, "bubble"));
      break;
    case "quote":
      row.append(element("strong", "Расчёт · версия " + data.quote.revision),
        element("p", data.quote.totalKopecks == null ? "Условия на согласовании" : "Итого " + money(data.quote.totalKopecks)));
      for (const line of data.quote.lines) {
        row.append(element("p", line.name + " · " + line.packages + " уп. × " +
          (line.unitPriceKopecks == null ? "цена на согласовании" : money(line.unitPriceKopecks))));
      }
      for (const [field, label] of [["payment","Оплата"],["delivery","Поставка"],["taxNote","Налоги"],["validUntil","Действует до"]]) {
        row.append(element("div", label + ": " + (data.quote.terms[field] || "требует согласования")));
      }
      if (data.quote.state === "needs_review") {
        const pendingPrices = data.quote.lines.filter(line => line.unitPriceKopecks == null).length;
        if (pendingPrices) row.append(element("p", "Ожидают подтверждения цены: " + pendingPrices + " из " + data.quote.lines.length));
        row.append(element("small", "Сумма известных строк: " + money(data.quote.knownSubtotalKopecks)));
        const link = element("a", "Скачать черновик КП");
        link.href = buyerUrl("/api/v1/conversations/" + encodeURIComponent(conversationId) + "/quotes/" +
          encodeURIComponent(data.quote.quoteId) + "/" + encodeURIComponent(data.quote.revision) + "/preview.pdf");
        row.append(link);
      } else if (submit) {
        const get = element("button", "Получить PDF КП");
        get.type = "button"; get.dataset.confirmQuote = data.quote.quoteId;
        get.onclick = () => submit({type:"request_document",kind:"quote_pdf",quoteId:data.quote.quoteId,revision:data.quote.revision});
        row.append(get);
      }
      if (data.quote.state === "ready" && submit) {
        const confirm = element("button", "Подтвердить условия КП");
        confirm.type = "button"; confirm.dataset.confirmQuote = data.quote.quoteId;
        confirm.onclick = () => submit({type:"confirm_quote", quoteId:data.quote.quoteId, revision:data.quote.revision});
        row.append(confirm);
      }
      if (data.quote.state === "confirmed" && submit) {
        const invoice = element("button", "Получить демо-счёт");
        invoice.type = "button"; invoice.dataset.confirmQuote = data.quote.quoteId;
        invoice.onclick = () => submit({type:"request_document",kind:"demo_invoice",quoteId:data.quote.quoteId,revision:data.quote.revision});
        row.append(invoice);
      }
      break;
    case "document": {
      const link = element("a", data.document.kind === "quote_pdf" ? "Скачать КП" : "Скачать демо-счёт");
      if (/^\/api\/v1\/documents\/[\w.-]+$/.test(data.document.url)) link.href = buyerUrl(data.document.url);
      row.append(link);
      break;
    }
  }
  return row;
}
