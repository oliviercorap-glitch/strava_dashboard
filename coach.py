"""
coach.py — Génération de débriefs post-séance.
==============================================
Croise le ressenti écrit par Olivier avec les métriques objectives de
l'activité (résumé + intervalles détectés), et produit un débrief façon
coach + recommandations, via l'API Anthropic.
"""

import os
import anthropic

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Contexte athlète — aligné sur l'objectif à jour (semi prioritaire,
# vélo en soutien) + volet médical conservé.
ATHLETE_CONTEXT = """
Athlète : Olivier, 52 ans, CFO basé à Shanghai.

OBJECTIF PRINCIPAL ET UNIQUE : semi-marathon avant Noël 2026.
Le vélo n'est PLUS un objectif de puissance. C'est :
  - du volume aérobie de soutien à la course (développe le moteur sans
    impact articulaire),
  - du plaisir social (sorties de groupe S-CAFÉ Cycling Club, tenir 100 km
    en groupe à 28-30 km/h, ~120W moyens, sans objectif de perf).
Donc une sortie vélo tranquille en zone 2 ou à l'abri dans le groupe est
une séance RÉUSSIE — ne jamais recommander de "pousser plus fort" sur le vélo.
La course à pied est la seule discipline qui compte pour la performance ;
le vélo ne doit pas compromettre la récupération des jambes pour la course.

PROFIL MÉDICAL (à respecter absolument) :
  - Synovite du genou en décembre 2025, déclenchée par une hausse de volume
    trop rapide et erratique. Risque de récidive si progression agressive.
  - Récupération plus lente après 50 ans (+20-30% de temps d'adaptation).
  - Tendons/cartilages moins tolérants aux surcharges brutales.

CONTEXTE ACTUEL : reprise après une coupure de ~3 semaines (fin juin/début
juillet). Priorité : remonter le CTL progressivement, sans surmenage.
"""

SYSTEM_PROMPT = f"""Tu es le coach personnel d'Olivier. Tu débriefes ses
séances comme un vrai entraîneur : lucide, bienveillant, concret, sans
flagornerie. Tu tutoies Olivier.

{ATHLETE_CONTEXT}

Pour chaque séance on te donne :
1. Les MÉTRIQUES OBJECTIVES (résumé Strava/Garmin).
2. Éventuellement le DÉTAIL DES INTERVALLES (si c'est du fractionné).
3. Le RESSENTI d'Olivier (ce qu'il a écrit après la séance).

Ton travail :
- Croiser objectif, données et ressenti. Repère les écarts intéressants
  (FC haute pour un effort facile = fatigue ou chaleur ; ressenti dur avec
  chiffres modestes = signal de récup ; reps qui décrochent en fin de série
  = fatigue ou départ trop rapide).
- Situer la séance par rapport à l'objectif semi et à la phase de reprise.
- Signaler tout risque de surcharge articulaire (genou).
- Donner 1 à 3 recommandations concrètes pour la suite.
- Rester BREF : un débrief se lit en 30 secondes.

Format :
- Un court paragraphe de lecture de séance (2-4 phrases).
- Puis "→" suivi de 1 à 3 recommandations, une par ligne.
Interprète les chiffres, ne les récite pas."""


def format_metrics(a: dict) -> str:
    """Formate le résumé d'activité (clés du formater_activite de app.py)."""
    labels = {
        "type": "Sport", "date": "Date", "distance_km": "Distance (km)",
        "duree": "Durée", "vitesse_moy": "Vitesse moy. (km/h)",
        "puissance_moy": "Puissance moy. (W)", "fc_moy": "FC moy. (bpm)",
        "fc_max": "FC max (bpm)", "elevation_m": "D+ (m)",
        "suffer_score": "Suffer score",
    }
    lines = [f"- {lbl} : {a[k]}" for k, lbl in labels.items()
             if a.get(k) not in (None, "", 0)]
    return "\n".join(lines) if lines else "- (aucune métrique)"


def format_intervals(intervals: dict) -> str:
    """Formate les reps détectées (sortie de intervals.extract_intervals)."""
    if not intervals or not intervals.get("reps"):
        return ""
    method = {"laps": "laps manuels", "auto": "détection auto"}.get(
        intervals.get("method"), intervals.get("method"))
    lines = [f"(source : {method})"]
    for r in intervals["reps"]:
        parts = [f"Rep {r['rep']}"]
        if r.get("duration_s"):
            parts.append(f"{r['duration_s']}s")
        if r.get("avg_power_w"):
            parts.append(f"{r['avg_power_w']}W")
        if r.get("avg_effort"):
            parts.append(f"effort {r['avg_effort']}")
        if r.get("avg_hr"):
            parts.append(f"{r['avg_hr']} bpm")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def generate_debrief(activity: dict, feeling: str, intervals: dict = None,
                     hr_txt: str = "") -> str:
    """
    activity  : dict résumé (formater_activite)
    feeling   : ressenti écrit par Olivier
    intervals : sortie de extract_intervals (optionnel)
    """
    metrics_txt = format_metrics(activity)
    feeling_txt = feeling.strip() if feeling and feeling.strip() else "(aucun ressenti fourni)"
    intervals_txt = format_intervals(intervals)

    content = f"MÉTRIQUES OBJECTIVES :\n{metrics_txt}\n\n"
    if hr_txt:
        content += f"{hr_txt}\n\n"
    if intervals_txt:
        content += f"DÉTAIL DES INTERVALLES :\n{intervals_txt}\n\n"
    content += f"RESSENTI D'OLIVIER :\n{feeling_txt}"

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")
