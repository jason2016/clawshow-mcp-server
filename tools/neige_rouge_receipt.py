"""
Neige Rouge 红雪餐厅 — PDF receipt & invoice generator (French only, Helvetica)
Receipt number format : NR-YYYY-NNNN
Invoice number format : F-YYYY-NNNN
"""
from __future__ import annotations

import json
from datetime import datetime
from io import BytesIO

from fpdf import FPDF

RESTAURANT_INFO = {
    "name": "NEIGE ROUGE",
    "subtitle": "Cuisine Vietnamienne Authentique",
    "address": "75 Rue Buffon, 75005 Paris",
    "phone": "01 72 60 48 89",
    "siret": "82207280700016",
    "tva_number": "",          # a completer par le proprietaire
    "rcs": "",                 # a completer par le proprietaire
    "default_vat_rate_dine_in":  0.10,
    "default_vat_rate_takeaway": 0.055,
    "alcohol_vat_rate": 0.20,
}

# Primary brand colour (dark red)
_RED = (139, 0, 0)
_DARK = (30, 10, 10)
_GREY = (100, 90, 85)
_LIGHT = (180, 170, 165)
_BLACK = (30, 30, 30)


def _fmt(amount: float) -> str:
    s = f"{amount:.2f}"
    parts = s.split(".")
    intpart = parts[0]
    groups: list[str] = []
    while len(intpart) > 3:
        groups.insert(0, intpart[-3:])
        intpart = intpart[:-3]
    groups.insert(0, intpart)
    return " ".join(groups) + "," + parts[1] + " EUR"


def _effective_vat_rate(item: dict, order_type: str) -> float:
    """Return the correct VAT rate for an item."""
    if item.get("vat_rate"):
        return float(item["vat_rate"])
    if order_type == "takeaway":
        return RESTAURANT_INFO["default_vat_rate_takeaway"]
    return RESTAURANT_INFO["default_vat_rate_dine_in"]


def _vat_breakdown(items: list, order_type: str) -> dict[float, dict]:
    groups: dict[float, dict] = {}
    for item in items:
        rate = _effective_vat_rate(item, order_type)
        qty = int(item.get("qty", 1))
        price_ttc = float(item.get("price", 0)) * qty
        price_ht = round(price_ttc / (1 + rate), 4)
        tva = round(price_ttc - price_ht, 4)
        if rate not in groups:
            groups[rate] = {"ht": 0.0, "tva": 0.0, "ttc": 0.0}
        groups[rate]["ht"] += price_ht
        groups[rate]["tva"] += tva
        groups[rate]["ttc"] += price_ttc
    for g in groups.values():
        g["ht"] = round(g["ht"], 2)
        g["tva"] = round(g["tva"], 2)
        g["ttc"] = round(g["ttc"], 2)
    return groups


