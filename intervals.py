"""
intervals.py — Détection des intervalles d'une séance de fractionné.

Stratégie hybride, par ordre de priorité :
  1. LAPS manuels : si des laps existent ET ressemblent à du fractionné
     (alternance effort/récup), ils font autorité.
  2. DÉTECTION AUTO : sinon, on repère les intervalles dans les streams
     (puissance pour le vélo, allure/vitesse pour la course).

Le résultat est une liste normalisée de "reps" exploitable par coach.py.
"""

from statistics import mean, median


# ---------------------------------------------------------------------------
# 1. LAPS
# ---------------------------------------------------------------------------

def laps_look_like_intervals(laps: list[dict]) -> bool:
    """
    Décide si une liste de laps Strava ressemble à du fractionné structuré
    plutôt qu'à de l'auto-lap kilométrique.

    Heuristiques :
      - assez de laps pour que ce soit une séance (>= 3)
      - durées ou intensités IRRÉGULIÈRES (l'auto-lap km donne des laps
        très réguliers ; le fractionné alterne court/intense et récup)
    laps : liste de dicts avec au moins 'elapsed_time' et si possible
           'average_watts' ou 'average_heartrate'.
    """
    if not laps or len(laps) < 3:
        return False

    durations = [l.get("elapsed_time", 0) for l in laps if l.get("elapsed_time")]
    if len(durations) < 3:
        return False

    # Coefficient de variation des durées : faible => laps réguliers
    # (auto-lap km), élevé => alternance effort/récup.
    m = mean(durations)
    if m == 0:
        return False
    cv_duration = (max(durations) - min(durations)) / m

    # Si on a la puissance, on regarde aussi l'alternance d'intensité.
    powers = [l.get("average_watts") for l in laps if l.get("average_watts")]
    cv_power = 0
    if len(powers) >= 3 and mean(powers) > 0:
        cv_power = (max(powers) - min(powers)) / mean(powers)

    # Seuils volontairement prudents : on veut éviter de prendre de
    # l'auto-lap km pour du fractionné.
    return cv_duration > 0.4 or cv_power > 0.35


def reps_from_laps(laps: list[dict], effort_metric: str = "average_watts") -> list[dict]:
    """
    Transforme des laps en liste de reps d'effort, en retirant les laps
    de récupération (ceux nettement sous la médiane d'intensité).
    effort_metric : 'average_watts' (vélo) ou 'average_speed' (course).
    """
    vals = [l.get(effort_metric, 0) or 0 for l in laps]
    if not any(vals):
        # Pas de métrique d'effort : on renvoie tous les laps tels quels.
        return [_lap_to_rep(l, i) for i, l in enumerate(laps, 1)]

    threshold = median([v for v in vals if v > 0]) * 0.85
    reps = []
    rep_num = 1
    for l in laps:
        if (l.get(effort_metric, 0) or 0) >= threshold:
            reps.append(_lap_to_rep(l, rep_num))
            rep_num += 1
    return reps


def _lap_to_rep(lap: dict, num: int) -> dict:
    return {
        "rep": num,
        "duration_s": lap.get("elapsed_time"),
        "distance_m": lap.get("distance"),
        "avg_power_w": lap.get("average_watts"),
        "avg_hr": lap.get("average_heartrate"),
        "avg_speed_ms": lap.get("average_speed"),
        "source": "lap",
    }


# ---------------------------------------------------------------------------
# 2. DÉTECTION AUTO dans les streams
# ---------------------------------------------------------------------------

