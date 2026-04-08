"""
Tool: generate_rental_website
------------------------------
Zero Human Intervention: generates a full React app AND deploys it via
GitHub Actions to GitHub Pages, returning a live accessible URL.

Flow:
  1. Build React project files from property data
  2. Create a new public GitHub repo (clawshow-rental-{slug}-{ts})
  3. Push all files in a single commit via Git Tree API (blobs → tree → commit → update ref)
  4. GitHub Actions triggers automatically: npm install → build → deploy to gh-pages
  5. Poll Actions until workflow completes (~2-3 min)
  6. Enable GitHub Pages from gh-pages branch
  7. Poll URL until live, return https://{owner}.github.io/{repo}/

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
# Constants
# ---------------------------------------------------------------------------

_GITHUB_API   = "https://api.github.com"
_DEFAULT_EMAIL = "puflorent@gmail.com"
_DEFAULT_PHONE = "+33 6 42 98 45 35"


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

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


def _log(msg: str) -> None:
    import sys
    print(f"[ClawShow] {msg}", flush=True, file=sys.stderr)


def _create_repo(repo_name: str, description: str) -> bool:
    """Create repo. Returns True if newly created, False if it already existed."""
    _log(f"Creating repo: {repo_name}")
    r = httpx.post(
        f"{_GITHUB_API}/user/repos",
        headers=_gh_headers(),
        json={"name": repo_name, "description": description, "private": False, "auto_init": True},
        timeout=20,
    )
    if r.status_code in (409, 422):
        _log(f"Repo already exists ({r.status_code}) — reusing it")
        return False
    r.raise_for_status()
    _log("Repo created OK")
    return True


def _get_branch_sha(owner: str, repo: str, branch: str = "main") -> str | None:
    """Return the current HEAD SHA of a branch, or None if it doesn't exist."""
    r = httpx.get(
        f"{_GITHUB_API}/repos/{owner}/{repo}/git/refs/heads/{branch}",
        headers=_gh_headers(),
        timeout=15,
    )
    if r.status_code == 200:
        return r.json()["object"]["sha"]
    return None


