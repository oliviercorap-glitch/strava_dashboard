"""
Strava Dashboard — Application Flask
=====================================
Dashboard personnel temps réel : FTP, CTL/ATL/TSB, recommandations d'entraînement
Protégé par mot de passe simple

Variables d'environnement (Railway) :
  STRAVA_CLIENT_ID
  STRAVA_CLIENT_SECRET
  STRAVA_REFRESH_TOKEN
  ANTHROPIC_API_KEY
  DASHBOARD_PASSWORD     — mot de passe d'accès au dashboard
  SECRET_KEY             — clé secrète Flask (chaîne aléatoire)
"""

import os, json, time, hashlib
from collections import defaultdict
from datetime import datetime, timedelta, date
from functools import wraps
from pathlib import Path

import requests
import anthropic
from flask import (Flask, render_template, request, session,
                   redirect, url_for, jsonify)
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "changeme-random-string-here")

FTP              = 201
JOURS_HISTORIQUE = 90
OBJECTIFS = {
    "ftp_actuelle":  201,
    "ftp_cible":     230,
    "km_sem_cible":  120,
    "semi_date":     "décembre 2026",
}

# Cache simple en mémoire (évite de re-appeler Strava/Claude à chaque refresh)
_cache = {}
CACHE_TTL = 1800  # 30 minutes


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        pwd = request.form.get("password", "")
        expected = os.environ.get("DASHBOARD_PASSWORD", "strava2026")
        if pwd == expected:
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Mot de passe incorrect."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Strava OAuth2
# ---------------------------------------------------------------------------

def rafraichir_token():
    cached = _cache.get("strava_token")
    if cached and cached["expires_at"] > time.time() + 300:
        return cached["access_token"]

    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id":     os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "grant_type":    "refresh_token",
        "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
    }, timeout=10)
    resp.raise_for_status()
    t = resp.json()
    _cache["strava_token"] = t
    return t["access_token"]


def get_activites(jours=90):
    cache_key = f"activites_{jours}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached["ts"] < CACHE_TTL:
        return cached["data"]

    token = rafraichir_token()
    depuis = int((datetime.now() - timedelta(days=jours)).timestamp())
    activites, page = [], 1
    while True:
        resp = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers={"Authorization": f"Bearer {token}"},
            params={"after": depuis, "per_page": 100, "page": page},
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch: break
        activites.extend(batch)
        if len(batch) < 100: break
        page += 1
        time.sleep(0.3)

    _cache[cache_key] = {"data": activites, "ts": time.time()}
    return activites


def formater_activite(a):
    duree_h = a["moving_time"] / 3600
    return {
        "id":            a["id"],
        "nom":           a.get("name", "Sortie"),
        "type":          a.get("sport_type", a.get("type", "?")),
        "date":          a.get("start_date_local", "")[:10],
        "distance_km":   round(a["distance"] / 1000, 1),
        "duree_h":       round(duree_h, 2),
        "duree":         f"{int(duree_h)}h{int((duree_h % 1) * 60):02d}",
        "elevation_m":   int(a.get("total_elevation_gain", 0)),
        "vitesse_moy":   round(a.get("average_speed", 0) * 3.6, 1),
        "fc_moy":        a.get("average_heartrate"),
        "fc_max":        a.get("max_heartrate"),
        "puissance_moy": a.get("average_watts"),
        "suffer_score":  a.get("suffer_score") or 0,
        "calories":      a.get("calories") or 0,
    }


# ---------------------------------------------------------------------------
# CTL / ATL / TSB
# ---------------------------------------------------------------------------

