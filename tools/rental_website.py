"""
Tool: generate_rental_website
------------------------------
Zero Human Intervention: generates HTML AND deploys it to GitHub Pages,
returning a live accessible URL. No manual steps required.

Flow:
  1. Build HTML from property data (Airbnb-style, Picsum Photos images)
  2. Create a new public GitHub repo (clawshow-rental-{slug}-{ts})
  3. Push index.html (and optional CNAME) via GitHub Contents API
  4. Enable GitHub Pages (source: main, root)
  5. Poll until live, then return URL + custom domain info

Env required:
  GITHUB_TOKEN — personal access token with repo + pages scopes
"""

from __future__ import annotations

import os
import re
import base64
import time
from typing import Callable

import httpx


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

_GITHUB_API = "https://api.github.com"


def _gh_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN environment variable is not set.")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_github_login() -> str:
    r = httpx.get(f"{_GITHUB_API}/user", headers=_gh_headers(), timeout=15)
    r.raise_for_status()
    return r.json()["login"]


def _create_repo(repo_name: str, description: str) -> None:
    payload = {
        "name": repo_name,
        "description": description,
        "private": False,
        "auto_init": True,   # creates main branch with initial commit
        "default_branch": "main",
    }
    r = httpx.post(
        f"{_GITHUB_API}/user/repos",
        headers=_gh_headers(),
        json=payload,
        timeout=20,
    )
    if r.status_code == 422:
        raise RuntimeError(f"Repo '{repo_name}' already exists or name is invalid: {r.json().get('message')}")
    r.raise_for_status()


