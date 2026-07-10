"""
Strava Dashboard — Application Flask
=====================================
Dashboard personnel temps réel : FTP, CTL/ATL/TSB, recommandations d'entrainement
Protege par mot de passe simple

Variables d'environnement (Railway) :
  STRAVA_CLIENT_ID
  STRAVA_CLIENT_SECRET
  STRAVA_REFRESH_TOKEN
  ANTHROPIC_API_KEY
  DASHBOARD_PASSWORD
  SECRET_KEY
"""

import os, json, time, hashlib
from collections import defaultdict
from datetime import datetime, timedelta, date
from functools import wraps

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
    "semi_date":     "decembre 2026",
}

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
    if cached and cached.get("expires_at", 0) > time.time() + 300:
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
    """Recupere toutes les activites des N derniers jours, triees du plus recent au plus ancien."""
    cache_key = "activites_{}".format(jours)
    cached = _cache.get(cache_key)
    if cached and time.time() - cached["ts"] < CACHE_TTL:
        return cached["data"]

    token = rafraichir_token()
    depuis = int((datetime.now() - timedelta(days=jours)).timestamp())
    activites, page = [], 1

    while True:
        resp = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers={"Authorization": "Bearer " + token},
            params={"after": depuis, "per_page": 100, "page": page},
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        activites.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        time.sleep(0.3)

    # Tri explicite par date decroissante — plus recent en premier
    activites = sorted(
        activites,
        key=lambda x: x.get("start_date_local", ""),
        reverse=True
    )

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
        "duree":         "{}h{:02d}".format(int(duree_h), int((duree_h % 1) * 60)),
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
            "date": d,
            "charge": round(charge, 1),
            "CTL": round(ctl, 1),
            "ATL": round(atl, 1),
            "TSB": round(ctl - atl, 1),
        })

    dernier = historique[-1]
    tsb = dernier["TSB"]
    if tsb > 10:
        interp = "Forme fraiche - bon moment pour un effort intense"
    elif tsb > -10:
        interp = "Equilibre - entrainement modere conseille"
    elif tsb > -30:
        interp = "Fatigue accumulee - surveiller la recuperation"
    else:
        interp = "Surcharge - recuperation prioritaire"

    return {
        "CTL": dernier["CTL"],
        "ATL": dernier["ATL"],
        "TSB": dernier["TSB"],
        "interpretation": interp,
        "historique": historique[-30:],
    }


# ---------------------------------------------------------------------------
# Analyse Claude
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Tu es un coach specialise en medecine du sport pour les athletes de plus de 50 ans.
Tu conseilles Olivier, 50+ ans, base a Shanghai.

PROFIL MEDICAL & HISTORIQUE :
- Synovite du genou en decembre 2025, declenchee par une augmentation de volume trop rapide et erratique
- Risque de recidive si progression trop agressive ou irreguliere
- Recuperation musculaire plus lente qu apres 50 ans — les adaptations prennent 20-30% plus de temps
- Tendons et cartilages moins tolerants aux surcharges brutales

PROFIL SPORTIF :
- FTP actuelle : 201W (Garmin) → cible 230W fin 2026
- Volume velo cible : 120 km/semaine — a atteindre PROGRESSIVEMENT (+10% max par semaine)
- Objectif running : semi-marathon avant Noel 2026
- Sports pratiques : gravel, velo route, Zwift (Wahoo Kickr), course a pied

REGLES ABSOLUES DE PROGRESSION :
1. Ne jamais augmenter le volume running de plus de 10% par semaine
2. Alterner semaines de charge et semaines allegees (-20-30% volume)
3. Privilegier la regularite sur l intensite — 3 seances/semaine regulieres valent mieux que 5 en rafale
4. Toujours inclure au moins 1 jour de repos complet entre deux seances running
5. Si TSB < -20 : recommander recuperation active uniquement, pas de seances intenses
6. Signaler explicitement tout risque de surcharge articulaire