def _git_post(url: str, headers: dict, payload: dict, label: str) -> dict:
    """POST to Git API with retry on 404/409 (repo still initialising)."""
    for attempt in range(4):
        r = httpx.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code in (404, 409) and attempt < 3:
            wait = (attempt + 1) * 3
            _log(f"  {label} got {r.status_code} (attempt {attempt + 1}/3) — retry in {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"{label} failed after retries")


def _push_all(owner: str, repo: str, files: dict[str, str], message: str) -> None:
    """Push ALL files as a single commit via Git Tree API.
    Requires auto_init=True repo so main branch already exists."""
    _log(f"Pushing {len(files)} files as single commit to {owner}/{repo}")
    api = _GITHUB_API
    headers = _gh_headers()

    # 1. Get current main HEAD SHA (auto_init guarantees it exists)
    parent_sha = _get_branch_sha(owner, repo, "main")
    if not parent_sha:
        raise RuntimeError("main branch not found — repo may not have initialised yet")
    _log(f"  main HEAD: {parent_sha[:8]}")

    # 2. Create blobs for every file
    tree_items = []
    for i, (path, content) in enumerate(files.items(), 1):
        data = _git_post(
            f"{api}/repos/{owner}/{repo}/git/blobs",
            headers,
            {"content": content, "encoding": "utf-8"},
            f"blob:{path}",
        )
        tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": data["sha"]})
        _log(f"  blob [{i}/{len(files)}] {path}")

    # 3. Create tree (base_tree = current main so auto_init's README stays unless overwritten)
    data = _git_post(
        f"{api}/repos/{owner}/{repo}/git/trees",
        headers,
        {"base_tree": parent_sha, "tree": tree_items},
        "tree",
    )
    tree_sha = data["sha"]
    _log(f"  tree: {tree_sha[:8]}")

    # 4. Create commit with main as parent
    data = _git_post(
        f"{api}/repos/{owner}/{repo}/git/commits",
        headers,
        {"message": message, "tree": tree_sha, "parents": [parent_sha]},
        "commit",
    )
    commit_sha = data["sha"]
    _log(f"  commit: {commit_sha[:8]}")

    # 5. Fast-forward main ref to new commit
    r = httpx.patch(
        f"{api}/repos/{owner}/{repo}/git/refs/heads/main",
        headers=headers,
        json={"sha": commit_sha, "force": True},
        timeout=30,
    )
    r.raise_for_status()
    _log("  push complete — single commit on main")


def _wait_for_branch(owner: str, repo: str, branch: str, max_wait: int = 300, interval: int = 10) -> bool:
    """Poll until a branch exists in the repo (Actions needs to create gh-pages first)."""
    _log(f"Waiting for branch '{branch}' to appear in {owner}/{repo}")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if _get_branch_sha(owner, repo, branch) is not None:
            _log(f"Branch '{branch}' found")
            return True
        time.sleep(interval)
    _log(f"Timed out waiting for branch '{branch}'")
    return False


def _enable_pages(owner: str, repo: str, branch: str = "gh-pages") -> str:
    """Wait for branch to exist, then enable GitHub Pages from it. Returns Pages URL."""
    _log(f"Enabling Pages from branch '{branch}'")

    # Wait for the branch to exist (Actions deploys to gh-pages)
    _wait_for_branch(owner, repo, branch)

    r = httpx.post(
        f"{_GITHUB_API}/repos/{owner}/{repo}/pages",
        headers=_gh_headers(),
        json={"source": {"branch": branch, "path": "/"}},
        timeout=20,
    )
    if r.status_code == 409:
        _log("Pages already enabled — updating source branch")
        httpx.put(
            f"{_GITHUB_API}/repos/{owner}/{repo}/pages",
            headers=_gh_headers(),
            json={"source": {"branch": branch, "path": "/"}},
            timeout=20,
        )
    elif r.status_code not in (200, 201):
        _log(f"Pages enable returned {r.status_code}: {r.text[:200]}")
        r.raise_for_status()

    get_r = httpx.get(
        f"{_GITHUB_API}/repos/{owner}/{repo}/pages",
        headers=_gh_headers(),
        timeout=15,
    )
    get_r.raise_for_status()
    url = get_r.json().get("html_url", f"https://{owner}.github.io/{repo}/")
    _log(f"Pages URL: {url}")
    return url


def _wait_for_actions(owner: str, repo: str, max_wait: int = 360, interval: int = 15) -> bool:
    """Poll Actions API until the latest workflow run on main completes successfully."""
    _log("Waiting for GitHub Actions to complete (up to 6 min)...")
    time.sleep(20)  # Give Actions time to register the run
    deadline = time.time() + max_wait
    while time.time() < deadline:
        r = httpx.get(
            f"{_GITHUB_API}/repos/{owner}/{repo}/actions/runs",
            headers=_gh_headers(),
            params={"branch": "main", "per_page": 1},
            timeout=15,
        )
        if r.status_code == 200:
            runs = r.json().get("workflow_runs", [])
            if runs:
                run = runs[0]
                _log(f"Actions run status: {run['status']} / {run.get('conclusion', '-')}")
                if run["status"] == "completed":
                    return run["conclusion"] == "success"
        time.sleep(interval)
    _log("Actions timed out")
    return False


def _wait_for_pages_url(url: str, max_wait: int = 90, interval: int = 8) -> bool:
    """Poll until the Pages URL responds with 200."""
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
# React project file generators
# ---------------------------------------------------------------------------

def _build_types_ts(
    site_name: str,
    contact_email: str,
    contact_phone: str,
    properties: list[dict],
    currency: str,
    language: str,
    payment_url: str = "",
) -> str:
    def _to_camel(props: dict) -> dict:
        key_map = {
            "price_per_night": "pricePerNight",
            "max_guests":      "maxGuests",
            "booking_url":     "bookingUrl",
            "image_url":       "imageUrl",
            "contact_email":   "contactEmail",
            "contact_phone":   "contactPhone",
        }
        return {key_map.get(k, k): v for k, v in props.items()}

    data = {
        "siteName":     site_name,
        "contactEmail": contact_email or _DEFAULT_EMAIL,
        "contactPhone": contact_phone or _DEFAULT_PHONE,
        "currency":     currency,
        "language":     language,
        "paymentUrl":   payment_url,
        "properties":   [_to_camel(p) for p in properties],
    }
    json_str = json.dumps(data, indent=2, ensure_ascii=False)
    return f"export const SITE_DATA = {json_str};\n\nexport type SiteData = typeof SITE_DATA;\n"


def _build_app_tsx() -> str:
    return """import { useState, useEffect } from 'react'
import { Mail, Phone, ChevronLeft, ChevronRight, Users } from 'lucide-react'
import { SITE_DATA } from './types'

const ACCENT = '#FF385C'

const ROOM_GALLERY: Record<string, number[]> = {
  'Living Room': [10, 11, 12],
  'Bedroom':     [13, 14, 15],
  'Kitchen':     [16, 17, 18],
  'Bathroom':    [19, 20, 21],
  'Bath':        [22, 23, 24],
  'Balcony':     [25, 26, 27],
  'Street View': [28, 29, 30],
}

const HERO_SEEDS = [1, 31, 32, 33]

function picsum(seed: number, w = 800, h = 600): string {
  return `https://picsum.photos/${w}/${h}?random=${seed}`
}

function HeroCarousel() {
  const { siteName, currency, properties } = SITE_DATA
  const prop = properties[0] as any
  const [idx, setIdx] = useState(0)
  const n = HERO_SEEDS.length

  useEffect(() => {
    const t = setInterval(() => setIdx(i => (i + 1) % n), 5000)
    return () => clearInterval(t)
  }, [n])

  return (
    <div className="relative overflow-hidden" style={{ height: '520px', marginTop: '64px' }}>
      {HERO_SEEDS.map((seed, i) => (
        <img
          key={seed}
          src={picsum(seed, 1600, 900)}
          alt={siteName}
          className="absolute inset-0 w-full h-full object-cover transition-opacity duration-700"
          style={{ opacity: i === idx ? 1 : 0 }}
        />
      ))}
      <div className="absolute inset-0 bg-gradient-to-t from-black/60 via-black/20 to-transparent" />
      <div className="absolute inset-0 flex flex-col items-center justify-center text-white text-center px-4">
        <h1 className="text-4xl sm:text-5xl font-bold drop-shadow-lg mb-2">{prop.name || siteName}</h1>
        {prop.pricePerNight && (
          <p className="text-2xl font-semibold mt-2 drop-shadow" style={{ color: ACCENT }}>
            {currency}{prop.pricePerNight}<span className="text-lg font-normal text-white/80">/night</span>
          </p>
        )}
        {prop.location && <p className="text-white/75 mt-2">{prop.location}</p>}
      </div>
      <button
        onClick={() => setIdx(i => (i - 1 + n) % n)}
        className="absolute left-4 top-1/2 -translate-y-1/2 bg-white/80 hover:bg-white rounded-full p-2 transition-colors"
      >
        <ChevronLeft className="w-5 h-5 text-gray-800" />
      </button>
      <button
        onClick={() => setIdx(i => (i + 1) % n)}
        className="absolute right-4 top-1/2 -translate-y-1/2 bg-white/80 hover:bg-white rounded-full p-2 transition-colors"
      >
        <ChevronRight className="w-5 h-5 text-gray-800" />
      </button>
      <div className="absolute bottom-4 left-1/2 -translate-x-1/2 flex gap-2">
        {HERO_SEEDS.map((_, i) => (
          <button
            key={i}
            onClick={() => setIdx(i)}
            className="w-2 h-2 rounded-full transition-colors"
            style={{ background: i === idx ? ACCENT : 'rgba(255,255,255,0.6)' }}
          />
        ))}
      </div>
    </div>
  )
}

function GallerySection() {
  const rooms = Object.keys(ROOM_GALLERY)
  const [activeRoom, setActiveRoom] = useState(rooms[0])
  const [imgIdx, setImgIdx] = useState(0)

  useEffect(() => { setImgIdx(0) }, [activeRoom])

  const seeds = ROOM_GALLERY[activeRoom]
  const n = seeds.length

  return (
    <section className="max-w-5xl mx-auto px-4 sm:px-6 py-10">
      <h2 className="text-xl font-semibold text-gray-900 mb-4">Photo Gallery</h2>
      <div className="flex gap-2 overflow-x-auto pb-2 mb-4" style={{ scrollbarWidth: 'none' }}>
        {rooms.map(room => (
          <button
            key={room}
            onClick={() => setActiveRoom(room)}
            className="px-4 py-2 text-sm font-medium rounded-full whitespace-nowrap transition-colors border"
            style={activeRoom === room
              ? { background: ACCENT, color: '#fff', borderColor: ACCENT }
              : { background: '#fff', color: '#6b7280', borderColor: '#e5e7eb' }
            }
          >
            {room}
          </button>
        ))}
      </div>
      <div className="relative overflow-hidden rounded-xl" style={{ height: '400px' }}>
        {seeds.map((seed, i) => (
          <img
            key={seed}
            src={picsum(seed)}
            alt={`${activeRoom} ${i + 1}`}
            className="absolute inset-0 w-full h-full object-cover transition-opacity duration-500"
            style={{ opacity: i === imgIdx ? 1 : 0 }}
          />
        ))}
        <button
          onClick={() => setImgIdx(i => (i - 1 + n) % n)}
          className="absolute left-3 top-1/2 -translate-y-1/2 bg-white/80 hover:bg-white rounded-full p-2 transition-colors"
        >
          <ChevronLeft className="w-5 h-5 text-gray-800" />
        </button>
        <button
          onClick={() => setImgIdx(i => (i + 1) % n)}
          className="absolute right-3 top-1/2 -translate-y-1/2 bg-white/80 hover:bg-white rounded-full p-2 transition-colors"
        >
          <ChevronRight className="w-5 h-5 text-gray-800" />
        </button>
        <div className="absolute bottom-3 left-1/2 -translate-x-1/2 flex gap-2">
          {seeds.map((_, i) => (
            <button
              key={i}
              onClick={() => setImgIdx(i)}
              className="w-2 h-2 rounded-full transition-colors"
              style={{ background: i === imgIdx ? ACCENT : 'rgba(255,255,255,0.6)' }}
            />
          ))}
        </div>
      </div>
      <p className="text-center text-sm text-gray-500 mt-3 font-medium">{activeRoom}</p>
    </section>
  )
}

function BookingCard() {
  const { currency, properties, paymentUrl } = SITE_DATA
  const prop = properties[0] as any
  return (
    <div className="border border-gray-200 rounded-2xl shadow-lg p-6 sticky top-20">
      {prop.pricePerNight && (
        <div className="text-2xl font-bold text-gray-900 mb-1">
          <span style={{ color: ACCENT }}>{currency}{prop.pricePerNight}</span>
          <span className="text-base font-normal text-gray-500">/night</span>
        </div>
      )}
      <div className="flex items-center gap-4 text-sm text-gray-500 mb-5">
        {prop.bedrooms && <span>{prop.bedrooms} bed{prop.bedrooms !== 1 ? 's' : ''}</span>}
        {prop.maxGuests && (
          <span className="flex items-center gap-1">
            <Users className="w-4 h-4" /> up to {prop.maxGuests}
          </span>
        )}
      </div>
      {paymentUrl ? (
        <a
          href={paymentUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="block w-full text-center text-white font-semibold py-3 px-6 rounded-xl transition-colors mb-3"
          style={{ background: '#16a34a' }}
          onMouseOver={(e) => (e.currentTarget.style.background = '#15803d')}
          onMouseOut={(e) => (e.currentTarget.style.background = '#16a34a')}
        >
          Pay Now {prop.pricePerNight ? `— ${currency}${prop.pricePerNight}` : ''}
        </a>
      ) : (
        <a
          href="#contact"
          className="block w-full text-center text-white font-semibold py-3 px-6 rounded-xl transition-opacity hover:opacity-90 mb-3"
          style={{ background: ACCENT }}
        >
          Book Now
        </a>
      )}
      <p className="text-center text-gray-400 text-xs">
        {paymentUrl ? 'Secure payment via Stripe' : 'No charge until you confirm'}
      </p>
    </div>
  )
}

export default function App() {
  const { siteName, contactEmail, contactPhone, currency, properties } = SITE_DATA
  const prop = properties[0] as any

  return (
    <div className="min-h-screen bg-white text-gray-800">

      {/* Nav */}
      <nav className="fixed top-0 left-0 right-0 z-50 bg-white/95 backdrop-blur-sm border-b border-gray-100">
        <div className="max-w-5xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
          <span className="font-semibold text-gray-900 text-lg">{siteName}</span>
          <a href={`mailto:${contactEmail}`} className="text-sm text-gray-500 hover:text-gray-900 transition-colors hidden sm:block">
            {contactEmail}
          </a>
          <a
            href="#contact"
            className="text-sm text-white font-medium px-4 py-2 rounded-lg transition-opacity hover:opacity-90"
            style={{ background: ACCENT }}
          >
            Contact
          </a>
        </div>
      </nav>

      {/* Hero */}
      <HeroCarousel />

      {/* Gallery */}
      <GallerySection />

      {/* Main 2-col */}
      <section className="max-w-5xl mx-auto px-4 sm:px-6 py-8 border-t border-gray-100">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-10">
          <div className="lg:col-span-2 space-y-8">
            <div>
              <h2 className="text-2xl font-bold text-gray-900">{prop.name || siteName}</h2>
              {prop.location && <p className="text-gray-500 text-sm mt-1">{prop.location}</p>}
            </div>
            <hr className="border-gray-100" />
            {prop.description && (
              <div>
                <h3 className="text-lg font-semibold text-gray-900 mb-3">About this space</h3>
                <p className="text-gray-600 leading-relaxed">{prop.description}</p>
              </div>
            )}
            {prop.amenities && prop.amenities.length > 0 && (
              <div>
                <h3 className="text-lg font-semibold text-gray-900 mb-3">Amenities</h3>
                <div className="flex flex-wrap gap-2">
                  {prop.amenities.map((a: string) => (
                    <span key={a} className="bg-gray-100 text-gray-700 text-sm font-medium px-3 py-1 rounded-full">{a}</span>
                  ))}
                </div>
              </div>
            )}
            {prop.location && (
              <div>
                <h3 className="text-lg font-semibold text-gray-900 mb-2">Location</h3>
                <p className="text-gray-600">{prop.location}</p>
              </div>
            )}
          </div>
          <div className="lg:col-span-1">
            <BookingCard />
          </div>
        </div>
      </section>

      {/* Additional properties */}
      {properties.length > 1 && (
        <section className="max-w-5xl mx-auto px-4 sm:px-6 py-12 border-t border-gray-100">
          <h2 className="text-2xl font-bold text-gray-900 mb-6">More Properties</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
            {(properties as any[]).slice(1).map((p: any, i: number) => (
              <div key={i} className="border border-gray-200 rounded-2xl overflow-hidden shadow-sm hover:shadow-md transition-shadow">
                <img src={`https://picsum.photos/800/600?random=${40 + i}`} alt={p.name} className="w-full h-44 object-cover" />
                <div className="p-4">
                  <h3 className="font-semibold text-gray-900 mb-1">{p.name}</h3>
                  <p className="text-gray-500 text-sm mb-2">{p.location}</p>
                  {p.pricePerNight && (
                    <span className="font-bold text-gray-900">
                      {currency}{p.pricePerNight}
                      <span className="text-gray-400 text-sm font-normal">/night</span>
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Contact */}
      <section id="contact" className="bg-gray-50 py-16 mt-8">
        <div className="max-w-5xl mx-auto px-4 sm:px-6 text-center">
          <h2 className="text-2xl font-bold text-gray-900 mb-2">Get in Touch</h2>
          <p className="text-gray-500 mb-8">Ready to book? Reach out and we'll get back to you quickly.</p>
          <div className="flex flex-col sm:flex-row gap-4 justify-center">
            <a
              href={`mailto:${contactEmail}`}
              className="inline-flex items-center gap-2 text-white font-medium px-6 py-3 rounded-xl transition-opacity hover:opacity-90"
              style={{ background: ACCENT }}
            >
              <Mail className="w-4 h-4" /> {contactEmail}
            </a>
            {contactPhone && (
              <a
                href={`tel:${contactPhone}`}
                className="inline-flex items-center gap-2 bg-gray-800 hover:bg-gray-700 text-white font-medium px-6 py-3 rounded-xl transition-colors"
              >
                <Phone className="w-4 h-4" /> {contactPhone}
              </a>
            )}
          </div>
        </div>
      </section>

      {/* Footer */}
      <footer className="bg-gray-900 text-gray-400 py-8">
        <div className="max-w-5xl mx-auto px-4 sm:px-6 flex flex-col sm:flex-row items-center justify-between gap-3 text-sm">
          <span className="text-gray-200 font-medium">{siteName}</span>
          <span>Generated by <a href="https://clawshow.ai" className="hover:text-gray-200 transition-colors">ClawShow</a></span>
        </div>
      </footer>

    </div>
  )
}
"""


def _build_main_tsx() -> str:
    return """import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
"""


def _build_index_html(site_name: str) -> str:
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{site_name}</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
"""


def _build_index_css() -> str:
    return """@tailwind base;
@tailwind components;
@tailwind utilities;
"""


def _build_package_json() -> str:
    return json.dumps({
        "name": "rental-website",
        "private": True,
        "version": "0.0.0",
        "type": "module",
        "scripts": {
            "dev":     "vite",
            "build":   "vite build",
            "preview": "vite preview",
        },
        "dependencies": {
            "lucide-react": "^0.400.0",
            "react":        "^18.3.1",
            "react-dom":    "^18.3.1",
        },
        "devDependencies": {
            "@types/react":        "^18.3.1",
            "@types/react-dom":    "^18.3.1",
            "@vitejs/plugin-react":"^4.3.1",
            "autoprefixer":        "^10.4.20",
            "postcss":             "^8.4.49",
            "tailwindcss":         "^3.4.16",
            "typescript":          "^5.6.2",
            "vite":                "^5.4.10",
        },
    }, indent=2)


def _build_vite_config(repo_name: str) -> str:
    return f"""import {{ defineConfig }} from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({{
  plugins: [react()],
  base: '/{repo_name}/',
}})
"""


def _build_tsconfig() -> str:
    return json.dumps({
        "compilerOptions": {
            "target":                   "ES2020",
            "useDefineForClassFields":  True,
            "lib":                      ["ES2020", "DOM", "DOM.Iterable"],
            "module":                   "ESNext",
            "skipLibCheck":             True,
            "moduleResolution":         "bundler",
            "allowImportingTsExtensions": True,
            "resolveJsonModule":        True,
            "isolatedModules":          True,
            "noEmit":                   True,
            "jsx":                      "react-jsx",
            "strict":                   False,
        },
        "include": ["src"],
    }, indent=2)


def _build_tailwind_config() -> str:
    return """export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: { extend: {} },
  plugins: [],
}
"""


def _build_postcss_config() -> str:
    return """export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
}
"""


def _build_deploy_yml() -> str:
    return """name: Deploy to GitHub Pages

