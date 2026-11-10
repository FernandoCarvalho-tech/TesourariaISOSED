"""Acesso ao banco (PostgreSQL) e criação/migração do schema.

Objetivo : conexão com API estilo sqlite ('?' e lastrowid) usada pelo app,
           fechamento garantido das conexões ao fim de cada requisição e
           criação idempotente do schema (tabelas, colunas e triggers).
Entradas : DATABASE_URL; opcionalmente ADMIN_INICIAL_USUARIO/ADMIN_INICIAL_SENHA
           (só usadas se o banco não tiver nenhum usuário).
Versão   : 1.1 (2026-11) — remove o admin padrão admin/admin123, adiciona
           fechamentos + histórico e triggers que travam meses efetivados.
"""
import logging
import os

import psycopg2
import psycopg2.extras
from flask import g, has_app_context
from werkzeug.security import generate_password_hash

log = logging.getLogger("tesouraria.db")

DATABASE_URL = os.environ.get("DATABASE_URL")

# Chave do advisory lock que serializa o init_db quando vários workers sobem juntos.
_LOCK_INIT_DB = 7_310_001


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
    (using '?' placeholders and dict-like rows) so the rest of the app can stay unchanged.

    Atenção: o '?' é trocado por '%s' no texto inteiro da query. Não use '?'
    nem '%' literais no SQL — passe valores sempre como parâmetro.
    """

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, query, params=()):
        pg_query = query.replace("?", "%s")
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Só emula lastrowid quando a própria query não traz RETURNING
        # (se trouxer, quem chamou lê o resultado com fetchone()).
        emula_lastrowid = (pg_query.strip().upper().startswith("INSERT")
                           and "RETURNING" not in pg_query.upper())
        if emula_lastrowid:
            pg_query += " RETURNING id"
        cur.execute(pg_query, params)
        lastrowid = None
        if emula_lastrowid:
            row = cur.fetchone()
            lastrowid = row["id"] if row else None
        return _PGCursor(cur, lastrowid)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def get_conn():
    raw_conn = psycopg2.connect(DATABASE_URL)
    if has_app_context():
        # Registra para o teardown fechar mesmo se a rota levantar exceção
        # antes do conn.close() (antes a conexão ficava aberta até o GC).
        g.setdefault("_conexoes_abertas", []).append(raw_conn)
    return _PGConnection(raw_conn)


def fechar_conexoes_pendentes(_exc=None) -> None:
    """Teardown: desfaz transação pendente e fecha conexões esquecidas."""
    for raw_conn in g.pop("_conexoes_abertas", []):
        if raw_conn.closed:
            continue
        try:
            raw_conn.rollback()
        except psycopg2.Error:
            pass
        finally:
            raw_conn.close()


HEALTHCHECK_TIMEOUT_S = int(os.environ.get("HEALTHCHECK_TIMEOUT_S", "5"))


def check_db() -> None:
    """Verifica se o banco responde, com timeout curto.

    Usado pela rota /health (monitor externo, ex.: UptimeRobot). Faz um
    SELECT 1 real, para que o monitor detecte banco pausado/fora do ar e, de
    quebra, gere atividade no Supabase free (que pausa por inatividade).
    Levanta psycopg2.Error se não conectar ou não responder.
    """
    raw_conn = psycopg2.connect(DATABASE_URL, connect_timeout=HEALTHCHECK_TIMEOUT_S)
    try:
        with raw_conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    finally:
        raw_conn.close()


def _criar_admin_inicial(cur) -> None:
    """Cria o primeiro admin SOMENTE se o banco não tiver nenhum usuário.

    Não existe mais senha padrão no código: a senha vem de variável de
    ambiente e deve ser removida do Render depois do primeiro acesso.
    """
    cur.execute("SELECT 1 FROM usuarios LIMIT 1")
    if cur.fetchone():
        return
    usuario = os.environ.get("ADMIN_INICIAL_USUARIO", "").strip()
    senha = os.environ.get("ADMIN_INICIAL_SENHA", "")
    if not usuario or len(senha) < 8:
        log.warning("Banco sem usuários: defina ADMIN_INICIAL_USUARIO e ADMIN_INICIAL_SENHA "
                    "(mínimo 8 caracteres) e reinicie para criar o primeiro administrador.")
        return
    cur.execute(
        "INSERT INTO usuarios (nome, usuario, senha_hash, papel) VALUES (%s, %s, %s, %s)",
        ("Administrador", usuario, generate_password_hash(senha), "admin"),
    )
    log.info("Administrador inicial criado (usuario=%s).", usuario)


# Triggers que barram INSERT/UPDATE/DELETE em mês efetivado. É a segunda
# camada da trava (a primeira, com mensagem amigável, está no app).
_SQL_TRAVA_MES = """
CREATE OR REPLACE FUNCTION fn_trava_mes_fechado() RETURNS trigger AS $$
DECLARE
    mes_ref TEXT;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        mes_ref := to_char(OLD.data, 'YYYY-MM');
        IF EXISTS (SELECT 1 FROM fechamentos WHERE mes = mes_ref AND status = 'fechado') THEN
            RAISE EXCEPTION 'MES_FECHADO:%', mes_ref;
        END IF;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        mes_ref := to_char(NEW.data, 'YYYY-MM');
        IF EXISTS (SELECT 1 FROM fechamentos WHERE mes = mes_ref AND status = 'fechado') THEN
            RAISE EXCEPTION 'MES_FECHADO:%', mes_ref;
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fn_trava_mes_fechado_dizimo_pastor() RETURNS trigger AS $$
DECLARE
    mes_ref TEXT;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        mes_ref := OLD.mes;
        IF EXISTS (SELECT 1 FROM fechamentos WHERE mes = mes_ref AND status = 'fechado') THEN
            RAISE EXCEPTION 'MES_FECHADO:%', mes_ref;
        END IF;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        mes_ref := NEW.mes;
        IF EXISTS (SELECT 1 FROM fechamentos WHERE mes = mes_ref AND status = 'fechado') THEN
            RAISE EXCEPTION 'MES_FECHADO:%', mes_ref;
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_trava_mes_entradas ON entradas;
CREATE TRIGGER trg_trava_mes_entradas BEFORE INSERT OR UPDATE OR DELETE ON entradas
    FOR EACH ROW EXECUTE FUNCTION fn_trava_mes_fechado();

