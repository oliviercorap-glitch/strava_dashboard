"""
Strava Dashboard — Application Flask
=====================================
Dashboard personnel temps réel : CTL/ATL/TSB, recommandations d'entrainement,
et débriefs post-séance (ressenti + analyse coach) via l'API Anthropic.
Protégé par mot de passe simple.

Variables d'environnement (Railway) :
  STRAVA_CLIENT_ID
  STRAVA_CLIENT_SECRET
  STRAVA_REFRESH_TOKEN
  ANTHROPIC_API_KEY
  DASHBOARD_PASSWORD
  SECRET_KEY
  DATABASE_URL          (injectée par le service Postgres Railway)
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

# Modules du projet
import db
import coach
import intervals as intervals_mod
import strava_detail

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "changeme-random-string-here")

FTP              = 201   # sert uniquement à calibrer le calcul du TSS vélo (pas un objectif)
JOURS_HISTORIQUE = 90
OBJECTIFS = {
    "objectif_principal": "Semi-marathon avant Noel 2026",
    "velo_role":          "Volume aerobie de soutien + plaisir social (S-CAFE)",
    "velo_cible_groupe":  "Tenir 100 km en groupe a 28-30 km/h (~120W)",
    "semi_date":          "decembre 2026",
}

_cache = {}
CACHE_TTL = 1800  # 30 minutes

TYPES_VELO = {'Ride', 'GravelRide', 'VirtualRide', 'EBikeRide', 'MountainBikeRide'}
TYPES_RUN  = {'Run', 'VirtualRun', 'TrailRun'}


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
    """Récupère les activités des N derniers jours, triées du plus récent au plus ancien."""
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


def _sport_from_type(t):
    return "ride" if t in TYPES_VELO else "run"


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
Tu conseilles Olivier, 52 ans, base a Shanghai.

PROFIL MEDICAL & HISTORIQUE :
- Synovite du genou en decembre 2025, declenchee par une augmentation de volume trop rapide et erratique
- Risque de recidive si progression trop agressive ou irreguliere
- Recuperation musculaire plus lente apres 50 ans — les adaptations prennent 20-30% plus de temps
- Tendons et cartilages moins tolerants aux surcharges brutales

PROFIL SPORTIF :
- OBJECTIF PRINCIPAL ET UNIQUE : semi-marathon avant Noel 2026
- Le velo N'EST PLUS un objectif de puissance : c'est du volume aerobie de soutien a la course
  + du plaisir social (sorties groupe S-CAFE, tenir 100 km a 28-30 km/h ~120W). Une sortie velo
  Z2 ou a l'abri dans le groupe est une seance REUSSIE. Ne JAMAIS recommander de "pousser plus
  fort" sur le velo ni evoquer une progression de FTP comme un objectif.
- La course a pied est la seule discipline qui compte pour la performance ; le velo ne doit pas
  compromettre la recuperation des jambes pour la course.
- FTP actuelle 201W : utile seulement pour calibrer l'intensite des sorties, PAS un objectif.
- Sports : gravel, velo route, Zwift (Wahoo Kickr), course a pied.

REGLES ABSOLUES DE PROGRESSION :
1. Ne jamais augmenter le volume running de plus de 10% par semaine
2. Alterner semaines de charge et semaines allegees (-20-30% volume)
3. Privilegier la regularite sur l'intensite — 3 seances/semaine regulieres valent mieux que 5 en rafale
4. Toujours inclure au moins 1 jour de repos complet entre deux seances running
5. Si TSB < -20 : recommander recuperation active uniquement, pas de seances intenses
6. Signaler explicitement tout risque de surcharge articulaire

FORMAT DE REPONSE :
- Court, direct, en francais, oriente action
- Toujours mentionner le niveau de risque articulaire des seances proposees : 🟢 Faible / 🟡 Modere / 🔴 Eleve
- Markdown avec titres ## et listes -
"""