on:
  push:
    branches: [main]

permissions:
  contents: write

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: npm install
      - run: npm run build
      - uses: peaceiris/actions-gh-pages@v3
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: ./dist
"""


def _build_all_files(
    site_name: str,
    contact_email: str,
    contact_phone: str,
    properties: list[dict],
    currency: str,
    language: str,
    repo_name: str,
    custom_domain: str,
    payment_url: str = "",
) -> dict[str, str]:
    files = {
        "index.html":                    _build_index_html(site_name),
        "src/main.tsx":                  _build_main_tsx(),
        "src/App.tsx":                   _build_app_tsx(),
        "src/types.ts":                  _build_types_ts(site_name, contact_email, contact_phone, properties, currency, language, payment_url),
        "src/index.css":                 _build_index_css(),
        "package.json":                  _build_package_json(),
        "vite.config.ts":                _build_vite_config(repo_name),
        "tsconfig.json":                 _build_tsconfig(),
        "tailwind.config.js":            _build_tailwind_config(),
        "postcss.config.js":             _build_postcss_config(),
        ".github/workflows/deploy.yml":  _build_deploy_yml(),
    }
    if custom_domain:
        cname = custom_domain.replace("https://", "").replace("http://", "").rstrip("/")
        files["CNAME"] = cname
    return files


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
        payment_url: str = "",
    ) -> str:
        """
        Generate a complete rental property website with photo gallery, pricing
        table, location map, availability calendar, and booking form.
        Auto-deployed to GitHub Pages. Input: property details JSON (address,
        photos, price, amenities, rules). Output: live website URL. Ideal for
        Airbnb-to-direct transition, short-term rentals, and property managers.
        Supports custom domains and multi-property portfolios.

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
            contact_email: Owner email
            properties:    List of property dicts (name, location, description,
                           bedrooms, max_guests, price_per_night, amenities,
                           booking_url, image_url)
            contact_phone: Optional phone
            currency:      Currency symbol, default "€"
            language:      "en" or "fr", default "en"
            custom_domain: Optional custom domain (CNAME file will be added)
            payment_url:   Optional Stripe Checkout URL — changes "Book Now"
                           to a green "Pay Now" button linking to Stripe

        Returns:
            Live URL once deployed (~3 minutes), or URL + status if still building.
        """
        record_call("generate_rental_website")

        slug      = re.sub(r"[^a-z0-9]+", "-", site_name.lower()).strip("-")[:30]
        ts        = int(time.time())
        repo_name = f"clawshow-{slug}-{ts}"

        _log(f"Starting deployment for: {site_name} → {repo_name}")
        owner = _get_github_login()
        _log(f"GitHub owner: {owner}")

        # 1. Create repo (silently reuses if exists, auto_init creates main branch)
        is_new = _create_repo(repo_name, description=f"Rental website: {site_name} — generated by ClawShow")
        if is_new:
            _log("Waiting 5s for repo initialisation...")
            time.sleep(5)

        # 2. Build all React project files and push via Contents API
        files = _build_all_files(
            site_name, contact_email, contact_phone,
            properties, currency, language, repo_name, custom_domain,
            payment_url,
        )
        _push_all(owner, repo_name, files, f"Add rental website: {site_name}")

        # 3. Wait for GitHub Actions to build and deploy to gh-pages branch
        _wait_for_actions(owner, repo_name)

        # 4. Wait for gh-pages branch, then enable Pages
        pages_url = _enable_pages(owner, repo_name, branch="gh-pages")
        if not pages_url.endswith("/"):
            pages_url += "/"

        # 5. Wait for Pages URL to go live
        _log(f"Polling Pages URL: {pages_url}")
        live = _wait_for_pages_url(pages_url)

        repo_url = f"https://github.com/{owner}/{repo_name}"
        _log(f"Done. Live={live} URL={pages_url}")

        if live:
            result = pages_url
        else:
            result = (
                f"{pages_url}\n\n"
                f"(Site is still building — should be live within 60 seconds.\n"
                f"Repo: {repo_url})"
            )

        if custom_domain:
            result += (
                f"\n\ncustom_domain: {custom_domain}"
                f"\nDNS: add CNAME record → {owner}.github.io"
            )

        return result