DROP TRIGGER IF EXISTS trg_trava_mes_saidas ON saidas;
CREATE TRIGGER trg_trava_mes_saidas BEFORE INSERT OR UPDATE OR DELETE ON saidas
    FOR EACH ROW EXECUTE FUNCTION fn_trava_mes_fechado();

DROP TRIGGER IF EXISTS trg_trava_mes_transferencias ON transferencias;
CREATE TRIGGER trg_trava_mes_transferencias BEFORE INSERT OR UPDATE OR DELETE ON transferencias
    FOR EACH ROW EXECUTE FUNCTION fn_trava_mes_fechado();

DROP TRIGGER IF EXISTS trg_trava_mes_dizimo_pastor ON dizimo_pastor;
CREATE TRIGGER trg_trava_mes_dizimo_pastor BEFORE INSERT OR UPDATE OR DELETE ON dizimo_pastor
    FOR EACH ROW EXECUTE FUNCTION fn_trava_mes_fechado_dizimo_pastor();
"""


def init_db():
    conn = get_conn()
    cur = conn._conn.cursor()
    # Serializa a migração entre workers/instâncias que sobem ao mesmo tempo.
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_INIT_DB,))

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
    _criar_admin_inicial(cur)

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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS dizimo_pastor (
            id SERIAL PRIMARY KEY,
            mes TEXT NOT NULL UNIQUE,
            valor NUMERIC(12,2) NOT NULL DEFAULT 0,
            observacao TEXT,
            usuario_id INTEGER REFERENCES usuarios (id),
            criado_em TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS membros (
            id SERIAL PRIMARY KEY,
            dizimista_id INTEGER REFERENCES dizimistas (id),
            nome TEXT NOT NULL,
            sexo TEXT,
            endereco TEXT,
            numero TEXT,
            bairro TEXT,
            cep TEXT,
            naturalidade TEXT,
            data_nascimento DATE,
            data_batizo DATE,
            fone TEXT,
            profissao TEXT,
            rg TEXT,
            cpf TEXT,
            estado_civil TEXT,
            nome_conjuge TEXT,
            cargo_igreja TEXT,
            funcao TEXT,
            ativo BOOLEAN NOT NULL DEFAULT TRUE,
            criado_em TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS membros_pendentes (
            id SERIAL PRIMARY KEY,
            nome TEXT NOT NULL,
            sexo TEXT,
            endereco TEXT,
            numero TEXT,
            bairro TEXT,
            cep TEXT,
            naturalidade TEXT,
            data_nascimento DATE,
            data_batizo DATE,
            fone TEXT,
            profissao TEXT,
            rg TEXT,
            cpf TEXT,
            estado_civil TEXT,
            nome_conjuge TEXT,
            cargo_igreja TEXT,
            funcao TEXT,
            status TEXT NOT NULL DEFAULT 'pendente',
            criado_em TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    cur.execute("ALTER TABLE membros ADD COLUMN IF NOT EXISTS conjuge_membro_id INTEGER REFERENCES membros(id)")
    cur.execute("ALTER TABLE membros ADD COLUMN IF NOT EXISTS dizimista_casal_id INTEGER REFERENCES dizimistas(id)")

    # Fechamento mensal efetivado (um registro por mês) e histórico de ações.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fechamentos (
            mes TEXT PRIMARY KEY CHECK (mes ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
            status TEXT NOT NULL CHECK (status IN ('fechado', 'reaberto')),
            totais JSONB,
            fechado_em TIMESTAMP,
            fechado_por INTEGER REFERENCES usuarios (id),
            atualizado_em TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fechamentos_log (
            id SERIAL PRIMARY KEY,
            mes TEXT NOT NULL,
            acao TEXT NOT NULL CHECK (acao IN ('efetivar', 'reabrir')),
            motivo TEXT,
            usuario_id INTEGER NOT NULL REFERENCES usuarios (id),
            ip TEXT,
            totais JSONB,
            criado_em TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_fechamentos_log_mes ON fechamentos_log (mes, criado_em)")
    cur.execute(_SQL_TRAVA_MES)

    conn.commit()
    conn.close()
