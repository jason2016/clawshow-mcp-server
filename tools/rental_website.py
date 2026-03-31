"""
Tool: generate_rental_website
------------------------------
Input:  property details (name, contact, list of units)
Output: single self-contained HTML file as a string

Design goals:
  - Zero external runtime dependencies (stdlib + string templates)
  - Tailwind CDN in output HTML (no build step for end-user)
  - Supports en / fr
  - Mobile-first, single-file, directly deployable to any static host
"""

from __future__ import annotations

from typing import Callable


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _amenity_badge(label: str) -> str:
    return f'<span class="inline-block bg-stone-100 text-stone-600 text-xs px-2 py-1 rounded-full mr-1 mb-1">{label}</span>'


def _property_card(p: dict, currency: str) -> str:
    name = p.get("name", "Property")
    location = p.get("location", "")
    description = p.get("description", "")
    bedrooms = p.get("bedrooms", "")
    max_guests = p.get("max_guests", "")
    price = p.get("price_per_night", "")
    booking_url = p.get("booking_url", "#")
    image_url = p.get("image_url", "")
    amenities = p.get("amenities", [])

    image_html = (
        f'<img src="{image_url}" alt="{name}" class="w-full h-48 object-cover rounded-t-xl">'
        if image_url
        else '<div class="w-full h-48 bg-stone-200 rounded-t-xl flex items-center justify-center text-stone-400 text-sm">No photo</div>'
    )

    amenities_html = "".join(_amenity_badge(a) for a in amenities)

    price_html = f'{currency}{price}<span class="text-sm font-normal text-stone-400">/night</span>' if price else ""
    beds_guests = ""
    if bedrooms and max_guests:
        beds_guests = f'<p class="text-stone-500 text-sm mb-1">{bedrooms} bed · up to {max_guests} guests</p>'
    elif max_guests:
        beds_guests = f'<p class="text-stone-500 text-sm mb-1">Up to {max_guests} guests</p>'

    return f"""
    <div class="bg-white rounded-xl shadow-sm border border-stone-100 overflow-hidden flex flex-col">
      {image_html}
      <div class="p-5 flex flex-col flex-1">
        <h3 class="text-lg font-semibold text-stone-800 mb-1">{name}</h3>
        <p class="text-stone-500 text-sm mb-2">{location}</p>
        {beds_guests}
        <p class="text-stone-600 text-sm mb-3 flex-1">{description}</p>
        <div class="mb-3">{amenities_html}</div>
        <div class="flex items-center justify-between mt-auto">
          <span class="text-xl font-bold text-stone-800">{price_html}</span>
          <a href="{booking_url}" target="_blank" rel="noopener"
             class="bg-stone-800 hover:bg-stone-700 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors">
            Book
          </a>
        </div>
      </div>
    </div>
    """


def _build_html(
    site_name: str,
    contact_email: str,
    contact_phone: str,
    properties: list[dict],
    currency: str,
    language: str,
) -> str:
    cards_html = "\n".join(_property_card(p, currency) for p in properties)
    count = len(properties)
    subtitle = (
        f"{count} {'appartement' if language == 'fr' else 'apartment'}{'s' if count != 1 else ''} "
        f"{'disponibles' if language == 'fr' else 'available'} à Paris"
    )
    contact_label = "Contact" if language == "en" else "Contact"
    phone_html = (
        f'<a href="tel:{contact_phone}" class="text-stone-400 hover:text-white transition-colors">{contact_phone}</a>'
        if contact_phone
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="{language}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{site_name}</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body {{ font-family: 'Inter', ui-sans-serif, system-ui, sans-serif; }}
  </style>
</head>
<body class="bg-stone-50 min-h-screen">

  <!-- Header -->
  <header class="bg-white border-b border-stone-100 sticky top-0 z-10">
    <div class="max-w-5xl mx-auto px-4 sm:px-6 py-4 flex items-center justify-between">
      <span class="text-lg font-semibold text-stone-800">{site_name}</span>
      <a href="mailto:{contact_email}"
         class="text-sm text-stone-500 hover:text-stone-800 transition-colors">{contact_email}</a>
    </div>
  </header>

  <!-- Hero -->
  <section class="max-w-5xl mx-auto px-4 sm:px-6 pt-12 pb-8">
    <h1 class="text-3xl sm:text-4xl font-bold text-stone-800 mb-3">{site_name}</h1>
    <p class="text-stone-500 text-lg">{subtitle}</p>
  </section>

  <!-- Properties grid -->
  <main class="max-w-5xl mx-auto px-4 sm:px-6 pb-16">
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
      {cards_html}
    </div>
  </main>

  <!-- Footer -->
  <footer class="bg-stone-800 text-stone-400 py-10 px-4">
    <div class="max-w-5xl mx-auto flex flex-col sm:flex-row items-center justify-between gap-4">
      <span class="text-stone-300 font-medium">{site_name}</span>
      <div class="flex flex-col sm:flex-row gap-3 text-sm items-center">
        <a href="mailto:{contact_email}" class="hover:text-white transition-colors">{contact_email}</a>
        {phone_html}
      </div>
      <p class="text-xs text-stone-500">Generated by ClawShow · mcp.clawshow.ai</p>
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
    ) -> str:
        """
        Generate a complete, self-contained static HTML rental website.

        Returns a single HTML string — ready to save as index.html and deploy
        to any static host (GitHub Pages, Netlify, Vercel, etc.).

        Args:
            site_name:     Display name for the site, e.g. "Paris Short Stay"
            contact_email: Owner contact email shown in header and footer
            properties:    List of property objects. Each object should have:
                             - name (str): e.g. "Montmartre Studio"
                             - location (str): e.g. "18th arr., Paris"
                             - description (str)
                             - bedrooms (int, optional)
                             - max_guests (int, optional)
                             - price_per_night (number, optional)
                             - amenities (list of str, optional)
                             - booking_url (str, optional): Airbnb or direct link
                             - image_url (str, optional)
            contact_phone: Optional phone number shown in footer
            currency:      Currency symbol prefix, default "€"
            language:      "en" or "fr", affects UI labels, default "en"

        Returns:
            Complete HTML document as a string.
        """
        record_call("generate_rental_website")
        return _build_html(
            site_name=site_name,
            contact_email=contact_email,
            contact_phone=contact_phone,
            properties=properties,
            currency=currency,
            language=language,
        )
