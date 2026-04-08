"""
Tool: generate_business_page
------------------------------
Universal page engine. Generates and deploys a professional single-page
website to GitHub Pages. Supports rental, enrollment, product, service,
and restaurant page types. Built-in GEO optimization (llms.txt, JSON-LD).

Deploys as static HTML (Tailwind CDN) — no build step, live in ~60s.

Env required:
  GITHUB_TOKEN — personal access token with repo + pages scopes
"""

from __future__ import annotations

import os
import re
import json
import time
from typing import Callable

import httpx

# ---------------------------------------------------------------------------
# Constants & i18n
# ---------------------------------------------------------------------------

_GITHUB_API    = "https://api.github.com"
_DEFAULT_EMAIL = "puflorent@gmail.com"
_DEFAULT_PHONE = "+33 6 42 98 45 35"
_ACCENT        = "#FF385C"

_I18N = {
    "en": {"book": "Book Now", "pay": "Pay Now", "contact": "Contact", "enroll": "Enroll Now",
           "buy": "Buy Now", "reserve": "Reserve", "menu": "Menu", "about": "About",
           "amenities": "Amenities", "location": "Location", "schedule": "Schedule",
           "requirements": "Requirements", "features": "Features", "shipping": "Shipping",
           "hours": "Hours", "get_in_touch": "Get in Touch", "powered_by": "Powered by"},
    "fr": {"book": "Réserver", "pay": "Payer maintenant", "contact": "Contacter", "enroll": "S'inscrire",
           "buy": "Acheter", "reserve": "Réserver", "menu": "Menu", "about": "À propos",
           "amenities": "Équipements", "location": "Adresse", "schedule": "Horaires",
           "requirements": "Prérequis", "features": "Caractéristiques", "shipping": "Livraison",
           "hours": "Horaires", "get_in_touch": "Nous contacter", "powered_by": "Propulsé par"},
    "zh": {"book": "立即预订", "pay": "立即支付", "contact": "联系我们", "enroll": "立即报名",
           "buy": "立即购买", "reserve": "预约", "menu": "菜单", "about": "简介",
           "amenities": "设施", "location": "地址", "schedule": "时间安排",
           "requirements": "要求", "features": "特点", "shipping": "配送",
           "hours": "营业时间", "get_in_touch": "联系我们", "powered_by": "技术支持"},
}

def _t(key: str, lang: str) -> str:
    return _I18N.get(lang, _I18N["en"]).get(key, _I18N["en"].get(key, key))


# ---------------------------------------------------------------------------
# GitHub API helpers (shared with rental_website.py)
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    import sys
    print(f"[ClawShow:page] {msg}", flush=True, file=sys.stderr)

def _gh_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set")
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

def _get_github_login() -> str:
    r = httpx.get(f"{_GITHUB_API}/user", headers=_gh_headers(), timeout=15)
    r.raise_for_status()
    return r.json()["login"]

def _create_repo(name: str, desc: str) -> bool:
    r = httpx.post(f"{_GITHUB_API}/user/repos", headers=_gh_headers(),
                   json={"name": name, "description": desc, "private": False, "auto_init": True}, timeout=20)
    if r.status_code in (409, 422):
        _log(f"Repo exists ({r.status_code}) — reusing")
        return False
    r.raise_for_status()
    _log("Repo created")
    return True

def _get_branch_sha(owner: str, repo: str, branch: str = "main") -> str | None:
    r = httpx.get(f"{_GITHUB_API}/repos/{owner}/{repo}/git/refs/heads/{branch}", headers=_gh_headers(), timeout=15)
    return r.json()["object"]["sha"] if r.status_code == 200 else None

def _git_post(url: str, headers: dict, payload: dict, label: str) -> dict:
    for attempt in range(4):
        r = httpx.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code in (404, 409) and attempt < 3:
            time.sleep((attempt + 1) * 3)
            _log(f"  {label} {r.status_code} retry {attempt+1}/3")
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"{label} failed")