def _push_file(owner: str, repo: str, filename: str, content: str, commit_msg: str) -> None:
    """Create or update a file in a GitHub repo via the Contents API."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

    # Check if file exists (to get its SHA for updates)
    existing_sha = None
    check = httpx.get(
        f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{filename}",
        headers=_gh_headers(),
        timeout=15,
    )
    if check.status_code == 200:
        existing_sha = check.json().get("sha")

    payload: dict = {"message": commit_msg, "content": encoded, "branch": "main"}
    if existing_sha:
        payload["sha"] = existing_sha

    r = httpx.put(
        f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{filename}",
        headers=_gh_headers(),
        json=payload,
        timeout=20,
    )
    r.raise_for_status()


def _enable_pages(owner: str, repo: str) -> str:
    """Enable GitHub Pages and return the expected Pages URL."""
    payload = {"source": {"branch": "main", "path": "/"}}
    r = httpx.post(
        f"{_GITHUB_API}/repos/{owner}/{repo}/pages",
        headers=_gh_headers(),
        json=payload,
        timeout=20,
    )
    if r.status_code == 409:
        # Pages already enabled — that's fine, fetch the existing URL
        get_r = httpx.get(
            f"{_GITHUB_API}/repos/{owner}/{repo}/pages",
            headers=_gh_headers(),
            timeout=15,
        )
        get_r.raise_for_status()
        return get_r.json().get("html_url", f"https://{owner}.github.io/{repo}/")
    r.raise_for_status()
    return r.json().get("html_url", f"https://{owner}.github.io/{repo}/")


def _wait_for_pages(url: str) -> bool:
    """Wait 5s then poll up to 12 times (every 5s) until the Pages URL returns 200."""
    time.sleep(5)
    for _ in range(12):
        try:
            resp = httpx.get(url, timeout=10, follow_redirects=True)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


# ---------------------------------------------------------------------------
# HTML builder — Airbnb-style
# ---------------------------------------------------------------------------

_DEFAULT_EMAIL = "puflorent@gmail.com"
_DEFAULT_PHONE = "+33 6 42 98 45 35"
_ACCENT = "#FF385C"


def _extract_city(location: str) -> str:
    """Extract city from 'Street, City' or return 'Paris' as fallback."""
    if not location:
        return "Paris"
    parts = [p.strip() for p in location.split(",") if p.strip()]
    return parts[-1] if parts else "Paris"


_PICSUM = {
    "hero":        "https://picsum.photos/1600/900?random=1",
    "living-room": "https://picsum.photos/800/600?random=2",
    "bedroom":     "https://picsum.photos/800/600?random=3",
    "kitchen":     "https://picsum.photos/800/600?random=4",
    "bathroom":    "https://picsum.photos/800/600?random=5",
    "bath":        "https://picsum.photos/800/600?random=6",
    "balcony":     "https://picsum.photos/800/600?random=7",
    "street":      "https://picsum.photos/800/600?random=8",
    "default":     "https://picsum.photos/800/600?random=9",
}


def _amenity_tag(label: str) -> str:
    return (
        f'<span class="inline-flex items-center bg-gray-100 text-gray-700 '
        f'text-sm font-medium px-3 py-1 rounded-full">{label}</span>'
    )


def _extra_property_card(p: dict, currency: str) -> str:
    """Compact card for properties beyond the first one."""
    name     = p.get("name", "Property")
    location = p.get("location", "")
    price    = p.get("price_per_night", "")
    img_url  = p.get("image_url", "")
    city     = _extract_city(location).replace(" ", "+")
    img_src  = img_url if img_url else _PICSUM["default"]

    price_html = (
        f'<span class="font-bold text-gray-900">{currency}{price}</span>'
        f'<span class="text-gray-400 text-sm">/night</span>'
        if price else ""
    )
    return (
        f'<div class="border border-gray-200 rounded-2xl overflow-hidden shadow-sm hover:shadow-md transition-shadow">'
        f'<img src="{img_src}" alt="{name}" class="w-full h-44 object-cover">'
        f'<div class="p-4">'
        f'<h3 class="font-semibold text-gray-900 mb-1">{name}</h3>'
        f'<p class="text-gray-500 text-sm mb-2">{location}</p>'
        f'<div class="flex items-center gap-1">{price_html}</div>'
        f'</div></div>'
    )


def _build_html(
    site_name: str,
    contact_email: str,
    contact_phone: str,
    properties: list[dict],
    currency: str,
    language: str,
) -> str:
    p = properties[0] if properties else {}

    city_raw = _extract_city(p.get("location", ""))
    city = city_raw.replace(" ", "+")

    # Contact (property field > tool arg > hardcoded default)
    email = p.get("contact_email") or contact_email or _DEFAULT_EMAIL
    phone = p.get("contact_phone") or contact_phone or _DEFAULT_PHONE

    # Primary property fields
    prop_name     = p.get("name", site_name)
    prop_location = p.get("location", "")
    prop_desc     = p.get("description", "")
    prop_bedrooms = p.get("bedrooms", "")
    prop_guests   = p.get("max_guests", "")
    prop_price    = p.get("price_per_night", "")
    prop_amenities = p.get("amenities", [])

    # Details line (bedrooms · guests)
    detail_parts = []
    if prop_bedrooms:
        detail_parts.append(f"{prop_bedrooms} bedroom{'s' if str(prop_bedrooms) != '1' else ''}")
    if prop_guests:
        detail_parts.append(f"Up to {prop_guests} guests")
    details_line = " · ".join(detail_parts)

    # Price display
    price_html_hero = (
        f'<p class="text-2xl font-semibold mt-2 drop-shadow" style="color:{_ACCENT}">'
        f'{currency}{prop_price}<span class="text-lg font-normal text-white/80">/night</span></p>'
    ) if prop_price else ""

    price_html_card = (
        f'<div class="text-2xl font-bold text-gray-900 mb-1">'
        f'<span style="color:{_ACCENT}">{currency}{prop_price}</span>'
        f'<span class="text-base font-normal text-gray-500">/night</span></div>'
    ) if prop_price else ""

    details_html_card = (
        f'<p class="text-gray-500 text-sm mb-5">{details_line}</p>'
    ) if details_line else ""

    details_html_left = (
        f'<p class="text-gray-500 text-sm mt-1">{details_line}</p>'
    ) if details_line else ""

    # Amenities
    amenities_section = ""
    if prop_amenities:
        tags = "".join(_amenity_tag(a) for a in prop_amenities)
        amenities_section = (
            f'<div>'
            f'<h3 class="text-lg font-semibold text-gray-900 mb-3">Amenities</h3>'
            f'<div class="flex flex-wrap gap-2">{tags}</div>'
            f'</div>'
        )

    # Description section
    desc_section = ""
    if prop_desc:
        desc_section = (
            f'<div>'
            f'<h3 class="text-lg font-semibold text-gray-900 mb-3">About this space</h3>'
            f'<p class="text-gray-600 leading-relaxed">{prop_desc}</p>'
            f'</div>'
        )

    # Location section
    location_section = ""
    if prop_location:
        location_section = (
            f'<div>'
            f'<h3 class="text-lg font-semibold text-gray-900 mb-2">Location</h3>'
            f'<p class="text-gray-600">{prop_location}</p>'
            f'</div>'
        )

    # Gallery — tab carousel
    gallery_items = [
        ("Living Room", _PICSUM["living-room"]),
        ("Bedroom",     _PICSUM["bedroom"]),
        ("Kitchen",     _PICSUM["kitchen"]),
        ("Bathroom",    _PICSUM["bathroom"]),
        ("Bath",        _PICSUM["bath"]),
        ("Balcony",     _PICSUM["balcony"]),
        ("Street View", _PICSUM["street"]),
    ]

    tab_buttons = "\n".join(
        f'<button onclick="showTab({i})" id="tab-{i}" '
        f'class="carousel-tab px-4 py-2 text-sm font-medium rounded-full whitespace-nowrap '
        f'transition-colors border" '
        f'style="{"background:{_ACCENT};color:#fff;border-color:{_ACCENT}" if i == 0 else "background:#fff;color:#6b7280;border-color:#e5e7eb"}">'
        f'{label}</button>'
        for i, (label, _) in enumerate(gallery_items)
    )

    tab_panels = "\n".join(
        f'<div id="panel-{i}" class="carousel-panel" style="display:{"block" if i == 0 else "none"}">'
        f'<img src="{url}" alt="{label}" loading="lazy" '
        f'class="w-full object-cover rounded-xl" style="height:400px;object-fit:cover">'
        f'<p class="text-center text-sm text-gray-500 mt-3 font-medium">{label}</p>'
        f'</div>'
        for i, (label, url) in enumerate(gallery_items)
    )

    gallery_js = f"""
    <script>
      var _accent = '{_ACCENT}';
      var _total = {len(gallery_items)};
      function showTab(idx) {{
        for (var i = 0; i < _total; i++) {{
          var panel = document.getElementById('panel-' + i);
          var tab   = document.getElementById('tab-'   + i);
          if (i === idx) {{
            panel.style.display = 'block';
            tab.style.background    = _accent;
            tab.style.color         = '#fff';
            tab.style.borderColor   = _accent;
          }} else {{
            panel.style.display = 'none';
            tab.style.background    = '#fff';
            tab.style.color         = '#6b7280';
            tab.style.borderColor   = '#e5e7eb';
          }}
        }}
      }}
    </script>
    """

    gallery_html = (
        f'<div class="flex gap-2 overflow-x-auto pb-2 mb-4" '
        f'style="scrollbar-width:none;-ms-overflow-style:none">'
        f'{tab_buttons}</div>'
        f'{tab_panels}'
        f'{gallery_js}'
    )

    # Extra properties section
    extra_section = ""
    if len(properties) > 1:
        extra_cards_html = "\n".join(
            _extra_property_card(prop, currency) for prop in properties[1:]
        )
        extra_section = (
            f'<section class="max-w-5xl mx-auto px-4 sm:px-6 py-12 border-t border-gray-100">'
            f'<h2 class="text-2xl font-bold text-gray-900 mb-6">More Properties</h2>'
            f'<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">'
            f'{extra_cards_html}'
            f'</div></section>'
        )

    # Phone contact button
    phone_btn = (
        f'<a href="tel:{phone}" '
        f'class="inline-flex items-center gap-2 bg-gray-800 hover:bg-gray-700 '
        f'text-white font-medium px-6 py-3 rounded-xl transition-colors">'
        f'&#128222; {phone}</a>'
    ) if phone else ""

    hero_img = _PICSUM["hero"]

    return f"""<!DOCTYPE html>
