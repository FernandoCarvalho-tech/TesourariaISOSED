import os
import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash

DATABASE_URL = os.environ.get("DATABASE_URL")


class _PGCursor:
    """Wraps a psycopg2 cursor to expose sqlite3-style .lastrowid."""

    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class _PGConnection:
    """Wraps a psycopg2 connection to expose a sqlite3-style .execute() API
    (using '?' placeholders and dict-like rows) so the rest of the app can stay unchanged."""

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, query, params=()):
        pg_query = query.replace("?", "%s")
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        is_insert = pg_query.strip().upper().startswith("INSERT")
        if is_insert and "RETURNING" not in pg_query.upper():
            pg_query += " RETURNING id"
        cur.execute(pg_query, params)
        lastrowid = None
        if is_insert:
            row = cur.fetchone()
            lastrowid = row["id"] if row else None
        return _PGCursor(cur, lastrowid)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_conn():
    raw_conn = psycopg2.connect(DATABASE_URL)
    return _PGConnection(raw_conn)


def init_db():
    conn = get_conn()
    cur = conn._conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id SERIAL PRIMARY KEY,
            nome TEXT NOT NULL,
            usuario TEXT UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL,
            papel TEXT NOT NULL CHECK (papel IN ('admin', 'tesoureiro', 'visualizador')),
            ativo BOOLEAN NOT NULL DEFAULT TRUE,
            criado_em TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("SELECT 1 FROM usuarios WHERE usuario = %s", ("admin",))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO usuarios (nome, usuario, senha_hash, papel) VALUES (%s, %s, %s, %s)",
            ("Administrador", "admin", generate_password_hash("admin123"), "admin"),
        )

    cur.execute("""
        CREATE TABLE IF NOT EXISTS contas (
            id SERIAL PRIMARY KEY,
            nome TEXT NOT NULL,
            tipo TEXT NOT NULL CHECK (tipo IN ('dinheiro', 'moedas', 'banco', 'outro')),
            ativo BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)
    cur.execute("SELECT 1 FROM contas")
    if not cur.fetchone():
        for nome, tipo in [
            ("Dinheiro", "dinheiro"),
            ("Cofrinho (moedas)", "moedas"),
            ("Banco", "banco"),
        ]:
            cur.execute(
                "INSERT INTO contas (nome, tipo) VALUES (%s, %s)", (nome, tipo)
            )

    cur.execute("""
        CREATE TABLE IF NOT EXISTS dizimistas (
            id SERIAL PRIMARY KEY,
            nome TEXT NOT NULL,
            ativo BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS entradas (
            id SERIAL PRIMARY KEY,
            tipo TEXT NOT NULL CHECK (tipo IN ('dizimo', 'oferta_nominal', 'oferta_coletiva')),
            dizimista_id INTEGER REFERENCES dizimistas (id),
            nome_avulso TEXT,
            valor NUMERIC(12, 2) NOT NULL,
            conta_id INTEGER NOT NULL REFERENCES contas (id),
            data DATE NOT NULL,
            observacao TEXT,
            usuario_id INTEGER NOT NULL REFERENCES usuarios (id),
            criado_em TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS saidas (
            id SERIAL PRIMARY KEY,
            categoria TEXT NOT NULL DEFAULT 'OUTROS',
            motivo TEXT NOT NULL,
            valor NUMERIC(12, 2) NOT NULL,
            conta_id INTEGER NOT NULL REFERENCES contas (id),
            data DATE NOT NULL,
            observacao TEXT,
            usuario_id INTEGER NOT NULL REFERENCES usuarios (id),
            criado_em TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("ALTER TABLE saidas ADD COLUMN IF NOT EXISTS categoria TEXT NOT NULL DEFAULT 'OUTROS'")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transferencias (
            id SERIAL PRIMARY KEY,
            conta_origem_id INTEGER NOT NULL REFERENCES contas (id),
            conta_destino_id INTEGER NOT NULL REFERENCES contas (id),
            valor NUMERIC(12, 2) NOT NULL,
            data DATE NOT NULL,
            motivo TEXT,
            usuario_id INTEGER NOT NULL REFERENCES usuarios (id),
            criado_em TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    conn.commit()
    conn.close()
