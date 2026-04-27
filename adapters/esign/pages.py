"""eSign user-facing HTML pages (signup / login / verify / dashboard / new).

All pages are minimal, mobile-first, match ClawShow dark brand.
Served by server.py routes added in patch_server_esign_pages.py.
"""
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from adapters.esign.auth import get_session_user, COOKIE_NAME

_BASE_STYLE = """
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0f172a;color:#f1f5f9;min-height:100vh;display:flex;
     flex-direction:column;align-items:center;justify-content:center;padding:24px}
.card{background:#1e293b;border:1px solid #334155;border-radius:14px;
      padding:36px 40px;width:100%;max-width:420px;box-shadow:0 8px 32px rgba(0,0,0,.4)}
.logo{text-align:center;margin-bottom:28px}
.logo-x{font-size:26px;font-weight:700;color:#0F62FE}
.logo-text{font-size:16px;font-weight:600;color:#e2e8f0;margin-left:4px}
h1{font-size:22px;font-weight:700;color:#f1f5f9;margin-bottom:6px}
.sub{color:#94a3b8;font-size:14px;margin-bottom:28px}
label{display:block;font-size:13px;color:#94a3b8;margin-bottom:6px;margin-top:16px}
input{width:100%;padding:11px 14px;background:#0f172a;border:1px solid #334155;
      border-radius:8px;color:#f1f5f9;font-size:15px;outline:none;transition:.2s}
input:focus{border-color:#0F62FE;box-shadow:0 0 0 3px rgba(15,98,254,.2)}
.btn{display:block;width:100%;padding:13px;background:#0F62FE;color:#fff;
     border:none;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;
     margin-top:24px;transition:.2s}
.btn:hover{background:#0050e6}
.btn.green{background:#10b981}.btn.green:hover{background:#059669}
.btn-secondary{background:transparent;border:1px solid #334155;color:#94a3b8}
.btn-secondary:hover{border-color:#64748b;color:#e2e8f0}
.err{color:#f87171;font-size:13px;margin-top:12px;display:none}
.info{color:#7dd3fc;font-size:13px;margin-top:12px;display:none}
.link{color:#60a5fa;text-decoration:none;font-size:13px}
.link:hover{text-decoration:underline}
.center{text-align:center}
.mt{margin-top:18px}
.badge{display:inline-block;background:#1e3a5f;color:#7dd3fc;
       border:1px solid #1d4ed8;border-radius:6px;font-size:12px;
       padding:3px 10px;margin-bottom:16px}
.quota-bar-wrap{background:#0f172a;border-radius:6px;height:8px;margin:8px 0 4px;overflow:hidden}
.quota-bar{height:8px;background:#10b981;border-radius:6px;transition:width .4s}
.quota-bar.warn{background:#f59e0b}
.quota-bar.empty{background:#ef4444}
.doc-row{display:flex;align-items:center;justify-content:space-between;
         padding:12px 0;border-bottom:1px solid #1e293b}
.doc-row:last-child{border-bottom:none}
.doc-status{font-size:12px;padding:2px 8px;border-radius:4px;font-weight:600}
.s-pending{background:#1e3a5f;color:#7dd3fc}
.s-completed{background:#064e3b;color:#6ee7b7}
.s-declined{background:#450a0a;color:#fca5a5}
</style>
"""


def _page(title: str, body: str) -> str:
    return (
        f"<!DOCTYPE html><html><head><title>{title} — ClawShow eSign</title>"
        f"{_BASE_STYLE}</head><body>{body}</body></html>"
    )


# ── GET /signup ───────────────────────────────────────────────────────────────

