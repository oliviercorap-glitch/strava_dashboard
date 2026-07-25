# Patches à appliquer sur `app.py`

Les 4 nouveaux modules (`db.py`, `coach.py`, `intervals.py`, `strava_detail.py`)
se posent à côté de ton `app.py`. Voici ce qu'il faut modifier DANS `app.py`.

---

## 1. Imports (en haut, après les imports existants)

```python
import db
import coach
import intervals as intervals_mod
import strava_detail
```

---

## 2. Objectifs — remplacer le dict OBJECTIFS

REMPLACER :
```python
OBJECTIFS = {
    "ftp_actuelle":  201,
    "ftp_cible":     230,
    "km_sem_cible":  120,
    "semi_date":     "decembre 2026",
}
```
PAR :
```python
OBJECTIFS = {
    "objectif_principal": "Semi-marathon avant Noel 2026",
    "velo_role":          "Volume aerobie de soutien + plaisir social (S-CAFE)",
    "velo_cible_groupe":  "Tenir 100 km en groupe a 28-30 km/h (~120W)",
    "semi_date":          "decembre 2026",
}
```
(La constante `FTP = 201` reste : elle sert toujours au calcul du TSS vélo,
c'est une donnée physiologique, pas un objectif.)

---

## 3. SYSTEM_PROMPT — réaligner sur le semi

Dans ton `SYSTEM_PROMPT` (celui de `analyser_avec_claude`), REMPLACER le
bloc "PROFIL SPORTIF" par :

```
PROFIL SPORTIF :
- OBJECTIF PRINCIPAL ET UNIQUE : semi-marathon avant Noel 2026
- Le velo N'EST PLUS un objectif de puissance : c'est du volume aerobie de
  soutien a la course + du plaisir social (sorties groupe S-CAFE, tenir
  100 km a 28-30 km/h ~120W). Une sortie velo Z2 ou a l'abri dans le groupe
  est une seance REUSSIE. Ne jamais recommander de "pousser plus fort" sur le velo.
- La course a pied est la seule discipline qui compte pour la performance ;
  le velo ne doit pas compromettre la recuperation des jambes pour la course.
- FTP actuelle 201W : utile seulement pour calibrer l'intensite des sorties,
  PAS un objectif a faire progresser.
- Sports : gravel, velo route, Zwift (Wahoo Kickr), course a pied.
```

Et dans la section "Trajectoire objectifs" du prompt utilisateur, REMPLACER :
```
"- FTP 230W : progression en cours ?\n"
"- Semi-marathon decembre : base aerobie suffisante ?\n\n"
```
PAR :
```
"- Semi-marathon decembre : base aerobie suffisante ? Volume run en hausse ?\n"
"- Velo : sert-il bien de soutien aerobie sans fatiguer les jambes de course ?\n\n"
```

---

## 4. init_db au démarrage — juste avant `if __name__ == "__main__":`

```python
# Initialise la table debriefs au démarrage (si DATABASE_URL présente)
try:
    db.init_db()
except Exception as e:
    print(f"[db] init KO (les débriefs seront indisponibles) : {e}")
```

---

## 5. Enrichir la sortie /api/data avec les débriefs existants

Dans `api_data()`, APRÈS la ligne `recentes = activites[:10]`, AJOUTER :

```python
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
```

---

## 6. Nouvelles routes — à ajouter avant `if __name__ == "__main__":`

```python
# Mapping type Strava -> sport pour intervals
TYPES_VELO_SET = {'Ride', 'GravelRide', 'VirtualRide', 'EBikeRide', 'MountainBikeRide'}

def _sport_from_type(t):
    return "ride" if t in TYPES_VELO_SET else "run"


@app.route("/api/ressenti", methods=["POST"])
@login_required
def api_ressenti():
    """Enregistre le ressenti d'une activité, puis génère le débrief coach."""
    data = request.get_json(force=True)
    activity_id = int(data["activity_id"])
    ressenti    = (data.get("ressenti") or "").strip()
    if not ressenti:
        return jsonify({"ok": False, "error": "Ressenti vide"}), 400

    # Retrouve le résumé de l'activité (depuis le cache d'activités)
    activites = [formater_activite(a) for a in get_activites(JOURS_HISTORIQUE)]
    activite = next((a for a in activites if a["id"] == activity_id), None)
    if not activite:
        return jsonify({"ok": False, "error": "Activité introuvable"}), 404

    sport = _sport_from_type(activite["type"])

    # Sauvegarde le ressenti
    db.save_ressenti(activity_id, ressenti,
                     activity_date=activite["date"], sport=sport)

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
    d = db.get_debrief(activity_id)
    if not d:
        return jsonify({"ok": False, "error": "Aucun débrief"}), 404
    return jsonify({"ok": True, "ressenti": d["ressenti"], "debrief": d["debrief"]})
```

---

## 7. requirements.txt — ajouter

```
psycopg2-binary
```

---

## 8. Railway

- Ajoute un service **Postgres** à ton projet (New → Database → PostgreSQL).
- Railway injecte automatiquement `DATABASE_URL` dans ton app si les deux
  services sont dans le même projet. Sinon, référence-la manuellement :
  variable `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`.