def analyser_avec_claude(activites, charge, km_semaine, km_run=0, ressentis=None):
    today_str = date.today().isoformat()
    # La clé de cache inclut le nombre de ressentis : si tu ajoutes un ressenti,
    # le bilan est recalculé au lieu de resservir une version obsolète.
    nb_ressentis = len(ressentis) if ressentis else 0
    cache_key = "analyse_{}_{}_{}".format(today_str, charge["CTL"], nb_ressentis)
    cached = _cache.get(cache_key)
    if cached and time.time() - cached["ts"] < CACHE_TTL:
        return cached["data"]

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    recentes = activites[:5]
    recentes_txt = "\n".join(
        "- {} | {} | {} km | {}m D+ | {} | FC:{} | {}W".format(
            a["date"], a["type"], a["distance_km"], a["elevation_m"],
            a["duree"], a["fc_moy"] or "?", a["puissance_moy"] or "?"
        )
        for a in recentes
    )

    # Ressentis bruts des 7 derniers jours (voix directe d'Olivier, non filtrée)
    ressentis_txt = ""
    if ressentis:
        lignes = "\n".join(
            "- {} | {} : « {} »".format(
                r.get("activity_date"), r.get("sport", "?"), r.get("ressenti", "")
            )
            for r in ressentis
        )
        ressentis_txt = (
            "\nRESSENTIS D'OLIVIER (7 derniers jours, ses mots exacts — a prendre en compte\n"
            "dans le bilan : ils expliquent souvent ce que les chiffres seuls ne disent pas,\n"
            "comme la chaleur, la fatigue, une gene, une bonne sensation)\n" + lignes + "\n"
        )

    prompt = (
        "Date : {}\n\n".format(datetime.now().strftime("%d %B %Y")) +
        "METRIQUES\nCTL={} ATL={} TSB={}\n".format(charge["CTL"], charge["ATL"], charge["TSB"]) +
        "Volume velo semaine : {} km | Run semaine : {} km\n\n".format(km_semaine, km_run) +
        "5 DERNIERES ACTIVITES (de la plus recente a la plus ancienne)\n" +
        recentes_txt + "\n" +
        ressentis_txt + "\n" +
        "Reponds en 4 sections :\n\n"
        "## Bilan recent\n"
        "(2-3 phrases sur les dernieres sorties, FC, intensite, regularite. Croise les chiffres\n"
        "avec les ressentis d'Olivier quand ils sont disponibles : si un ressenti explique une\n"
        "donnee, dis-le explicitement plutot que de rester dans l'hypothese.)\n\n"
        "## Trajectoire objectif\n"
        "- Semi-marathon decembre : la base aerobie running progresse-t-elle ? Volume suffisant ?\n"
        "- Velo : sert-il bien de soutien aerobie sans fatiguer les jambes de course ?\n"
        "  (NE PAS parler de progression FTP ni recommander de pousser la puissance)\n\n"
        "## Recommandations 7 prochains jours\n"
        "Propose exactement 3 seances adaptees au profil medical (50+, synovite genou) et a\n"
        "l'objectif semi (le velo reste en soutien aerobie, pas d'objectif de puissance).\n"
        "Pour chaque seance : jour suggere, type, duree, intensite precise, risque articulaire 🟢/🟡/🔴\n\n"
        "## SEANCES_JSON\n"
        "Termine OBLIGATOIREMENT par un tableau JSON valide de 3 seances, precede EXACTEMENT du\n"
        "marqueur ###JSON### sur sa propre ligne.\n"
        "Format EXACT (copie ce format, adapte les valeurs) :\n"
        "###JSON###\n"
        "[{\"titre\":\"Zwift Z2\",\"type\":\"indoor\",\"meta\":\"45 min Z2 120-145W\",\"risque\":\"faible\",\"tags\":[\"Zwift\",\"Z2\"]},"
        "{\"titre\":\"Run facile\",\"type\":\"run\",\"meta\":\"5km FC<150\",\"risque\":\"faible\",\"tags\":[\"Course\",\"Endurance\"]},"
        "{\"titre\":\"Velo Z2\",\"type\":\"bike\",\"meta\":\"90min Z2 sortie groupe\",\"risque\":\"faible\",\"tags\":[\"Velo outdoor\",\"Z2\"]}]\n"
        "Le JSON doit etre la DERNIERE chose de ta reponse, sur une seule ligne, sans markdown autour."
    )

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    full_text = msg.content[0].text

    analyse_text, seances = _extraire_analyse_et_seances(full_text)

    result = {"texte": analyse_text, "seances": seances}
    _cache[cache_key] = {"data": result, "ts": time.time()}
    return result


def _extraire_analyse_et_seances(full_text):
    """
    Sépare le texte d'analyse (markdown à afficher) du JSON des séances.
    Robuste : tente plusieurs stratégies et garantit qu'aucun JSON ni marqueur
    ne fuite dans le texte affiché, même si l'IA change légèrement de format.
    """
    import re as _re

    def try_parse_json(s):
        try:
            candidate = json.loads(s)
            if isinstance(candidate, list) and candidate and \
               all(isinstance(x, dict) and "titre" in x for x in candidate):
                return candidate
        except Exception:
            pass
        return None

    seances = []

    # 1) Marqueur explicite ###JSON###
    if "###JSON###" in full_text:
        avant, apres = full_text.split("###JSON###", 1)
        seances = try_parse_json(apres.strip()) or []
        texte = avant
    else:
        texte = full_text
        # 2) Bloc ```json ... ``` ou ``` ... ```
        m = _re.search(r'```(?:json)?\s*(\[.*?\])\s*```', full_text, _re.DOTALL)
        if m:
            seances = try_parse_json(m.group(1)) or []
        # 3) Dernier tableau JSON brut contenant "titre"
        if not seances:
            matches = _re.findall(r'(\[\s*\{.*?"titre".*?\}\s*\])', full_text, _re.DOTALL)
            if matches:
                seances = try_parse_json(matches[-1]) or []

    # Nettoyage FINAL du texte affiché : on retire tout résidu possible
    # (ancien titre ## SEANCES_JSON, marqueur, blocs de code, tableau JSON brut).
    texte = _re.split(r'#{0,3}\s*SEANCES_JSON', texte)[0]
    texte = texte.replace("###JSON###", "")
    texte = _re.sub(r'```[\s\S]*?```', '', texte)
    texte = _re.sub(r'\[\s*\{.*?"titre".*?\}\s*\]', '', texte, flags=_re.DOTALL)
    return texte.strip(), seances