async def page_signup(request: Request) -> HTMLResponse:
    body = """
<div class="card">
  <div class="logo">
    <span class="logo-x">X</span>
    <span class="logo-text">ClawShow eSign</span>
  </div>
  <div class="badge">3 signatures gratuites &bull; Sans carte bancaire</div>
  <h1>Créer mon compte</h1>
  <p class="sub">Commencez à signer en 2 minutes</p>

  <label>Adresse e-mail *</label>
  <input id="email" type="email" placeholder="vous@email.com" autocomplete="email">

  <label>Votre nom (optionnel)</label>
  <input id="name" type="text" placeholder="Jean Dupont">

  <button class="btn green" onclick="doSignup()">Créer mon compte &rarr;</button>
  <div id="err" class="err"></div>
  <div id="info" class="info"></div>

  <p class="center mt">
    Déjà inscrit ? <a href="/login" class="link">Se connecter</a>
  </p>
  <p class="center mt" style="color:#475569;font-size:11px">
    En créant un compte vous acceptez les
    <a href="https://clawshow.ai/cgu" class="link" target="_blank">CGU</a> et la
    <a href="https://clawshow.ai/confidentialite" class="link" target="_blank">Politique de confidentialité</a>.
  </p>
</div>
<script>
async function doSignup(){
  const email=document.getElementById('email').value.trim();
  const name=document.getElementById('name').value.trim();
  const err=document.getElementById('err');
  const info=document.getElementById('info');
  err.style.display='none'; info.style.display='none';
  if(!email){err.textContent='Veuillez entrer votre e-mail.';err.style.display='block';return;}
  try{
    const r=await fetch('/esign/auth/signup',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email,display_name:name||undefined})
    });
    const d=await r.json();
    if(r.ok){
      window.location.href=d.redirect_url||'/login/verify?email='+encodeURIComponent(email)+'&new=1';
    } else {
      err.textContent=d.error||'Erreur inattendue';err.style.display='block';
    }
  }catch(e){err.textContent='Erreur réseau';err.style.display='block';}
}
document.getElementById('email').addEventListener('keydown',e=>{if(e.key==='Enter')doSignup();});
</script>
"""
    return HTMLResponse(_page("Inscription", body))


# ── GET /login ────────────────────────────────────────────────────────────────

async def page_login(request: Request) -> HTMLResponse:
    body = """
<div class="card">
  <div class="logo">
    <span class="logo-x">X</span>
    <span class="logo-text">ClawShow eSign</span>
  </div>
  <h1>Se connecter</h1>
  <p class="sub">Entrez votre e-mail pour recevoir un code de connexion</p>

  <label>Adresse e-mail</label>
  <input id="email" type="email" placeholder="vous@email.com" autocomplete="email">

  <button class="btn" onclick="doSend()">Envoyer le code &rarr;</button>
  <div id="err" class="err"></div>
  <div id="info" class="info"></div>

  <p class="center mt">
    Pas encore de compte ? <a href="/signup" class="link">S'inscrire gratuitement</a>
  </p>
</div>
<script>
async function doSend(){
  const email=document.getElementById('email').value.trim();
  const err=document.getElementById('err');
  const info=document.getElementById('info');
  err.style.display='none'; info.style.display='none';
  if(!email){err.textContent='Veuillez entrer votre e-mail.';err.style.display='block';return;}
  try{
    const r=await fetch('/esign/auth/login/otp/send',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email})
    });
    const d=await r.json();
    if(r.ok){
      window.location.href='/login/verify?email='+encodeURIComponent(email);
    } else {
      if(r.status===404){
        err.innerHTML='Compte introuvable. <a href="/signup" style="color:#60a5fa">Créer un compte</a>';
      } else {
        err.textContent=d.error||'Erreur';
      }
      err.style.display='block';
    }
  }catch(e){err.textContent='Erreur réseau';err.style.display='block';}
}
document.getElementById('email').addEventListener('keydown',e=>{if(e.key==='Enter')doSend();});
</script>
"""
    return HTMLResponse(_page("Connexion", body))


# ── GET /login/verify ─────────────────────────────────────────────────────────