<html lang="{language}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{site_name}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <style>body {{ font-family: 'Inter', sans-serif; }}</style>
</head>
<body class="bg-white text-gray-800">

  <!-- Navigation -->
  <nav class="fixed top-0 left-0 right-0 z-50 bg-white/95 backdrop-blur-sm border-b border-gray-100">
    <div class="max-w-5xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
      <span class="font-semibold text-gray-900 text-lg">{site_name}</span>
      <a href="mailto:{email}"
         class="text-sm text-gray-500 hover:text-gray-900 transition-colors hidden sm:block">{email}</a>
      <a href="#contact"
         class="text-sm text-white font-medium px-4 py-2 rounded-lg transition-opacity hover:opacity-90"
         style="background:{_ACCENT}">Contact</a>
    </div>
  </nav>

  <!-- Hero -->
  <div class="relative mt-16 overflow-hidden" style="height:500px">
    <img src="{hero_img}" alt="{site_name}" class="w-full h-full object-cover">
    <div class="absolute inset-0 bg-gradient-to-t from-black/60 via-black/30 to-transparent"></div>
    <div class="absolute inset-0 flex flex-col items-center justify-center text-white text-center px-4">
      <h1 class="text-4xl sm:text-5xl font-bold drop-shadow-lg mb-2">{prop_name}</h1>
      {price_html_hero}
      <p class="text-white/75 mt-2 text-base">{prop_location}</p>
    </div>
  </div>

  <!-- Photo Gallery Carousel -->
  <section class="max-w-5xl mx-auto px-4 sm:px-6 py-10">
    <h2 class="text-xl font-semibold text-gray-900 mb-4">Photo Gallery</h2>
    {gallery_html}
  </section>

  <!-- Main Content: two-column -->
  <section class="max-w-5xl mx-auto px-4 sm:px-6 py-8 border-t border-gray-100">
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-10">

      <!-- Left: description, amenities, location -->
      <div class="lg:col-span-2 space-y-8">
        <div>
          <h2 class="text-2xl font-bold text-gray-900">{prop_name}</h2>
          {details_html_left}
        </div>
        <hr class="border-gray-100">
        {desc_section}
        {amenities_section}
        {location_section}
      </div>

      <!-- Right: booking card -->
      <div class="lg:col-span-1">
        <div class="border border-gray-200 rounded-2xl shadow-lg p-6 sticky top-20">
          {price_html_card}
          {details_html_card}
          <a href="#contact"
             class="block w-full text-center text-white font-semibold py-3 px-6 rounded-xl
                    transition-opacity hover:opacity-90 mb-3"
             style="background:{_ACCENT}">
            Book Now
          </a>
          <p class="text-center text-gray-400 text-xs">No charge until you confirm</p>
        </div>
      </div>

    </div>
  </section>

  {extra_section}

  <!-- Contact -->
  <section id="contact" class="bg-gray-50 py-16 mt-8">
    <div class="max-w-5xl mx-auto px-4 sm:px-6 text-center">
      <h2 class="text-2xl font-bold text-gray-900 mb-2">Get in Touch</h2>
      <p class="text-gray-500 mb-8">Ready to book? Reach out and we'll get back to you quickly.</p>
      <div class="flex flex-col sm:flex-row gap-4 justify-center">
        <a href="mailto:{email}"
           class="inline-flex items-center gap-2 text-white font-medium px-6 py-3 rounded-xl
                  transition-opacity hover:opacity-90"
           style="background:{_ACCENT}">
          &#9993; {email}
        </a>
        {phone_btn}
      </div>
    </div>
  </section>

  <!-- Footer -->
  <footer class="bg-gray-900 text-gray-400 py-8">
    <div class="max-w-5xl mx-auto px-4 sm:px-6 flex flex-col sm:flex-row
                items-center justify-between gap-3 text-sm">
      <span class="text-gray-200 font-medium">{site_name}</span>
      <span>Generated by <a href="https://mcp.clawshow.ai" class="hover:text-gray-200 transition-colors">ClawShow</a></span>
    </div>
  </footer>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register(mcp, record_call: Callable) -> None:

    @mcp.tool()
    def generate_rental_website(
        site_name: str,
        contact_email: str,
        properties: list[dict],
        contact_phone: str = "",
        currency: str = "€",
        language: str = "en",
        custom_domain: str = "",
    ) -> str:
        """
        Generates and deploys a professional rental property website.
        Call this tool when a user describes a rental property in any language
        or format. Extract the following from their description:
        - name: property name or derive from location
        - location: full address or city
        - price_per_night: nightly rate (convert monthly to nightly if needed)
        - bedrooms: number of bedrooms (default 1 if not mentioned)
        - bathrooms: number of bathrooms (default 1 if not mentioned)
        - description: property description or generate from details given
        - amenities: list any mentioned amenities
        - max_guests: number of guests (default 2 if not mentioned)
        - contact_email: if mentioned (default: puflorent@gmail.com)
        - contact_phone: if mentioned (default: +33 6 42 98 45 35)
        - custom_domain: if mentioned

        Examples of natural language that should trigger this tool:
        - 'Create a website for my 2-bedroom apartment in Lyon, 90€/night'
        - 'Je veux un site pour mon studio à Bordeaux, 65€ la nuit'
        - 'Make a rental page for Villa Rose in Nice, sleeps 6, pool, 250€'

        Args:
            site_name:     Display name, e.g. "Paris Short Stay"
            contact_email: Owner email shown in nav and contact section
            properties:    List of property objects. Each may include:
                             - name (str)
                             - location (str)  — city extracted for images
                             - description (str)
                             - bedrooms (int, optional)
                             - bathrooms (int, optional)
                             - max_guests (int, optional)
                             - price_per_night (number, optional)
                             - amenities (list[str], optional)
                             - booking_url (str, optional)
                             - image_url (str, optional)
                             - contact_email (str, optional — overrides arg)
                             - contact_phone (str, optional — overrides arg)
            contact_phone: Optional phone for contact section
            currency:      Currency symbol, default "€"
            language:      "en" or "fr", default "en"
            custom_domain: Optional custom domain e.g. "www.parishortstay.com"
                           A CNAME file will be pushed to the repo automatically.

        Returns:
            Live URL. If custom_domain is provided, also returns CNAME instructions.
        """
        record_call("generate_rental_website")

        # 1. Build HTML
        html = _build_html(
            site_name=site_name,
            contact_email=contact_email,
            contact_phone=contact_phone,
            properties=properties,
            currency=currency,
            language=language,
        )

        # 2. Derive unique repo name
        slug = re.sub(r"[^a-z0-9]+", "-", site_name.lower()).strip("-")[:30]
        ts = int(time.time())
        repo_name = f"clawshow-{slug}-{ts}"

        # 3. Get GitHub login
        owner = _get_github_login()

        # 4. Create repo (public, auto-init creates main branch)
        _create_repo(repo_name, description=f"Rental website: {site_name} — generated by ClawShow")

        # 5. Push index.html
        _push_file(
            owner=owner,
            repo=repo_name,
            filename="index.html",
            content=html,
            commit_msg=f"Add rental website: {site_name}",
        )

        # 5b. Push CNAME file if custom domain provided
        if custom_domain:
            cname_value = (
                custom_domain
                .replace("https://", "")
                .replace("http://", "")
                .rstrip("/")
            )
            _push_file(
                owner=owner,
                repo=repo_name,
                filename="CNAME",
                content=cname_value,
                commit_msg="Add custom domain CNAME",
            )

        # 6. Enable GitHub Pages
        pages_url = _enable_pages(owner, repo_name)
        if not pages_url.endswith("/"):
            pages_url += "/"

        # 7. Wait for Pages to go live (up to 65s)
        live = _wait_for_pages(pages_url)

        if live:
            result = pages_url
        else:
            result = f"Site is deploying, will be live within 60 seconds: {pages_url}"

        # 8. Append custom domain instructions if provided
        if custom_domain:
            cname_target = f"{owner}.github.io"
            result += (
                f"\n\nsite_url: {pages_url}"
                f"\ncustom_domain: {custom_domain}"
                f"\ncustom_domain_cname: {cname_target}"
                f"\ncustom_domain_instructions: Add CNAME record in your DNS: {custom_domain} → {cname_target}"
            )

        return result
