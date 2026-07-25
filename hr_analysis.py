"""
hr_analysis.py — Analyse de la fréquence cardiaque à partir du stream.
======================================================================
Le résumé Strava ne donne que FC moyenne et FC max, ce qui masque la
réalité d'une séance : une sortie qui passe 26% en Z4 avec des pics à 175
"pèse" bien plus lourd qu'une FC moyenne de 159 ne le suggère.

Ce module exploite le stream de FC (seconde par seconde) déjà récupéré par
strava_detail.py pour caractériser l'effort cardiaque réel :
  - répartition du temps par zone,
  - dérive cardiaque (1re moitié vs 2e moitié),
  - temps passé en haute intensité (Z4+).

Zones basées sur la FC max d'Olivier = 185 bpm (seuils Strava) :
  Z1 0-120 | Z2 121-150 | Z3 151-165 | Z4 166-179 | Z5 >=180
"""

# Bornes hautes de chaque zone (incluses). Z5 = tout ce qui dépasse Z4.
FC_MAX = 185
ZONES = [
    ("Z1", 0,   120),
    ("Z2", 121, 150),
    ("Z3", 151, 165),
    ("Z4", 166, 179),
    ("Z5", 180, 10_000),
]
# Seuil "haute intensité" : Z4 et au-dessus.
SEUIL_HAUTE_INTENSITE = 166


def _zone_de(bpm: float) -> str:
    for nom, lo, hi in ZONES:
        if lo <= bpm <= hi:
            return nom
    return "Z1"


def analyser_fc(hr_stream: list, time_stream: list | None = None) -> dict | None:
    """
    Analyse un stream de FC.

    hr_stream   : liste des FC par point (bpm). Les None sont ignorés.
    time_stream : timestamps (s) alignés, pour pondérer par le temps réel.
                  Si absent, on suppose 1 point = 1 seconde.

    Retourne un dict prêt à formater pour le coach, ou None si pas de données.
    """
    # Nettoie en gardant l'alignement hr/temps
    pts = []  # (bpm, t)
    for i, hr in enumerate(hr_stream or []):
        if hr is None:
            continue
        t = time_stream[i] if time_stream and i < len(time_stream) else i
        pts.append((float(hr), t))

    if len(pts) < 10:
        return None

    hrs = [p[0] for p in pts]
    n = len(pts)

    # --- Répartition par zone, pondérée par la durée de chaque échantillon ---
    zone_secondes = {z[0]: 0.0 for z in ZONES}
    total_s = 0.0
    for idx, (bpm, t) in enumerate(pts):
        # Durée représentée par ce point = écart au point suivant (ou 1s par défaut)
        if idx + 1 < n:
            dt = max(0.0, pts[idx + 1][1] - t)
            if dt == 0 or dt > 60:   # garde-fou contre trous/pauses
                dt = 1.0
        else:
            dt = 1.0
        zone_secondes[_zone_de(bpm)] += dt
        total_s += dt

    zone_pct = {z: round(100 * s / total_s) for z, s in zone_secondes.items()} if total_s else {}

    # --- Temps en haute intensité (Z4+) ---
    secondes_haute = sum(s for z, s in zone_secondes.items() if z in ("Z4", "Z5"))
    pct_haute = round(100 * secondes_haute / total_s) if total_s else 0

    # --- Dérive cardiaque : FC moyenne 1re moitié vs 2e moitié ---
    moitie = n // 2
    fc_debut = sum(hrs[:moitie]) / moitie if moitie else 0
    fc_fin = sum(hrs[moitie:]) / (n - moitie) if (n - moitie) else 0
    derive = round(fc_fin - fc_debut, 1)

    return {
        "fc_moy": round(sum(hrs) / n),
        "fc_max": round(max(hrs)),
        "zone_pct": zone_pct,                         # ex {"Z1":1,"Z2":17,...}
        "pct_haute_intensite": pct_haute,             # % du temps en Z4+
        "secondes_haute": int(secondes_haute),
        "derive_bpm": derive,                         # + = FC monte en fin (dérive)
        "duree_min": round(total_s / 60, 1),
    }


def formater_pour_coach(a: dict | None) -> str:
    """Rend l'analyse FC en texte lisible pour le prompt du coach. '' si None."""
    if not a:
        return ""
    zones_txt = " | ".join(
        f"{z} {a['zone_pct'].get(z, 0)}%" for z, _, _ in ZONES
    )
    lignes = [
        "ANALYSE FC DÉTAILLÉE (à partir du tracé seconde par seconde) :",
        f"- Répartition par zone : {zones_txt}",
        f"- Temps en haute intensité (Z4+) : {a['pct_haute_intensite']}% "
        f"(~{a['secondes_haute'] // 60} min à FC ≥ {SEUIL_HAUTE_INTENSITE})",
        f"- Dérive cardiaque : {a['derive_bpm']:+} bpm entre 1re et 2e moitié",
    ]
    # Note interprétative pour attirer l'œil du coach sur l'effort caché
    if a["pct_haute_intensite"] >= 20:
        lignes.append(
            f"  ⚠ La FC moyenne ({a['fc_moy']}) masque une part importante passée en "
            f"haute intensité : la séance a été plus dure que la moyenne ne le suggère."
        )
    if a["derive_bpm"] >= 8:
        lignes.append(
            "  ⚠ Dérive cardiaque marquée : fatigue, chaleur ou déshydratation probable."
        )
    return "\n".join(lignes)