async def page_login_verify(request: Request) -> HTMLResponse:
    email = request.query_params.get("email", "")
    is_new = request.query_params.get("new", "") == "1"
    welcome = "Bienvenue ! Vérifiez votre e-mail." if is_new else "Vérifiez votre e-mail."
    safe_email = email.replace('"', "").replace("<", "").replace(">", "")

    body = f"""
<div class="card">
  <div class="logo">
    <span class="logo-x">X</span>
    <span class="logo-text">ClawShow eSign</span>
  </div>
  <h1>Entrez votre code</h1>
  <p class="sub">{welcome}<br>
    Un code à 6 chiffres a été envoyé à <strong>{safe_email}</strong></p>

  <input type="hidden" id="email" value="{safe_email}">

  <label>Code de vérification</label>
  <input id="otp" type="text" inputmode="numeric" maxlength="6"
         placeholder="123456" autocomplete="one-time-code"
         style="font-size:28px;letter-spacing:8px;text-align:center">

  <button class="btn" onclick="doVerify()">Vérifier &rarr;</button>
  <div id="err" class="err"></div>

  <p class="center mt">
    <a href="#" onclick="doResend();return false" class="link">Renvoyer le code</a>
    &nbsp;&bull;&nbsp;
    <a href="/login" class="link">Changer d'e-mail</a>
  </p>
</div>
<script>
const EMAIL=document.getElementById('email').value;
async function doVerify(){{
  const otp=document.getElementById('otp').value.trim();
  const err=document.getElementById('err');
  err.style.display='none';
  if(otp.length!==6){{err.textContent='Le code doit contenir 6 chiffres.';err.style.display='block';return;}}
  try{{
    const r=await fetch('/esign/auth/login/verify',{{
      method:'POST',headers:{{'Content-Type':'application/json'}},
      credentials:'include',
      body:JSON.stringify({{email:EMAIL,otp}})
    }});
    const d=await r.json();
    if(r.ok){{window.location.href='/dashboard';}}
    else{{err.textContent=d.error||'Code incorrect';err.style.display='block';}}
  }}catch(e){{err.textContent='Erreur réseau';err.style.display='block';}}
}}
async function doResend(){{
  await fetch('/esign/auth/login/otp/send',{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{email:EMAIL}})
  }});
  alert('Nouveau code envoyé !');
}}
document.getElementById('otp').addEventListener('keydown',e=>{{if(e.key==='Enter')doVerify();}});
document.getElementById('otp').focus();
</script>
"""
    return HTMLResponse(_page("Vérification", body))


# ── GET /dashboard ────────────────────────────────────────────────────────────

async def page_dashboard(request: Request) -> HTMLResponse:
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    remaining = user["free_quota_total"] - user["free_quota_used"]
    total = user["free_quota_total"]
    pct = int(remaining / total * 100) if total else 0
    bar_class = "quota-bar" + (" warn" if pct <= 33 else "")
    if remaining == 0:
        bar_class = "quota-bar empty"

    quota_msg = f"{remaining} / {total} signatures disponibles"
    if remaining == 0:
        quota_msg = "Quota gratuit épuisé"

    btn_html = (
        '<button class="btn green" onclick="location.href=\'/new\'">+ Nouvelle signature</button>'
        if remaining > 0 else
        '<div style="background:#1c0a0a;border:1px solid #7f1d1d;border-radius:8px;padding:16px;margin-top:20px">'
        '<p style="color:#fca5a5;margin-bottom:8px">Vous avez utilisé vos 3 signatures gratuites.</p>'
        '<a href="mailto:contact@clawshow.ai?subject=Upgrade%20mon%20compte%20eSign" '
        'class="link">Contacter pour passer à un plan payant &rarr;</a></div>'
    )

    name = user.get("display_name") or user["email"].split("@")[0]

    body = f"""
<div style="width:100%;max-width:640px">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px">
    <div>
      <span class="logo-x" style="font-size:20px">X</span>
      <span class="logo-text" style="font-size:14px">ClawShow eSign</span>
    </div>
    <div style="display:flex;gap:12px;align-items:center">
      <span style="color:#94a3b8;font-size:13px">{user['email']}</span>
      <button class="btn btn-secondary" style="width:auto;padding:6px 14px;margin:0;font-size:13px"
              onclick="doLogout()">Déconnexion</button>
    </div>
  </div>

  <div class="card" style="margin-bottom:16px">
    <h1 style="margin-bottom:4px">Bonjour, {name} !</h1>
    <p class="sub">{quota_msg}</p>
    <div class="quota-bar-wrap">
      <div class="{bar_class}" style="width:{pct}%"></div>
    </div>
    <p style="color:#475569;font-size:12px">{remaining} signature(s) gratuite(s) restante(s)</p>
    {btn_html}
  </div>

  <div class="card">
    <h2 style="font-size:16px;color:#e2e8f0;margin-bottom:16px">Vos signatures récentes</h2>
    <div id="docs"><p style="color:#475569">Chargement...</p></div>
  </div>
</div>

<script>
async function loadDocs(){{
  const r=await fetch('/esign/user/documents',{{credentials:'include'}});
  if(!r.ok){{document.getElementById('docs').innerHTML='<p style="color:#475569">Aucune signature.</p>';return;}}
  const d=await r.json();
  if(!d.documents||!d.documents.length){{
    document.getElementById('docs').innerHTML='<p style="color:#475569">Aucune signature pour l&apos;instant.</p>';
    return;
  }}
  const statLabel={{pending:'En attente',completed:'Signé',declined:'Refusé'}};
  const statCls={{pending:'s-pending',completed:'s-completed',declined:'s-declined'}};
  document.getElementById('docs').innerHTML=d.documents.map(doc=>
    '<div class="doc-row">'
    +'<div><div style="font-size:14px;color:#e2e8f0">'+doc.signer_name+'</div>'
    +'<div style="font-size:12px;color:#64748b">'+doc.id+' &bull; '+(doc.created_at||'').slice(0,10)+'</div></div>'
    +'<div style="display:flex;gap:8px;align-items:center">'
    +'<span class="doc-status '+(statCls[doc.status]||'s-pending')+'">'+(statLabel[doc.status]||doc.status)+'</span>'
    +(doc.status==='completed'?'<a href="/esign/'+doc.id+'/signed.pdf" target="_blank" '
      +'style="color:#60a5fa;font-size:12px">PDF</a>':'')
    +'</div></div>'
  ).join('');
}}
async function doLogout(){{
  await fetch('/esign/auth/logout',{{method:'POST',credentials:'include'}});
  window.location.href='/login';
}}
loadDocs();
</script>
"""
    return HTMLResponse(_page("Dashboard", body))


