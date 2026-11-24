"""
db.py — Persistance Postgres (Railway).
=======================================
Stocke les débriefs post-séance : le ressenti écrit par Olivier + le
débrief généré par le coach, rattachés à un id d'activité Strava.

Railway fournit automatiquement la variable d'environnement DATABASE_URL
quand tu ajoutes un service Postgres à ton projet.

Dépendance à ajouter dans requirements.txt :
  psycopg2-binary
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager


DATABASE_URL = os.environ.get("DATABASE_URL")


@contextmanager
def get_conn():
    """Context manager : ouvre une connexion, la ferme proprement."""
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL absente. Ajoute un service Postgres sur Railway "
            "et lie-le à ton app (la variable est injectée automatiquement)."
        )
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Crée la table debriefs si elle n'existe pas, et migre activity_id en TEXT
    si une ancienne version en BIGINT existe. À appeler au démarrage."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS debriefs (
                    activity_id   TEXT PRIMARY KEY,
                    ressenti      TEXT,
                    debrief       TEXT,
                    activity_date DATE,
                    sport         TEXT,
                    created_at    TIMESTAMPTZ DEFAULT NOW(),
                    updated_at    TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            # Migration : si activity_id est encore en BIGINT (ancienne table),
            # on le convertit en TEXT. Les id entiers existants deviennent leur
            # representation en chaine — le meme identifiant, donc rien n'est perdu.
            cur.execute("""
                SELECT data_type FROM information_schema.columns
                WHERE table_name = 'debriefs' AND column_name = 'activity_id';
            """)
            row = cur.fetchone()
            if row and row[0] != 'text':
                cur.execute(
                    "ALTER TABLE debriefs ALTER COLUMN activity_id TYPE TEXT "
                    "USING activity_id::TEXT;"
                )
                print("[db] migration activity_id -> TEXT effectuée")


def save_ressenti(activity_id: str, ressenti: str,
                  activity_date=None, sport=None):
    """Enregistre (ou met à jour) le ressenti d'une activité.
    Ne touche pas au débrief déjà généré."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO debriefs (activity_id, ressenti, activity_date, sport, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (activity_id) DO UPDATE
                    SET ressenti   = EXCLUDED.ressenti,
                        activity_date = COALESCE(EXCLUDED.activity_date, debriefs.activity_date),
                        sport      = COALESCE(EXCLUDED.sport, debriefs.sport),
                        updated_at = NOW();
            """, (activity_id, ressenti, activity_date, sport))


def save_debrief(activity_id: str, debrief: str):
    """Enregistre le débrief généré par le coach pour une activité."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE debriefs SET debrief = %s, updated_at = NOW()
                WHERE activity_id = %s;
            """, (debrief, activity_id))


def get_debrief(activity_id: str):
    """Retourne le dict {ressenti, debrief, ...} d'une activité, ou None."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM debriefs WHERE activity_id = %s;",
                        (activity_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def get_ressentis_recents(depuis_date) -> list[dict]:
    """
    Retourne les ressentis enregistrés depuis `depuis_date` (incluse), du plus
    récent au plus ancien. Chaque élément : {activity_date, sport, ressenti}.
    Utilisé pour injecter le contexte subjectif dans le bilan hebdo.
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT activity_date, sport, ressenti
                FROM debriefs
                WHERE ressenti IS NOT NULL
                  AND ressenti <> ''
                  AND activity_date >= %s
                ORDER BY activity_date DESC;
            """, (depuis_date,))
            return [dict(r) for r in cur.fetchall()]


def get_debriefs_for(activity_ids: list[str]) -> dict:
    """Récupère en une requête les débriefs de plusieurs activités.
    Retourne un dict {activity_id: {ressenti, debrief}} pour l'affichage liste."""
    if not activity_ids:
        return {}
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT activity_id, ressenti, debrief FROM debriefs "
                "WHERE activity_id = ANY(%s);",
                (activity_ids,)
            )
            return {r["activity_id"]: dict(r) for r in cur.fetchall()}