# ---------------------------------------------------------------------------
# Routes principales
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    return render_template("dashboard.html")


@app.route("/api/data")
@login_required
def api_data():
    try:
        activites_brutes = get_activites(jours=JOURS_HISTORIQUE)
        activites = [formater_activite(a) for a in activites_brutes]

        charge = calculer_charge(activites)

        today_d = date.today()
        debut_sem = today_d - timedelta(days=today_d.weekday())
        debut_sem_str = debut_sem.isoformat()

        km_velo_semaine = round(sum(
            a['distance_km'] for a in activites
            if a['date'] >= debut_sem_str and a['type'] in TYPES_VELO
        ), 1)
        km_run_semaine = round(sum(
            a['distance_km'] for a in activites
            if a['date'] >= debut_sem_str and a['type'] in TYPES_RUN
        ), 1)
        km_semaine = km_velo_semaine

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

        # Ressentis des 7 derniers jours, pour enrichir le bilan (best effort)
        ressentis_recents = []
        try:
            depuis_7j = (today_d - timedelta(days=7)).isoformat()
            ressentis_recents = db.get_ressentis_recents(depuis_7j)
        except Exception as e:
            print(f"[db] lecture ressentis récents KO : {e}")

        analyse_result = analyser_avec_claude(
            activites, charge, km_semaine, km_run_semaine, ressentis=ressentis_recents)
        analyse_md = analyse_result["texte"]
        seances_dynamiques = analyse_result["seances"]

        recentes = activites[:10]

        # Attache les débriefs déjà enregistrés aux activités affichées
        try:
            ids = [a["id"] for a in recentes]
            debriefs = db.get_debriefs_for(ids)
            for a in recentes:
                d = debriefs.get(a["id"])
                a["ressenti"] = d["ressenti"] if d else None
                a["debrief"]  = d["debrief"] if d else None
        except Exception as e:
            print(f"[db] lecture débriefs KO : {e}")

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


# ---------------------------------------------------------------------------
# Routes débrief post-séance
# ---------------------------------------------------------------------------

@app.route("/api/ressenti", methods=["POST"])
@login_required
def api_ressenti():
    """Enregistre le ressenti d'une activité, puis génère le débrief coach."""
    data = request.get_json(force=True)
    try:
        activity_id = int(data["activity_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "activity_id invalide"}), 400

    ressenti = (data.get("ressenti") or "").strip()
    if not ressenti:
        return jsonify({"ok": False, "error": "Ressenti vide"}), 400

    # Retrouve le résumé de l'activité
    activites = [formater_activite(a) for a in get_activites(JOURS_HISTORIQUE)]
    activite = next((a for a in activites if a["id"] == activity_id), None)
    if not activite:
        return jsonify({"ok": False, "error": "Activité introuvable"}), 404

    sport = _sport_from_type(activite["type"])

    # Sauvegarde le ressenti
    try:
        db.save_ressenti(activity_id, ressenti,
                         activity_date=activite["date"], sport=sport)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Sauvegarde KO : {e}"}), 500

    # Détecte les intervalles (laps + streams) — best effort
    intervals_result = None
    try:
        token = rafraichir_token()
        detail = strava_detail.get_activity_detail(activity_id, token, sport)
        intervals_result = intervals_mod.extract_intervals(
            activite, detail["laps"], detail["streams"], sport)
    except Exception as e:
        print(f"[intervals] détection KO pour {activity_id} : {e}")

    # Génère le débrief coach
    try:
        debrief = coach.generate_debrief(activite, ressenti, intervals_result)
        db.save_debrief(activity_id, debrief)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Coach KO : {e}"}), 500

    return jsonify({
        "ok": True,
        "debrief": debrief,
        "intervals_method": intervals_result["method"] if intervals_result else None,
    })


@app.route("/api/debrief/<int:activity_id>")
@login_required
def api_get_debrief(activity_id):
    """Retourne le débrief déjà enregistré pour une activité."""
    try:
        d = db.get_debrief(activity_id)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if not d:
        return jsonify({"ok": False, "error": "Aucun débrief"}), 404
    return jsonify({"ok": True, "ressenti": d["ressenti"], "debrief": d["debrief"]})


# ---------------------------------------------------------------------------
# Démarrage
# ---------------------------------------------------------------------------

# Initialise la table debriefs au démarrage (si DATABASE_URL présente)
try:
    db.init_db()
except Exception as e:
    print(f"[db] init KO (les débriefs seront indisponibles) : {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