class NRReceiptPDF(FPDF):
    def __init__(self):
        super().__init__(unit="mm", format="A4")
        self.set_margins(20, 15, 20)
        self.set_auto_page_break(auto=True, margin=15)

    def _font(self, size: int, bold: bool = False):
        self.set_font("Helvetica", style="B" if bold else "", size=size)

    def _color(self, rgb: tuple):
        self.set_text_color(*rgb)

    def _line_sep(self, dashed: bool = False):
        self.set_draw_color(*_RED)
        if dashed:
            self.set_line_width(0.2)
            self.dashed_line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y(), 2, 2)
        else:
            self.set_line_width(0.4)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def _header_block(self):
        ri = RESTAURANT_INFO
        self._font(20, True)
        self._color(_RED)
        self.cell(0, 11, ri["name"], align="C", new_x="LMARGIN", new_y="NEXT")
        self._font(9)
        self._color(_GREY)
        self.cell(0, 5, ri["subtitle"], align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(2)
        self._font(8)
        self._color(_LIGHT)
        self.cell(0, 4, ri["address"], align="C", new_x="LMARGIN", new_y="NEXT")
        self.cell(0, 4, "Tel : " + ri["phone"], align="C", new_x="LMARGIN", new_y="NEXT")
        self.cell(0, 4, "SIRET : " + ri["siret"], align="C", new_x="LMARGIN", new_y="NEXT")
        if ri["tva_number"]:
            self.cell(0, 4, "N° TVA : " + ri["tva_number"], align="C", new_x="LMARGIN", new_y="NEXT")
        if ri["rcs"]:
            self.cell(0, 4, "RCS : " + ri["rcs"], align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(3)


def generate_receipt(order: dict) -> bytes:
    items = order.get("items", [])
    if isinstance(items, str):
        items = json.loads(items)
    order_type = order.get("order_type", "dine_in")

    pdf = NRReceiptPDF()
    pdf.add_page()

    pdf._header_block()
    pdf._line_sep()

    # Title
    receipt_num = order.get("receipt_number") or order.get("order_number", "---")
    pdf._font(11, True)
    pdf._color(_RED)
    pdf.cell(0, 8, "RECU N° " + receipt_num, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)

    # Meta
    pdf._font(8)
    pdf._color(_GREY)
    created = order.get("created_at", "")
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        date_str = dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        date_str = created[:16] if created else "---"

    mode = "A emporter" if order_type == "takeaway" else "Sur place"
    order_num = order.get("order_number", "")

    pdf.cell(0, 4, "Date : " + date_str, align="C", new_x="LMARGIN", new_y="NEXT")
    if order_num:
        pdf.cell(0, 4, "Commande N° " + order_num, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 4, "Mode : " + mode, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf._line_sep(dashed=True)

    # Items
    lw = pdf.w - pdf.l_margin - pdf.r_margin
    for item in items:
        qty = int(item.get("qty", 1))
        name = item.get("name") or item.get("name_fr") or "---"
        price = float(item.get("price", 0))
        line_total = price * qty
        opts = item.get("options")

        pdf._font(9)
        pdf._color(_BLACK)
        pdf.cell(lw - 28, 6, str(qty) + "x  " + name, new_x="RIGHT", new_y="TOP")
        pdf.cell(28, 6, _fmt(line_total), align="R", new_x="LMARGIN", new_y="NEXT")
        if opts:
            opts_str = "  " + " · ".join(str(v) for v in opts.values() if v)
            if opts_str.strip():
                pdf._font(7.5)
                pdf._color(_LIGHT)
                pdf.cell(0, 4, opts_str, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(2)
    pdf._line_sep(dashed=True)

    # Totals
    vat = _vat_breakdown(items, order_type)
    total_ht = sum(g["ht"] for g in vat.values())
    total_amount = float(order.get("total_amount") or sum(float(i.get("price", 0)) * int(i.get("qty", 1)) for i in items))

    pdf._font(8)
    pdf._color(_GREY)
    pdf.cell(lw - 28, 5, "Sous-total HT", new_x="RIGHT", new_y="TOP")
    pdf.cell(28, 5, _fmt(round(total_ht, 2)), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)

    for rate, g in sorted(vat.items()):
        label = "alcool" if rate >= 0.19 else ("emporter" if rate <= 0.06 else "repas")
        pdf.cell(lw - 28, 4, "TVA " + str(int(round(rate * 100))) + "% (" + label + ")", new_x="RIGHT", new_y="TOP")
        pdf.cell(28, 4, _fmt(g["tva"]), align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(2)
    pdf.set_draw_color(*_RED)
    pdf.set_line_width(0.6)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(1)

    pdf._font(12, True)
    pdf._color(_RED)
    pdf.cell(lw - 28, 8, "TOTAL TTC", new_x="RIGHT", new_y="TOP")
    pdf.cell(28, 8, _fmt(total_amount), align="R", new_x="LMARGIN", new_y="NEXT")

    deposit_applied = float(order.get("deposit_applied") or 0)
    if deposit_applied > 0:
        amount_paid = max(0.0, round(total_amount - deposit_applied, 2))
        pdf.ln(1)
        pdf._font(9)
        pdf._color(_GREY)
        pdf.cell(lw - 28, 5, "Acompte deduit", new_x="RIGHT", new_y="TOP")
        pdf.cell(28, 5, "-" + _fmt(deposit_applied), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)
        pdf.set_draw_color(*_RED)
        pdf.set_line_width(0.8)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(1)
        pdf._font(12, True)
        pdf._color(_RED)
        pdf.cell(lw - 28, 8, "Montant paye", new_x="RIGHT", new_y="TOP")
        pdf.cell(28, 8, _fmt(amount_paid), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*_RED)
        pdf.set_line_width(0.6)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(4)
    else:
        pdf.set_draw_color(*_RED)
        pdf.set_line_width(0.6)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(4)

    # Payment method
    pdf._font(8)
    pdf._color(_GREY)
    pm = order.get("payment_method") or "stancer"
    pm_labels = {
        "stancer": "Carte bancaire (Stancer)",
        "online": "Carte bancaire (Stancer)",
        "card_counter": "Carte bancaire (comptoir)",
        "cash": "Especes",
        "pending_counter": "Carte bancaire (comptoir)",
        "pending_cash": "Especes",
    }
    pm_label = pm_labels.get(pm, pm)
    pdf.cell(0, 4, "Paye par : " + pm_label, new_x="LMARGIN", new_y="NEXT")
    pid = order.get("payment_id") or ""
    if pid:
        short = pid[:12] + "..." + pid[-6:] if len(pid) > 20 else pid
        pdf.cell(0, 4, "Ref. paiement : " + short, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf._line_sep(dashed=True)

    # Legal
    pdf._font(6.5)
    pdf._color(_LIGHT)
    for line in [
        "TVA non applicable sur les paiements intermedies par un",
        "etablissement bancaire de l'UE (CGI Art. 286 I-3 bis)",
        "",
        "Systeme de caisse auto-certifie conforme aux criteres ISCA",
        "(Loi n 2026-103 du 19/02/2026, Art. 125)",
    ]:
        pdf.cell(0, 3.5, line, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf._line_sep(dashed=True)

    # Footer
    pdf._font(9, True)
    pdf._color(_RED)
    pdf.cell(0, 6, "Merci de votre visite !", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf._font(7)
    pdf._color(_LIGHT)
    pdf.cell(0, 4, "Laissez un avis Google - nous l'apprecions vraiment !", align="C", new_x="LMARGIN", new_y="NEXT")

    buf = BytesIO()
    pdf.output(buf)
    return buf.getvalue()


def generate_invoice(order: dict, client_company: str, client_address: str,
                     client_vat_number: str = "", invoice_number: str = "") -> bytes:
    items = order.get("items", [])
    if isinstance(items, str):
        items = json.loads(items)
    order_type = order.get("order_type", "dine_in")

    pdf = NRReceiptPDF()
    pdf.add_page()

    pdf._header_block()
    pdf._line_sep()

    # Title
    inv_num = invoice_number or "F-XXXX-XXXX"
    pdf._font(11, True)
    pdf._color(_RED)
    pdf.cell(0, 8, "FACTURE N° " + inv_num, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)

    created = order.get("created_at", "")
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        date_str = dt.strftime("%d/%m/%Y")
    except Exception:
        date_str = created[:10] if created else "---"

    ri = RESTAURANT_INFO
    lw = pdf.w - pdf.l_margin - pdf.r_margin
    col_w = lw / 2 - 5

    # Emetteur / Client columns
    pdf._font(8, True)
    pdf._color(_RED)
    y = pdf.get_y()
    pdf.set_xy(pdf.l_margin, y)
    pdf.cell(col_w, 5, "EMETTEUR :")
    pdf.set_xy(pdf.l_margin + col_w + 10, y)
    pdf.cell(col_w, 5, "CLIENT :")
    pdf.ln(5)

    left_lines = [
        ri["name"],
        ri["address"],
        "SIRET : " + ri["siret"],
    ]
    if ri["tva_number"]:
        left_lines.append("N° TVA : " + ri["tva_number"])
    if ri["rcs"]:
        left_lines.append("RCS : " + ri["rcs"])

    right_lines = [client_company, client_address]
    if client_vat_number:
        right_lines.append("N° TVA : " + client_vat_number)

    start_y = pdf.get_y()
    for i, txt in enumerate(left_lines):
        pdf._font(7.5)
        pdf._color(_GREY)
        pdf.set_xy(pdf.l_margin, start_y + i * 4.5)
        pdf.cell(col_w, 4.5, txt)
    for i, txt in enumerate(right_lines):
        pdf._font(7.5)
        pdf._color(_GREY)
        pdf.set_xy(pdf.l_margin + col_w + 10, start_y + i * 4.5)
        pdf.cell(col_w, 4.5, txt)
    pdf.set_y(start_y + max(len(left_lines), len(right_lines)) * 4.5 + 2)

    pdf._font(8)
    pdf._color(_GREY)
    mode = "A emporter" if order_type == "takeaway" else "Sur place"
    pdf.cell(0, 4, "Date : " + date_str + "   |   Echeance : Payee   |   Mode : " + mode, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf._line_sep(dashed=True)

    # Items table header
    pdf._font(8, True)
    pdf._color(_RED)
    pdf.cell(lw - 52, 5, "Designation", new_x="RIGHT", new_y="TOP")
    pdf.cell(12, 5, "Qte", align="R", new_x="RIGHT", new_y="TOP")
    pdf.cell(20, 5, "P.U. HT", align="R", new_x="RIGHT", new_y="TOP")
    pdf.cell(20, 5, "Total HT", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf._line_sep(dashed=True)

    # Items
    for item in items:
        qty = int(item.get("qty", 1))
        name = item.get("name") or item.get("name_fr") or "---"
        price_ttc = float(item.get("price", 0))
        rate = _effective_vat_rate(item, order_type)
        price_ht = round(price_ttc / (1 + rate), 2)
        total_ht_item = round(price_ht * qty, 2)
        rate_label = "TVA " + str(int(round(rate * 100))) + "%"

        pdf._font(8)
        pdf._color(_BLACK)
        pdf.cell(lw - 52, 5, name + "  (" + rate_label + ")", new_x="RIGHT", new_y="TOP")
        pdf.cell(12, 5, str(qty), align="R", new_x="RIGHT", new_y="TOP")
        pdf.cell(20, 5, _fmt(price_ht), align="R", new_x="RIGHT", new_y="TOP")
        pdf.cell(20, 5, _fmt(total_ht_item), align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(2)
    pdf._line_sep(dashed=True)

    # Totals
    vat = _vat_breakdown(items, order_type)
    total_ht_sum = round(sum(g["ht"] for g in vat.values()), 2)
    total_amount = float(order.get("total_amount") or sum(float(i.get("price", 0)) * int(i.get("qty", 1)) for i in items))

    pdf._font(8)
    pdf._color(_GREY)
    pdf.cell(lw - 28, 5, "Total HT", new_x="RIGHT", new_y="TOP")
    pdf.cell(28, 5, _fmt(total_ht_sum), align="R", new_x="LMARGIN", new_y="NEXT")

    for rate, g in sorted(vat.items()):
        label = "alcool" if rate >= 0.19 else ("emporter" if rate <= 0.06 else "repas")
        pdf.cell(lw - 28, 4, "TVA " + str(int(round(rate * 100))) + "% (" + label + ")", new_x="RIGHT", new_y="TOP")
        pdf.cell(28, 4, _fmt(g["tva"]), align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(1)
    pdf.set_draw_color(*_RED)
    pdf.set_line_width(0.6)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(1)

    pdf._font(11, True)
    pdf._color(_RED)
    pdf.cell(lw - 28, 8, "Total TTC", new_x="RIGHT", new_y="TOP")
    pdf.cell(28, 8, _fmt(total_amount), align="R", new_x="LMARGIN", new_y="NEXT")

    deposit_applied = float(order.get("deposit_applied") or 0)
    if deposit_applied > 0:
        amount_paid = max(0.0, round(total_amount - deposit_applied, 2))
        pdf.ln(1)
        pdf._font(9)
        pdf._color(_GREY)
        pdf.cell(lw - 28, 5, "Acompte deduit", new_x="RIGHT", new_y="TOP")
        pdf.cell(28, 5, "-" + _fmt(deposit_applied), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)
        pdf.set_draw_color(*_RED)
        pdf.set_line_width(0.8)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(1)
        pdf._font(11, True)
        pdf._color(_RED)
        pdf.cell(lw - 28, 8, "Montant paye", new_x="RIGHT", new_y="TOP")
        pdf.cell(28, 8, _fmt(amount_paid), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*_RED)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(4)
    else:
        pdf.set_draw_color(*_RED)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(4)

    # Conditions
    pm = order.get("payment_method") or "stancer"
    pm_labels = {
        "stancer": "carte bancaire",
        "online": "carte bancaire",
        "card_counter": "carte bancaire (comptoir)",
        "cash": "especes",
    }
    pm_label = pm_labels.get(pm, pm)
    pdf._font(7.5)
    pdf._color(_GREY)
    pdf.cell(0, 4, "Conditions : Payee par " + pm_label + " le " + date_str, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 4, "Categorie : Prestation de services (Art. 242 nonies A, Annexe II CGI)", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf._line_sep(dashed=True)

    # Legal
    pdf._font(6.5)
    pdf._color(_LIGHT)
    for line in [
        "Systeme auto-certifie ISCA (Loi n 2026-103 du 19/02/2026, Art. 125)",
        "Conformement a la reforme de la facturation electronique (Loi de finances 2024, Art. 91),",
        "ce document est disponible au format PDF. Version Factur-X disponible sur demande.",
    ]:
        pdf.cell(0, 3.5, line, align="C", new_x="LMARGIN", new_y="NEXT")

    buf = BytesIO()
    pdf.output(buf)
    return buf.getvalue()