def detect_intervals_from_streams(
    time_s: list[int],
    effort: list[float],
    hr: list[float] | None = None,
    min_duration_s: int = 20,
    threshold_ratio: float = 1.15,
) -> list[dict]:
    """
    Détecte les intervalles dans un stream d'effort (puissance ou vitesse).

    Principe : on lisse le signal, on définit un seuil au-dessus de l'effort
    médian, et on repère les blocs continus au-dessus du seuil qui durent
    au moins `min_duration_s`.

    time_s          : timestamps (s) alignés avec effort
    effort          : puissance (vélo) ou vitesse (course), par seconde
    hr              : FC par seconde (optionnel), pour enrichir chaque rep
    min_duration_s  : durée mini pour qu'un bloc compte comme un interval
    threshold_ratio : seuil = médiane * ce ratio
    """
    if not effort or len(effort) < min_duration_s:
        return []

    smoothed = _moving_average(effort, window=5)
    base = median([v for v in smoothed if v and v > 0]) or 0
    if base == 0:
        return []
    threshold = base * threshold_ratio

    reps = []
    in_rep = False
    start_idx = 0
    rep_num = 1

    for i, v in enumerate(smoothed):
        if v >= threshold and not in_rep:
            in_rep, start_idx = True, i
        elif v < threshold and in_rep:
            in_rep = False
            dur = time_s[i - 1] - time_s[start_idx]
            if dur >= min_duration_s:
                reps.append(_build_rep(rep_num, start_idx, i, time_s, effort, hr))
                rep_num += 1

    # Bloc encore ouvert en fin de séance
    if in_rep:
        dur = time_s[-1] - time_s[start_idx]
        if dur >= min_duration_s:
            reps.append(_build_rep(rep_num, start_idx, len(effort), time_s, effort, hr))

    return reps


def _build_rep(num, i0, i1, time_s, effort, hr):
    seg_effort = [e for e in effort[i0:i1] if e is not None]
    rep = {
        "rep": num,
        "duration_s": time_s[i1 - 1] - time_s[i0],
        "avg_effort": round(mean(seg_effort), 1) if seg_effort else None,
        "peak_effort": round(max(seg_effort), 1) if seg_effort else None,
        "source": "auto",
    }
    if hr:
        seg_hr = [h for h in hr[i0:i1] if h is not None]
        if seg_hr:
            rep["avg_hr"] = round(mean(seg_hr))
            rep["max_hr"] = round(max(seg_hr))
    return rep


def _moving_average(data: list[float], window: int = 5) -> list[float]:
    if window <= 1:
        return list(data)
    out, half = [], window // 2
    for i in range(len(data)):
        lo, hi = max(0, i - half), min(len(data), i + half + 1)
        seg = [d for d in data[lo:hi] if d is not None]
        out.append(mean(seg) if seg else 0)
    return out


# ---------------------------------------------------------------------------
# 3. POINT D'ENTRÉE : la cascade
# ---------------------------------------------------------------------------

def extract_intervals(activity: dict, laps: list[dict] | None,
                      streams: dict | None, sport: str = "ride") -> dict:
    """
    Applique la cascade laps -> détection auto et renvoie un résultat
    normalisé prêt pour le coach.

    activity : dict de l'activité (résumé)
    laps     : liste des laps Strava (ou None)
    streams  : dict des streams, ex {'time': [...], 'watts': [...],
               'velocity_smooth': [...], 'heartrate': [...]} (ou None)
    sport    : 'ride' ou 'run' (choisit puissance vs vitesse)
    """
    effort_metric = "average_watts" if sport == "ride" else "average_speed"

    # 1) Laps prioritaires s'ils ressemblent à du fractionné
    if laps and laps_look_like_intervals(laps):
        return {
            "method": "laps",
            "reps": reps_from_laps(laps, effort_metric),
        }

    # 2) Sinon, détection auto dans les streams
    if streams and streams.get("time"):
        effort_stream = (streams.get("watts") if sport == "ride"
                         else streams.get("velocity_smooth"))
        if effort_stream:
            reps = detect_intervals_from_streams(
                time_s=streams["time"],
                effort=effort_stream,
                hr=streams.get("heartrate"),
            )
            return {"method": "auto", "reps": reps}

    # 3) Rien d'exploitable : séance continue, pas de fractionné détecté
    return {"method": "none", "reps": []}
