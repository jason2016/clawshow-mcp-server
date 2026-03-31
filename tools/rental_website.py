"""
Tool: generate_rental_website
------------------------------
Zero Human Intervention: generates HTML AND deploys it to GitHub Pages,
returning a live accessible URL. No manual steps required.

Flow:
  1. Build HTML from property data
  2. Create a new public GitHub repo (clawshow-rental-{slug}-{ts})
  3. Push index.html via GitHub Contents API
  4. Enable GitHub Pages (source: main, root)
  5. Return https://{owner}.github.io/{repo}/

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


def _wait_for_pages(url: str, max_wait: int = 90, interval: int = 8) -> bool:
    """Poll until the Pages URL returns 200, or timeout."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=10, follow_redirects=True)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def _amenity_badge(label: str) -> str:
    return (
        f'<span class="inline-block bg-stone-100 text-stone-600 '
        f'text-xs px-2 py-1 rounded-full mr-1 mb-1">{label}</span>'
    )


def _property_card(p: dict, currency: str) -> str:
    name        = p.get("name", "Property")
    location    = p.get("location", "")
    description = p.get("description", "")
    bedrooms    = p.get("bedrooms", "")
    max_guests  = p.get("max_guests", "")
    price       = p.get("price_per_night", "")
    booking_url = p.get("booking_url", "#")
    image_url   = p.get("image_url", "")
    amenities   = p.get("amenities", [])

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
    phone_html = (
        f'<a href="tel:{contact_phone}" class="text-stone-400 hover:text-white transition-colors">{contact_phone}</a>'
        if contact_phone else ""
    )

    return f"""<!DOCTYPE html>
<html lang="{language}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{site_name}</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-stone-50 min-h-screen">
  <header class="bg-white border-b border-stone-100 sticky top-0 z-10">
    <div class="max-w-5xl mx-auto px-4 sm:px-6 py-4 flex items-center justify-between">
      <span class="text-lg font-semibold text-stone-800">{site_name}</span>
      <a href="mailto:{contact_email}" class="text-sm text-stone-500 hover:text-stone-800 transition-colors">{contact_email}</a>
    </div>
  </header>
  <section class="max-w-5xl mx-auto px-4 sm:px-6 pt-12 pb-8">
    <h1 class="text-3xl sm:text-4xl font-bold text-stone-800 mb-3">{site_name}</h1>
    <p class="text-stone-500 text-lg">{subtitle}</p>
  </section>
  <main class="max-w-5xl mx-auto px-4 sm:px-6 pb-16">
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
      {cards_html}
    </div>
  </main>
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
        Generate and DEPLOY a rental website. Returns a live accessible URL.

        This tool builds a complete property listing website and automatically
        deploys it to GitHub Pages — no manual steps required. The returned URL
        is ready to share with guests immediately (live within ~60 seconds).

        Args:
            site_name:     Display name, e.g. "Paris Short Stay"
            contact_email: Owner email shown in header and footer
            properties:    List of property objects. Each may include:
                             - name (str)
                             - location (str)
                             - description (str)
                             - bedrooms (int, optional)
                             - max_guests (int, optional)
                             - price_per_night (number, optional)
                             - amenities (list[str], optional)
                             - booking_url (str, optional)
                             - image_url (str, optional)
            contact_phone: Optional phone for footer
            currency:      Currency symbol, default "€"
            language:      "en" or "fr", default "en"

        Returns:
            Live URL string, e.g. "https://jason2016.github.io/clawshow-paris-short-stay-1234567/"
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

        # 6. Enable GitHub Pages
        pages_url = _enable_pages(owner, repo_name)
        if not pages_url.endswith("/"):
            pages_url += "/"

        # 7. Wait for Pages to go live (up to 90s)
        live = _wait_for_pages(pages_url)

        if live:
            return pages_url
        else:
            return (
                f"{pages_url}\n\n"
                f"(GitHub Pages is still building — check back in ~60 seconds. "
                f"Repo: https://github.com/{owner}/{repo_name})"
            )
