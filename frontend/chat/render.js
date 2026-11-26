const names = { customer: "Вы", agent: "Ассистент", manager: "Оператор" };
const rubles = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
const money = kopecks => rubles.format(kopecks / 100) + " ₽";

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function sourceLink(source) {
  const link = element("a", source.label + " ↗");
  try {
    const url = new URL(source.url);
    if (url.protocol === "https:") {
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
  } catch { /* A malformed source can still be shown as a label. */ }
  return link;
}

function productCard(product) {
  const card = element("div", null, "result");
  const price = product.priceState === "on_request" ? "Цена по запросу" : money(product.unitPriceKopecks) + " за упаковку";
  card.append(element("strong", product.name), element("div", price));
  if (product.priceState === "availability_check") card.append(element("small", "Наличие требует подтверждения"));
  card.append(element("small", product.licenseTerm ? "Срок: " + product.licenseTerm : "Срок лицензии неизвестен"), sourceLink(product.source));
  return card;
}

export function renderEvent({ data, at }) {
  const row = element("div", null, "entry");
  const time = new Date(at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  switch (data.type) {
    case "message":
      row.classList.add(data.author);
      row.append(element("div", names[data.author] + " · " + time, "meta"), element("div", data.text, "bubble"));
      for (const source of data.sources) row.append(sourceLink(source));
      break;
    case "products":
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
      break;
    case "document": {
      const link = element("a", data.document.kind === "quote_pdf" ? "Скачать КП" : "Скачать демо-счёт");
      if (/^\/api\/v1\/documents\/[\w.-]+$/.test(data.document.url)) link.href = data.document.url;
      row.append(link);
      break;
    }
  }
  return row;
}