def _push_all(owner: str, repo: str, files: dict[str, str], message: str) -> None:
    _log(f"Pushing {len(files)} files to {owner}/{repo}")
    api, headers = _GITHUB_API, _gh_headers()
    parent = _get_branch_sha(owner, repo, "main")
    if not parent:
        raise RuntimeError("main branch not found")
    items = []
    for i, (path, content) in enumerate(files.items(), 1):
        d = _git_post(f"{api}/repos/{owner}/{repo}/git/blobs", headers, {"content": content, "encoding": "utf-8"}, f"blob:{path}")
        items.append({"path": path, "mode": "100644", "type": "blob", "sha": d["sha"]})
        _log(f"  [{i}/{len(files)}] {path}")
    d = _git_post(f"{api}/repos/{owner}/{repo}/git/trees", headers, {"base_tree": parent, "tree": items}, "tree")
    d = _git_post(f"{api}/repos/{owner}/{repo}/git/commits", headers, {"message": message, "tree": d["sha"], "parents": [parent]}, "commit")
    httpx.patch(f"{api}/repos/{owner}/{repo}/git/refs/heads/main", headers=headers, json={"sha": d["sha"], "force": True}, timeout=30).raise_for_status()
    _log("Push done")

def _enable_pages(owner: str, repo: str) -> str:
    _log("Enabling Pages from main")
    r = httpx.post(f"{_GITHUB_API}/repos/{owner}/{repo}/pages", headers=_gh_headers(),
                   json={"source": {"branch": "main", "path": "/"}}, timeout=20)
    if r.status_code == 409:
        httpx.put(f"{_GITHUB_API}/repos/{owner}/{repo}/pages", headers=_gh_headers(),
                  json={"source": {"branch": "main", "path": "/"}}, timeout=20)
    gr = httpx.get(f"{_GITHUB_API}/repos/{owner}/{repo}/pages", headers=_gh_headers(), timeout=15)
    gr.raise_for_status()
    return gr.json().get("html_url", f"https://{owner}.github.io/{repo}/")

