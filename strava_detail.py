"""
strava_detail.py — Récupération détaillée d'une activité Strava.
================================================================
Tire les laps et les streams (seconde par seconde) d'UNE activité, à la
demande, pour alimenter la détection d'intervalles (intervals.py).

On ne fait PAS ça pour toutes les activités : c'est coûteux en appels API
(Strava limite à ~100 requêtes / 15 min). On le déclenche seulement quand
on veut débriefer une séance précise.

Réutilise rafraichir_token() de app.py — passe le token en argument pour
éviter les imports circulaires.
"""

import requests

STRAVA_API = "https://www.strava.com/api/v3"


def get_laps(activity_id: int, token: str) -> list[dict]:
    """Récupère les laps (tours) d'une activité. Liste vide si aucun."""
    resp = requests.get(
        f"{STRAVA_API}/activities/{activity_id}/laps",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json() or []


def get_streams(activity_id: int, token: str) -> dict:
    """
    Récupère les streams d'une activité et les renvoie sous forme de dict
    de listes alignées : {'time': [...], 'watts': [...], 'heartrate': [...],
    'velocity_smooth': [...], 'cadence': [...], 'altitude': [...]}.

    Les clés absentes (ex : pas de capteur de puissance) sont simplement
    omises. 'time' est toujours présent si l'activité a des streams.
    """
    keys = "time,watts,heartrate,velocity_smooth,cadence,altitude"
    resp = requests.get(
        f"{STRAVA_API}/activities/{activity_id}/streams",
        headers={"Authorization": f"Bearer {token}"},
        params={"keys": keys, "key_by_type": "true"},
        timeout=20,
    )
    if resp.status_code == 404:
        return {}  # pas de streams (activité manuelle, trop ancienne, etc.)
    resp.raise_for_status()
    raw = resp.json()
    # key_by_type=true renvoie {'watts': {'data': [...]}, ...}
    return {k: v["data"] for k, v in raw.items() if "data" in v}


def get_activity_detail(activity_id: int, token: str, sport: str = "ride") -> dict:
    """
    Récupère laps + streams en une passe, prêts pour intervals.extract_intervals.
    Renvoie {'laps': [...], 'streams': {...}}.
    Tolérant aux erreurs : si un des deux appels échoue, on renvoie ce qu'on a.
    """
    laps, streams = [], {}
    try:
        laps = get_laps(activity_id, token)
    except Exception as e:
        print(f"[strava_detail] laps KO pour {activity_id}: {e}")
    try:
        streams = get_streams(activity_id, token)
    except Exception as e:
        print(f"[strava_detail] streams KO pour {activity_id}: {e}")
    return {"laps": laps, "streams": streams}