FORMAT DE REPONSE :
- Court, direct, en francais, oriente action
- Toujours mentionner le niveau de risque articulaire des seances proposees : 🟢 Faible / 🟡 Modere / 🔴 Eleve
- Markdown avec titres ## et listes -
"""

def analyser_avec_claude(activites, charge, km_semaine, km_run=0):
    # Cle de cache basee sur la date du jour + CTL (change si nouvelles activites)
    today_str = date.today().isoformat()
    cache_key = "analyse_{}_{}".format(today_str, charge["CTL"])
    cached = _cache.get(cache_key)
    if cached and time.time() - cached["ts"] < CACHE_TTL:
        return cached["data"]

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # 5 dernieres activites (deja triees du plus recent au plus ancien)
    recentes = activites[:5]
    recentes_txt = "\n".join(
        "- {} | {} | {} km | {}m D+ | {} | FC:{} | {}W".format(
            a["date"], a["type"], a["distance_km"], a["elevation_m"],
            a["duree"], a["fc_moy"] or "?", a["puissance_moy"] or "?"
        )
        for a in recentes
    )

    prompt = (
        "Date : {}\n\n".format(datetime.now().strftime("%d %B %Y")) +
        "METRIQUES\nCTL={} ATL={} TSB={}\n".format(charge["CTL"], charge["ATL"], charge["TSB"]) +
        "Volume velo semaine : {} km / 120 km | Run semaine : {} km\n\n".format(km_semaine, km_run) +
        "5 DERNIERES ACTIVITES (de la plus recente a la plus ancienne)\n" +
        recentes_txt + "\n\n" +
        "Reponds en 4 sections :\n\n"
        "## Bilan recent\n"
        "(2-3 phrases sur les dernieres sorties, FC, intensite, regularite)\n\n"
        "## Trajectoire objectifs\n"
        "- FTP 230W : progression en cours ?\n"
        "- Semi-marathon decembre : base aerobie suffisante ?\n\n"
        "## Recommandations 7 prochains jours\n"
        "Propose exactement 3 seances adaptees au profil medical (50+, synovite genou)\n"
        "Pour chaque seance : jour suggere, type, duree, intensite precise, risque articulaire 🟢/🟡/🔴\n\n"
        "## SEANCES_JSON\n"
        "Termine OBLIGATOIREMENT par un tableau JSON valide de 3 seances.\n"
        "Format EXACT (copie ce format, adapte les valeurs) :\n"
        "[{\"titre\":\"Zwift Z2\",\"type\":\"indoor\",\"meta\":\"45 min Z2 120-145W\",\"risque\":\"faible\",\"tags\":[\"Zwift\",\"Z2\"]},"
        "{\"titre\":\"Run facile\",\"type\":\"run\",\"meta\":\"5km FC<150\",\"risque\":\"faible\",\"tags\":[\"Course\",\"Endurance\"]},"
        "{\"titre\":\"Velo Z2/Z3\",\"type\":\"bike\",\"meta\":\"60min 2x8min Z3\",\"risque\":\"modere\",\"tags\":[\"Velo outdoor\",\"Z3\"]}]\n"
        "NE PAS mettre de markdown autour du JSON. Le JSON doit etre la DERNIERE chose de ta reponse."
    )

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    full_text = msg.content[0].text

    # Extrait le JSON des seances de la reponse
    import re as _re
    seances = []
    analyse_text = full_text

    # Essaie plusieurs patterns pour trouver le JSON
    def try_parse_json(s):
        try:
            candidate = json.loads(s)
            if isinstance(candidate, list) and len(candidate) > 0:
                if all(isinstance(x, dict) and "titre" in x for x in candidate):
                    return candidate
        except Exception:
            pass
        return None

    # Pattern 1: bloc ```json ... ```
    m1 = _re.search(r'```json\s*(\[.*?\])\s*```', full_text, _re.DOTALL)
    if m1:
        seances = try_parse_json(m1.group(1)) or []

    # Pattern 2: bloc ``` ... ```
    if not seances:
        m2 = _re.search(r'```\s*(\[.*?\])\s*```', full_text, _re.DOTALL)
        if m2:
            seances = try_parse_json(m2.group(1)) or []

    # Pattern 3: JSON brut avec "titre"
    if not seances:
        m3 = _re.search(r'(\[\s*\{"titre".*?\}\s*\])', full_text, _re.DOTALL)
        if m3:
            seances = try_parse_json(m3.group(1)) or []

    # Nettoie le texte affiche
    if "## SEANCES_JSON" in full_text:
        analyse_text = full_text[:full_text.rfind("## SEANCES_JSON")].strip()
    else:
        # Supprime les blocs de code du texte
        analyse_text = _re.sub(r'```[\s\S]*?```', '', full_text).strip()


    result = {"texte": analyse_text, "seances": seances}
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
    try:
        # Recupere et trie les activites (plus recent en premier)
        activites_brutes = get_activites(jours=JOURS_HISTORIQUE)
        activites = [formater_activite(a) for a in activites_brutes]

        charge = calculer_charge(activites)

        # Volume semaine en cours (lundi -> aujourd'hui)
        today_d = date.today()
        debut_sem = today_d - timedelta(days=today_d.weekday())
        debut_sem_str = debut_sem.isoformat()

        TYPES_VELO = {'Ride', 'GravelRide', 'VirtualRide', 'EBikeRide', 'MountainBikeRide'}
        TYPES_RUN  = {'Run', 'VirtualRun', 'TrailRun'}

        km_velo_semaine = round(sum(
            a['distance_km'] for a in activites
            if a['date'] >= debut_sem_str and a['type'] in TYPES_VELO
        ), 1)
        km_run_semaine = round(sum(
            a['distance_km'] for a in activites
            if a['date'] >= debut_sem_str and a['type'] in TYPES_RUN
        ), 1)
        km_semaine = km_velo_semaine  # pour compatibilite Claude

        # Stats par type
        par_type = defaultdict(lambda: {"nb": 0, "km": 0.0, "h": 0.0})
        for a in activites:
            par_type[a["type"]]["nb"] += 1
            par_type[a["type"]]["km"] += a["distance_km"]
            par_type[a["type"]]["h"]  += a["duree_h"]
        stats = {k: {
            "nb": v["nb"],
            "km": round(v["km"], 1),
            "h":  round(v["h"], 1),
        } for k, v in sorted(par_type.items(), key=lambda x: -x[1]["nb"])}

        # Analyse Claude
        analyse_result = analyser_avec_claude(activites, charge, km_semaine, km_run_semaine)
        analyse_md = analyse_result["texte"] if isinstance(analyse_result, dict) else analyse_result
        seances_dynamiques = analyse_result["seances"] if isinstance(analyse_result, dict) else []

        # 10 dernieres activites pour l'affichage (deja triees)
        recentes = activites[:10]

        return jsonify({
            "ok":            True,
            "charge":        charge,
            "km_semaine":       km_velo_semaine,
            "km_run_semaine":   km_run_semaine,
            "reste_semaine":    round(max(0, 120 - km_velo_semaine), 1),
            "nb_activites":  len(activites),
            "stats":         stats,
            "recentes":      recentes,
            "analyse":       analyse_md,
            "seances":       seances_dynamiques,
            "updated_at":    datetime.now().strftime("%H:%M"),
            "objectifs":     OBJECTIFS,
            "debug_first":   activites[0]["date"] if activites else "vide",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/refresh")
@login_required
def api_refresh():
    _cache.clear()
    return jsonify({"ok": True, "message": "Cache vide"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