def calculer_charge(activites):
    charge_par_jour = defaultdict(float)
    for a in activites:
        if a["puissance_moy"] and a["duree_h"]:
            if_val = a["puissance_moy"] / FTP
            tss = (if_val ** 2) * a["duree_h"] * 100
        elif a["suffer_score"]:
            tss = a["suffer_score"] * 1.5
        else:
            tss = a["duree_h"] * 40
        charge_par_jour[a["date"]] += tss

    today = date.today()
    ctl, atl = 0.0, 0.0
    k_ctl, k_atl = 2 / 43, 2 / 8
    historique = []
    for i in range(JOURS_HISTORIQUE - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        charge = charge_par_jour.get(d, 0.0)
        ctl = ctl + k_ctl * (charge - ctl)
        atl = atl + k_atl * (charge - atl)
        historique.append({
            "date": d, "charge": round(charge, 1),
            "CTL": round(ctl, 1), "ATL": round(atl, 1),
            "TSB": round(ctl - atl, 1)
        })

    dernier = historique[-1]
    tsb = dernier["TSB"]
    if tsb > 10:    interp = "Forme fraîche — bon moment pour un effort intense"
    elif tsb > -10: interp = "Équilibre — entraînement modéré conseillé"
    elif tsb > -30: interp = "Fatigue accumulée — surveiller la récupération"
    else:           interp = "Surcharge — récupération prioritaire"

    return {
        "CTL": dernier["CTL"], "ATL": dernier["ATL"], "TSB": dernier["TSB"],
        "interpretation": interp, "historique": historique[-30:]
    }


# ---------------------------------------------------------------------------
# Analyse Claude
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Tu es un coach cyclisme et running expert, conseiller personnel d'Olivier.

PROFIL :
- FTP actuelle : 201W (Garmin) → cible 230W
- Volume vélo cible : 120 km/semaine
- Objectif running : semi-marathon avant Noël 2026

Ton analyse est courte, directe, en français, orientée action.
Réponds toujours en markdown avec des titres ## et des listes - pour structurer.
"""

def analyser_avec_claude(activites, charge, km_semaine):
    cache_key = f"analyse_{hashlib.md5(str(len(activites)).encode()).hexdigest()[:8]}_{charge['CTL']}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached["ts"] < CACHE_TTL:
        return cached["data"]

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # 5 dernières activités
    recentes = activites[:5]
    recentes_txt = "\n".join(
        f"- {a['date']} | {a['type']} | {a['distance_km']} km | {a['elevation_m']}m D+ | "
        f"{a['duree']} | FC:{a['fc_moy'] or '?'} | {a['puissance_moy'] or '?'}W"
        for a in recentes
    )

    prompt = (
        f"Date : {datetime.now().strftime('%d %B %Y')}\n\n"
        f"MÉTRIQUES\nCTL={charge['CTL']} ATL={charge['ATL']} TSB={charge['TSB']}\n"
        f"Volume semaine en cours : {km_semaine} km / 120 km objectif\n\n"
        f"5 DERNIÈRES ACTIVITÉS\n{recentes_txt}\n\n"
        "Analyse en 3 sections courtes :\n"
        "## Bilan récent\n(2-3 phrases sur les dernières sorties)\n\n"
        "## Trajectoire objectifs\n"
        "- FTP 230W : suis-je sur la bonne voie ?\n"
        "- Semi-marathon décembre : base aérobie suffisante ?\n\n"
        "## Recommandation pour les 7 prochains jours\n"
        "(3 séances concrètes avec type, durée, intensité)"
    )

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    result = msg.content[0].text
    _cache[cache_key] = {"data": result, "ts": time.time()}
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    return render_template("dashboard.html")


@app.route("/api/data")
@login_required
def api_data():
    """Endpoint JSON appelé par le dashboard en AJAX."""
    try:
        activites_brutes = get_activites(jours=JOURS_HISTORIQUE)
        # Tri explicite par date decroissante (plus recent en premier)
        activites_brutes = sorted(
            activites_brutes,
            key=lambda x: x.get('start_date_local', ''),
            reverse=True
        )
        activites = [formater_activite(a) for a in activites_brutes]

        charge = calculer_charge(activites)

        # Volume semaine en cours
        today_d = date.today()
        debut_sem = today_d - timedelta(days=today_d.weekday())
        km_semaine = round(sum(
            a["distance_km"] for a in activites
            if a["date"] >= debut_sem.isoformat()
        ), 1)

        # Stats par type
        par_type = defaultdict(lambda: {"nb": 0, "km": 0.0, "h": 0.0})
        for a in activites:
            par_type[a["type"]]["nb"] += 1
            par_type[a["type"]]["km"] += a["distance_km"]
            par_type[a["type"]]["h"]  += a["duree_h"]
        stats = {k: {
            "nb": v["nb"],
            "km": round(v["km"], 1),
            "h":  round(v["h"], 1)
        } for k, v in sorted(par_type.items(), key=lambda x: -x[1]["nb"])}

        # Analyse Claude
        analyse_md = analyser_avec_claude(activites, charge, km_semaine)

        # 10 dernières activités pour l'affichage
        recentes = activites[:10]

        return jsonify({
            "ok": True,
            "charge": charge,
            "km_semaine": km_semaine,
            "reste_semaine": round(max(0, 120 - km_semaine), 1),
            "nb_activites": len(activites),
            "stats": stats,
            "recentes": recentes,
            "analyse": analyse_md,
            "updated_at": datetime.now().strftime("%H:%M"),
            "objectifs": OBJECTIFS,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/refresh")
@login_required
def api_refresh():
    """Vide le cache pour forcer un rechargement complet."""
    _cache.clear()
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
