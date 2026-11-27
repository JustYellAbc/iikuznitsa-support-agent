"""Render the dated revision in the partner's six-column proposal format."""

from decimal import Decimal
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.agent.quote_calculation import Snapshot, amount_vat

PENDING = "Требует согласования"


@lru_cache(maxsize=1)
def fonts() -> tuple[str, str]:
    base = Path("/usr/share/fonts/truetype/dejavu")
    files = (base / "DejaVuSans.ttf", base / "DejaVuSans-Bold.ttf")
    if not all(p.exists() for p in files):
        base = Path("C:/Windows/Fonts")
        files = (base / "arial.ttf", base / "arialbd.ttf")
    for name, path in zip(("QuoteRegular", "QuoteBold"), files, strict=True):
        pdfmetrics.registerFont(TTFont(name, str(path)))
    return "QuoteRegular", "QuoteBold"


def money(value: int | None) -> str:
    return f"{Decimal(value) / 100:,.2f}".replace(",", " ").replace(".", ",") if value is not None else PENDING


def render(snapshot: Snapshot) -> bytes:
    regular, bold = fonts()

    def para(value, size=9, strong=False):
        return Paragraph(
            escape(str(value)),
            ParagraphStyle(
                "text",
                fontName=bold if strong else regular,
                fontSize=size,
                leading=size * 1.35,
                textColor=colors.HexColor("#202329"),
                splitLongWords=1,
            ),
        )

    out = BytesIO()
    doc = SimpleDocTemplate(
        out,
        pagesize=A4,
        leftMargin=17 * mm,
        rightMargin=17 * mm,
        topMargin=15 * mm,
        bottomMargin=18 * mm,
        title="Коммерческое предложение",
    )
    q = snapshot.quote
    story = [
        para("ПАО «Софтлайн»", 15, True),
        para("119021, Москва, ул. Льва Толстого, д. 5, стр. 1 · +7 495 232 00 23 · info@softline.com"),
        Spacer(1, 8 * mm),
        para("Коммерческое предложение", 19, True),
        para(
            f"{'Черновик · ' if q.state == 'needs_review' else ''}Версия {q.revision} · "
            f"Исх. № {q.quoteId} от {snapshot.dated:%d.%m.%Y}"
        ),
        Spacer(1, 5 * mm),
        para(f"Куда: {snapshot.customer.company}", 10, True),
        para(f"ИНН {snapshot.customer.inn}   КПП {snapshot.customer.kpp}"),
        Spacer(1, 4 * mm),
        para("Уважаемый заказчик! Предлагаем вашему вниманию следующие позиции."),
        Spacer(1, 3 * mm),
    ]
    header = ("Артикул", "Наименование", "Кол-во, уп.", "Цена, руб.", "Сумма, руб.", "В т.ч. НДС, руб.")
    rows = [[para(x, 8, True) for x in header]]
    for d in snapshot.details:
        rows.append(
            [
                para(x, 8)
                for x in (
                    d.article or PENDING,
                    f"{d.product.name} (ID {d.product.offerId})",
                    d.packages,
                    money(d.unit),
                    money(d.unit * d.packages if d.unit else None),
                    money(amount_vat(d)),
                )
            ]
        )
    rows.append(
        [
            para(x, 8, True)
            for x in (
                "",
                "Итого",
                "",
                "",
                money(q.totalKopecks),
                money(sum(amount_vat(d) for d in snapshot.details))
                if all(amount_vat(d) is not None for d in snapshot.details)
                else PENDING,
            )
        ]
    )
    table = Table(rows, colWidths=[v * mm for v in (27, 48, 15, 26, 29, 29)], repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#ccd0d4")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf0f2")),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.extend([table, Spacer(1, 4 * mm)])
    if q.state == "needs_review":
        story.append(
            para(f"Сумма известных строк: {money(q.knownSubtotalKopecks)} руб. Итоговые условия требуют согласования.")
        )
    for text in (
        "Цены каталога конечные и включают применимые налоги и сборы.",
        f"Условия оплаты: {q.terms.payment or PENDING}.",
        f"Срок поставки: {q.terms.delivery or PENDING}.",
        f"Срок действия предложения: {q.terms.validUntil or PENDING}.",
    ):
        story.append(para(text))
    story.extend([Spacer(1, 5 * mm), para("Источники расчёта", 9, True)])
    for d in snapshot.details:
        story.append(para(f"ID {d.product.offerId} · снимок {d.feedDate} · {d.product.source.url}", 8))
        if d.conditions:
            story.append(para(f"Коммерческие условия: {d.conditions.source}", 8))
        if d.decision:
            story.append(para(f"Решение менеджера: {d.decision}", 8))
    doc.build(story)
    return out.getvalue()