def _wait_live(url: str, max_wait: int = 90) -> bool:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=10, follow_redirects=True).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(8)
    return False


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _head(title: str, lang: str, schema_json: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{title}</title>
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>body{{font-family:'Inter',sans-serif}}</style>
<script type="application/ld+json">{schema_json}</script>
</head>
<body class="bg-white text-gray-800 min-h-screen">"""

def _nav(name: str, email: str, cta_label: str, cta_href: str) -> str:
    return f"""
<nav class="fixed top-0 left-0 right-0 z-50 bg-white/95 backdrop-blur-sm border-b border-gray-100">
  <div class="max-w-5xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
    <span class="font-semibold text-gray-900 text-lg">{name}</span>
    <a href="mailto:{email}" class="text-sm text-gray-500 hover:text-gray-900 hidden sm:block">{email}</a>
    <a href="{cta_href}" class="text-sm text-white font-medium px-4 py-2 rounded-lg" style="background:{_ACCENT}">{cta_label}</a>
  </div>
</nav>"""

def _hero(title: str, subtitle: str, img_seed: int = 1) -> str:
    return f"""
<div class="relative mt-16 overflow-hidden" style="height:420px">
  <img src="https://picsum.photos/1600/900?random={img_seed}" alt="{title}" class="w-full h-full object-cover">
  <div class="absolute inset-0 bg-gradient-to-t from-black/60 via-black/20 to-transparent"></div>
  <div class="absolute inset-0 flex flex-col items-center justify-center text-white text-center px-4">
    <h1 class="text-4xl sm:text-5xl font-bold drop-shadow-lg mb-3">{title}</h1>
    <p class="text-lg text-white/80 max-w-xl">{subtitle}</p>
  </div>
</div>"""

def _pay_button(payment_url: str, label: str, price_label: str) -> str:
    if not payment_url:
        return ""
    txt = f"{label} — {price_label}" if price_label else label
    return f"""
<a href="{payment_url}" target="_blank" rel="noopener"
   class="inline-block text-white font-semibold py-3 px-8 rounded-xl transition-colors mb-4"
   style="background:#16a34a"
   onmouseover="this.style.background='#15803d'" onmouseout="this.style.background='#16a34a'">{txt}</a>"""

def _contact_section(email: str, phone: str, lang: str) -> str:
    phone_html = f'<a href="tel:{phone}" class="inline-flex items-center gap-2 bg-gray-800 hover:bg-gray-700 text-white font-medium px-6 py-3 rounded-xl">&#128222; {phone}</a>' if phone else ""
    return f"""
<section id="contact" class="bg-gray-50 py-16 mt-8">
  <div class="max-w-5xl mx-auto px-4 sm:px-6 text-center">
    <h2 class="text-2xl font-bold text-gray-900 mb-8">{_t('get_in_touch', lang)}</h2>
    <div class="flex flex-col sm:flex-row gap-4 justify-center">
      <a href="mailto:{email}" class="inline-flex items-center gap-2 text-white font-medium px-6 py-3 rounded-xl" style="background:{_ACCENT}">&#9993; {email}</a>
      {phone_html}
    </div>
  </div>
</section>"""

def _footer(name: str) -> str:
    return f"""
<footer class="bg-gray-900 text-gray-400 py-8">
  <div class="max-w-5xl mx-auto px-4 sm:px-6 flex flex-col sm:flex-row items-center justify-between gap-3 text-sm">
    <span class="text-gray-200 font-medium">{name}</span>
    <span>Powered by <a href="https://clawshow.ai" class="hover:text-gray-200">ClawShow</a></span>
  </div>
</footer></body></html>"""

def _badges(items: list[str]) -> str:
    return " ".join(f'<span class="inline-block bg-gray-100 text-gray-700 text-sm px-3 py-1 rounded-full">{i}</span>' for i in items)

def _info_row(label: str, value: str) -> str:
    return f'<div class="flex justify-between py-2 border-b border-gray-100"><span class="text-gray-500">{label}</span><span class="font-medium text-gray-900">{value}</span></div>'


# ---------------------------------------------------------------------------
# JSON-LD Schema generators
# ---------------------------------------------------------------------------

def _schema_rental(data: dict, name: str, email: str) -> str:
    return json.dumps({"@context": "https://schema.org", "@type": "LodgingBusiness",
        "name": name, "description": data.get("description", ""),
        "address": data.get("address", ""), "email": email,
        "priceRange": f"{data.get('price_per_night', '')} EUR/night"}, ensure_ascii=False)

def _schema_enrollment(data: dict, name: str, email: str) -> str:
    return json.dumps({"@context": "https://schema.org", "@type": "EducationalOrganization",
        "name": name, "description": data.get("description", ""),
        "address": data.get("location", ""), "email": email}, ensure_ascii=False)

def _schema_product(data: dict, name: str, email: str) -> str:
    return json.dumps({"@context": "https://schema.org", "@type": "Product",
        "name": data.get("product_name", name), "description": data.get("description", ""),
        "offers": {"@type": "Offer", "price": data.get("price", ""),
                   "priceCurrency": data.get("currency", "EUR").upper()}}, ensure_ascii=False)

def _schema_service(data: dict, name: str, email: str) -> str:
    return json.dumps({"@context": "https://schema.org", "@type": "Service",
        "name": data.get("service_name", name), "description": data.get("description", ""),
        "provider": {"@type": "Organization", "name": name, "email": email}}, ensure_ascii=False)

def _schema_restaurant(data: dict, name: str, email: str) -> str:
    return json.dumps({"@context": "https://schema.org", "@type": "Restaurant",
        "name": data.get("restaurant_name", name), "servesCuisine": data.get("cuisine", ""),
        "address": data.get("address", ""), "email": email,
        "openingHours": data.get("hours", "")}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# llms.txt generator
# ---------------------------------------------------------------------------

def _build_llms_txt(page_type: str, name: str, data: dict, email: str, phone: str) -> str:
    lines = [f"# {name}", f"type: {page_type}", f"contact: {email}"]
    if phone:
        lines.append(f"phone: {phone}")
    if page_type == "rental":
        lines += [f"location: {data.get('address', '')}", f"price: {data.get('price_per_night', '')} EUR/night",
                  f"bedrooms: {data.get('bedrooms', '')}", f"guests: {data.get('guests', '')}",
                  f"description: {data.get('description', '')}"]
    elif page_type == "enrollment":
        lines += [f"program: {data.get('program', '')}", f"tuition: {data.get('tuition', '')} {data.get('currency', 'EUR')}",
                  f"dates: {data.get('start_date', '')} to {data.get('end_date', '')}",
                  f"location: {data.get('location', '')}"]
    elif page_type == "product":
        lines += [f"product: {data.get('product_name', '')}", f"price: {data.get('price', '')} {data.get('currency', 'EUR')}",
                  f"description: {data.get('description', '')}"]
    elif page_type == "service":
        lines += [f"service: {data.get('service_name', '')}", f"description: {data.get('description', '')}"]
        for pkg in data.get("packages", []):
            lines.append(f"package: {pkg.get('name', '')} — {pkg.get('price', '')} EUR")
    elif page_type == "restaurant":
        lines += [f"cuisine: {data.get('cuisine', '')}", f"address: {data.get('address', '')}",
                  f"hours: {data.get('hours', '')}"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Page type HTML generators
# ---------------------------------------------------------------------------

def _page_rental(data: dict, name: str, email: str, phone: str, lang: str, payment_url: str, price_label: str) -> str:
    d = data
    schema = _schema_rental(d, name, email)
    amenities = _badges(d.get("amenities", []))
    price = d.get("price_per_night", "")
    info = ""
    if d.get("bedrooms"):
        info += _info_row("Bedrooms", str(d["bedrooms"]))
    if d.get("bathrooms"):
        info += _info_row("Bathrooms", str(d["bathrooms"]))
    if d.get("guests"):
        info += _info_row("Guests", f"Up to {d['guests']}")
    if price:
        info += _info_row("Price", f"€{price}/night")

    return _head(f"{name} — {d.get('title', '')}", lang, schema) + \
        _nav(name, email, _t("contact", lang), "#contact") + \
        _hero(d.get("title", name), d.get("address", ""), 1) + f"""
<section class="max-w-5xl mx-auto px-4 sm:px-6 py-10">
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-10">
    <div class="lg:col-span-2 space-y-6">
      <h2 class="text-2xl font-bold text-gray-900">{d.get('title', name)}</h2>
      <p class="text-gray-600 leading-relaxed">{d.get('description', '')}</p>
      <div><h3 class="text-lg font-semibold mb-3">{_t('amenities', lang)}</h3><div class="flex flex-wrap gap-2">{amenities}</div></div>
      <div><h3 class="text-lg font-semibold mb-3">{_t('location', lang)}</h3><p class="text-gray-600">{d.get('address', '')}</p></div>
    </div>
    <div>
      <div class="border border-gray-200 rounded-2xl shadow-lg p-6 sticky top-20">
        {info}
        <div class="mt-4 text-center">
          {_pay_button(payment_url, _t('pay', lang), price_label) if payment_url else f'<a href="#contact" class="block w-full text-center text-white font-semibold py-3 rounded-xl" style="background:{_ACCENT}">{_t("book", lang)}</a>'}
        </div>
      </div>
    </div>
  </div>
</section>""" + _contact_section(email, phone, lang) + _footer(name)


def _page_enrollment(data: dict, name: str, email: str, phone: str, lang: str, payment_url: str, price_label: str) -> str:
    d = data
    schema = _schema_enrollment(d, name, email)
    reqs = _badges(d.get("requirements", []))
    tuition = d.get("tuition", "")
    currency = d.get("currency", "eur").upper()
    info = _info_row("Program", d.get("program", ""))
    if d.get("start_date"):
        info += _info_row("Start", d["start_date"])
    if d.get("end_date"):
        info += _info_row("End", d["end_date"])
    if d.get("schedule"):
        info += _info_row(_t("schedule", lang), d["schedule"])
    if d.get("spots_available"):
        info += _info_row("Spots", str(d["spots_available"]))
    if tuition:
        info += _info_row("Tuition", f"{currency} {tuition}")

    return _head(f"{name} — {d.get('program', '')}", lang, schema) + \
        _nav(name, email, _t("enroll", lang), "#enroll") + \
        _hero(d.get("school_name", name), d.get("program", ""), 5) + f"""
<section class="max-w-5xl mx-auto px-4 sm:px-6 py-10">
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-10">
    <div class="lg:col-span-2 space-y-6">
      <h2 class="text-2xl font-bold text-gray-900">{d.get('program', '')}</h2>
      <p class="text-gray-600 leading-relaxed">{d.get('description', '')}</p>
      {f'<div><h3 class="text-lg font-semibold mb-3">{_t("requirements", lang)}</h3><div class="flex flex-wrap gap-2">{reqs}</div></div>' if reqs else ''}
      {f'<div><h3 class="text-lg font-semibold mb-3">{_t("location", lang)}</h3><p class="text-gray-600">{d.get("location", "")}</p></div>' if d.get("location") else ''}
    </div>
    <div id="enroll">
      <div class="border border-gray-200 rounded-2xl shadow-lg p-6 sticky top-20">
        {info}
        <div class="mt-4 text-center">
          {_pay_button(payment_url, _t('enroll', lang), price_label or f'{currency} {tuition}') if payment_url else f'<a href="#contact" class="block w-full text-center text-white font-semibold py-3 rounded-xl" style="background:{_ACCENT}">{_t("enroll", lang)}</a>'}
        </div>
      </div>
    </div>
  </div>
</section>""" + _contact_section(email, phone, lang) + _footer(name)


def _page_product(data: dict, name: str, email: str, phone: str, lang: str, payment_url: str, price_label: str) -> str:
    d = data
    schema = _schema_product(d, name, email)
    features = _badges(d.get("features", []))
    variants = _badges(d.get("variants", []))
    price = d.get("price", "")
    currency = d.get("currency", "eur").upper()

    return _head(f"{d.get('product_name', name)}", lang, schema) + \
        _nav(name, email, _t("buy", lang), "#buy") + \
        _hero(d.get("product_name", name), d.get("description", "")[:80], 10) + f"""
<section class="max-w-5xl mx-auto px-4 sm:px-6 py-10">
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-10">
    <div class="lg:col-span-2 space-y-6">
      <h2 class="text-2xl font-bold text-gray-900">{d.get('product_name', name)}</h2>
      <p class="text-gray-600 leading-relaxed">{d.get('description', '')}</p>
      {f'<div><h3 class="text-lg font-semibold mb-3">{_t("features", lang)}</h3><div class="flex flex-wrap gap-2">{features}</div></div>' if features else ''}
      {f'<div><h3 class="text-lg font-semibold mb-3">Variants</h3><div class="flex flex-wrap gap-2">{variants}</div></div>' if variants else ''}
      {f'<div><h3 class="text-lg font-semibold mb-3">{_t("shipping", lang)}</h3><p class="text-gray-600">{d.get("shipping_info", "")}</p></div>' if d.get("shipping_info") else ''}
    </div>
    <div id="buy">
      <div class="border border-gray-200 rounded-2xl shadow-lg p-6 sticky top-20">
        <div class="text-3xl font-bold text-gray-900 mb-4"><span style="color:{_ACCENT}">{currency} {price}</span></div>
        <div class="text-center">
          {_pay_button(payment_url, _t('buy', lang), price_label or f'{currency} {price}') if payment_url else f'<a href="#contact" class="block w-full text-center text-white font-semibold py-3 rounded-xl" style="background:{_ACCENT}">{_t("buy", lang)}</a>'}
        </div>
      </div>
    </div>
  </div>
</section>""" + _contact_section(email, phone, lang) + _footer(name)


def _page_service(data: dict, name: str, email: str, phone: str, lang: str, payment_url: str, price_label: str) -> str:
    d = data
    schema = _schema_service(d, name, email)
    packages = d.get("packages", [])
    pkg_html = ""
    for i, pkg in enumerate(packages):
        includes = "".join(f'<li class="text-gray-600 text-sm py-1">{item}</li>' for item in pkg.get("includes", []))
        highlight = "border-2" if i == len(packages) - 1 else "border"
        pkg_html += f"""
<div class="{highlight} border-gray-200 rounded-2xl p-6 flex flex-col">
  <h3 class="text-xl font-bold text-gray-900 mb-1">{pkg.get('name', '')}</h3>
  <div class="text-2xl font-bold mb-4" style="color:{_ACCENT}">€{pkg.get('price', '')}</div>
  <ul class="space-y-1 flex-1">{includes}</ul>
  <div class="mt-4">{_pay_button(payment_url, _t('buy', lang), f"€{pkg.get('price', '')}") if payment_url else f'<a href="#contact" class="block text-center text-white font-semibold py-2 rounded-xl text-sm" style="background:{_ACCENT}">{_t("contact", lang)}</a>'}</div>
</div>"""

    return _head(f"{d.get('service_name', name)}", lang, schema) + \
        _nav(name, email, _t("contact", lang), "#contact") + \
        _hero(d.get("service_name", name), d.get("description", "")[:80], 15) + f"""
<section class="max-w-5xl mx-auto px-4 sm:px-6 py-10">
  <h2 class="text-2xl font-bold text-gray-900 mb-2">{d.get('service_name', name)}</h2>
  <p class="text-gray-600 leading-relaxed mb-8">{d.get('description', '')}</p>
  <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-{min(len(packages), 3)} gap-6">
    {pkg_html}
  </div>
</section>""" + _contact_section(email, phone, lang) + _footer(name)


def _page_restaurant(data: dict, name: str, email: str, phone: str, lang: str, payment_url: str, price_label: str) -> str:
    d = data
    schema = _schema_restaurant(d, name, email)
    rname = d.get("restaurant_name", name)

    cats_html = ""
    for cat in d.get("menu_categories", []):
        items_html = ""
        for item in cat.get("items", []):
            items_html += f"""
<div class="flex justify-between items-start py-3 border-b border-gray-100">
  <div><span class="font-medium text-gray-900">{item.get('name', '')}</span>
  <p class="text-sm text-gray-500">{item.get('description', '')}</p></div>
  <span class="font-semibold text-gray-900 ml-4 whitespace-nowrap">€{item.get('price', '')}</span>
</div>"""
        cats_html += f"""
<div class="mb-8">
  <h3 class="text-xl font-bold text-gray-900 mb-4 pb-2 border-b-2" style="border-color:{_ACCENT}">{cat.get('name', '')}</h3>
  {items_html}
</div>"""

    reserve_btn = ""
    if d.get("reservation_url"):
        reserve_btn = f'<a href="{d["reservation_url"]}" target="_blank" class="inline-block text-white font-semibold py-3 px-8 rounded-xl mb-4" style="background:{_ACCENT}">{_t("reserve", lang)}</a>'

    return _head(rname, lang, schema) + \
        _nav(rname, email, _t("reserve", lang), d.get("reservation_url", "#contact")) + \
        _hero(rname, f'{d.get("cuisine", "")} — {d.get("address", "")}', 20) + f"""
<section class="max-w-5xl mx-auto px-4 sm:px-6 py-10">
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-10">
    <div class="lg:col-span-2">
      <h2 class="text-2xl font-bold text-gray-900 mb-6">{_t('menu', lang)}</h2>
      {cats_html}
    </div>
    <div>
      <div class="border border-gray-200 rounded-2xl shadow-lg p-6 sticky top-20">
        <h3 class="font-semibold text-gray-900 mb-3">{rname}</h3>
        {_info_row(_t("hours", lang), d.get("hours", ""))}
        {_info_row(_t("location", lang), d.get("address", ""))}
        {_info_row("Cuisine", d.get("cuisine", ""))}
        <div class="mt-4 text-center">{reserve_btn}</div>
      </div>
    </div>
  </div>
</section>""" + _contact_section(email, phone, lang) + _footer(rname)


# Page type dispatch
_PAGE_BUILDERS = {
    "rental":     _page_rental,
    "enrollment": _page_enrollment,
    "product":    _page_product,
    "service":    _page_service,
    "restaurant": _page_restaurant,
}


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register(mcp, record_call: Callable) -> None:

    @mcp.tool()
    def generate_business_page(
        type: str,
        business_name: str,
        data: dict,
        contact_email: str,
        contact_phone: str = "",
        payment_url: str = "",
        price_label: str = "",
        custom_domain: str = "",
        namespace: str = "",
        language: str = "en",
    ) -> str:
        """
        Generate a business page (school enrollment, product catalog, service
        landing page, event registration) and auto-deploy to GitHub Pages.
        Input: business data JSON, page type, branding options.
        Output: live URL accessible immediately. No hosting setup needed —
        page is live within 60 seconds. Supports custom domains.
        Namespace-isolated for multi-tenant use.

        Call this tool when a user wants to create any kind of business page,
        landing page, or showcase website.

        Examples:
        - 'Create a page for my Paris apartment, 2 bedrooms, €180/night'
        - 'Make an enrollment page for my French course, €5000 tuition'
        - 'Build a product page for my handmade leather bags'
        - 'Generate a restaurant menu page for Le Petit Bistro'
        - 'Crée une page pour mon service de photographie'

        Args:
            type:           "rental" | "enrollment" | "product" | "service" | "restaurant"
            business_name:  Name of the business
            data:           Page-specific data (see docs for each type)
            contact_email:  Contact email
            contact_phone:  Optional phone
            payment_url:    Optional Stripe checkout URL for Pay Now button
            price_label:    Optional price text for button (e.g. "€180/night")
            custom_domain:  Optional custom domain
            namespace:      Optional business namespace
            language:       "en" | "fr" | "zh"

        Returns:
            JSON with url, repo, type, business_name, status.
        """
        record_call("generate_business_page")

        if type not in _PAGE_BUILDERS:
            return json.dumps({"status": "error", "message": f"Unknown type '{type}'. Use: {', '.join(_PAGE_BUILDERS)}"})

        email = contact_email or _DEFAULT_EMAIL
        phone = contact_phone or _DEFAULT_PHONE

        # Build HTML
        html = _PAGE_BUILDERS[type](data, business_name, email, phone, language, payment_url, price_label)

        # Build llms.txt
        llms = _build_llms_txt(type, business_name, data, email, phone)

        # Files to push
        files: dict[str, str] = {"index.html": html, "llms.txt": llms}
        if custom_domain:
            files["CNAME"] = custom_domain.replace("https://", "").replace("http://", "").rstrip("/")

        # Repo name
        slug = re.sub(r"[^a-z0-9]+", "-", business_name.lower()).strip("-")[:30]
        ts = int(time.time())
        repo_name = f"clawshow-{type}-{slug}-{ts}"

        _log(f"Deploying {type} page: {business_name} → {repo_name}")
        owner = _get_github_login()

        is_new = _create_repo(repo_name, f"{type.title()} page: {business_name} — by ClawShow")
        if is_new:
            _log("Waiting 5s for repo init...")
            time.sleep(5)

        _push_all(owner, repo_name, files, f"Add {type} page: {business_name}")

        # Static HTML — deploy from main directly, no Actions needed
        pages_url = _enable_pages(owner, repo_name)
        if not pages_url.endswith("/"):
            pages_url += "/"

        _log(f"Polling: {pages_url}")
        live = _wait_live(pages_url)

        result: dict = {
            "url": pages_url,
            "repo": repo_name,
            "type": type,
            "business_name": business_name,
            "status": "deployed" if live else "deploying",
        }
        if not live:
            result["note"] = f"Site deploying — live within 60s. Repo: https://github.com/{owner}/{repo_name}"
        if custom_domain:
            result["custom_domain_instructions"] = f"Add DNS CNAME: {custom_domain} → {owner}.github.io"

        return json.dumps(result, indent=2, ensure_ascii=False)