# ── GET /new ──────────────────────────────────────────────────────────────────

async def page_new_esign(request: Request) -> HTMLResponse:
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    remaining = user["free_quota_total"] - user["free_quota_used"]
    if remaining <= 0:
        return RedirectResponse("/dashboard", status_code=302)

    body = f"""
<div class="card" style="max-width:520px">
  <div class="logo">
    <span class="logo-x">X</span>
    <span class="logo-text">ClawShow eSign</span>
  </div>
  <h1>Nouvelle signature</h1>
  <p class="sub">
    <span style="color:#f59e0b">1 signature sera déduite de vos {remaining} signature(s) gratuite(s)</span>
  </p>

  <label>Document PDF *</label>
  <input id="pdf" type="file" accept="application/pdf"
         style="background:#0f172a;border:1px dashed #334155;color:#94a3b8;cursor:pointer">

  <label>Nom du signataire *</label>
  <input id="signer_name" type="text" placeholder="Jean Dupont">

  <label>E-mail du signataire *</label>
  <input id="signer_email" type="email" placeholder="signataire@email.com">

  <label>Message (optionnel)</label>
  <input id="message" type="text" placeholder="Merci de signer ce document.">

  <button class="btn" onclick="doCreate()">Envoyer pour signature &rarr;</button>
  <div id="err" class="err"></div>
  <div id="info" class="info"></div>

  <p class="center mt">
    <a href="/dashboard" class="link">&larr; Retour</a>
  </p>
</div>
<script>
async function doCreate(){{
  const err=document.getElementById('err');
  const info=document.getElementById('info');
  err.style.display='none'; info.style.display='none';
  const signer_name=document.getElementById('signer_name').value.trim();
  const signer_email=document.getElementById('signer_email').value.trim();
  const pdfFile=document.getElementById('pdf').files[0];
  const message=document.getElementById('message').value.trim();
  if(!signer_name||!signer_email){{err.textContent='Nom et e-mail du signataire requis.';err.style.display='block';return;}}
  if(!pdfFile){{err.textContent='Veuillez sélectionner un document PDF.';err.style.display='block';return;}}
  info.textContent='Envoi en cours...'; info.style.display='block';
  const fd=new FormData();
  fd.append('pdf',pdfFile);
  fd.append('signer_name',signer_name);
  fd.append('signer_email',signer_email);
  if(message)fd.append('message',message);
  try{{
    const r=await fetch('/esign/user/create',{{method:'POST',credentials:'include',body:fd}});
    const d=await r.json();
    if(r.ok){{
      info.style.display='none';
      window.location.href='/dashboard?sent=1';
    }} else {{
      info.style.display='none';
      err.textContent=d.error||'Erreur lors de la création';err.style.display='block';
    }}
  }}catch(e){{info.style.display='none';err.textContent='Erreur réseau';err.style.display='block';}}
}}
</script>
"""
    return HTMLResponse(_page("Nouvelle signature", body))
