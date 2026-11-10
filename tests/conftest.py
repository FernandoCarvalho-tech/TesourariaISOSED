"""Fixtures dos testes de integração.

Requer um PostgreSQL de teste. Defina TEST_DATABASE_URL apontando para um
banco DESCARTÁVEL (as tabelas são truncadas a cada teste), por exemplo:
    TEST_DATABASE_URL=postgresql://postgres@localhost:5432/tesouraria_test
Sem a variável, os testes de integração são pulados (os unitários rodam).
"""
import os
import re
import secrets

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

# O app lê a configuração no import: precisa estar no ambiente antes.
os.environ.setdefault("SECRET_KEY", secrets.token_hex(32))
os.environ["APP_ENV"] = "dev"
os.environ["TRUSTED_PROXIES"] = "0"
if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

SENHA_ADMIN = "Admin-Teste-2026"
SENHA_TESOUREIRO = "Tesoureiro-2026"
_RE_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture(scope="session")
def app_mod():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL não definida — testes de integração pulados.")
    import app as app_mod  # noqa: WPS433 (import tardio proposital)
    app_mod.app.config["TESTING"] = True
    return app_mod


@pytest.fixture()
def banco(app_mod):
    """Zera os dados e cria admin, tesoureiro e contas padrão."""
    from werkzeug.security import generate_password_hash
    import psycopg2

    conn = psycopg2.connect(TEST_DATABASE_URL)
    with conn, conn.cursor() as cur:
        cur.execute("""TRUNCATE fechamentos_log, fechamentos, entradas, saidas, transferencias,
                       dizimo_pastor, membros, membros_pendentes, dizimistas, contas, usuarios
                       RESTART IDENTITY CASCADE""")
        cur.execute(
            "INSERT INTO usuarios (nome, usuario, senha_hash, papel) VALUES (%s,%s,%s,'admin'), (%s,%s,%s,'tesoureiro')",
            ("Admin Teste", "admin", generate_password_hash(SENHA_ADMIN),
             "Tesoureiro Teste", "tesoureiro", generate_password_hash(SENHA_TESOUREIRO)),
        )
        cur.execute("INSERT INTO contas (nome, tipo) VALUES ('Dinheiro','dinheiro'), ('Banco','banco')")
    conn.close()
    app_mod.limiter.reset()
    yield TEST_DATABASE_URL


@pytest.fixture()
def sql(banco):
    """Executa SQL direto no banco de teste (para conferir resultados)."""
    import psycopg2
    import psycopg2.extras

    def _executar(query, params=()):
        conn = psycopg2.connect(banco)
        try:
            with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params)
                return cur.fetchall() if cur.description else None
        finally:
            conn.close()
    return _executar


class ClienteWeb:
    """Cliente de teste que busca o token CSRF como o navegador faria."""

    def __init__(self, client):
        self.client = client

    def token(self, pagina="/login"):
        html = self.client.get(pagina).get_data(as_text=True)
        achado = _RE_CSRF.search(html)
        assert achado, f"token CSRF não encontrado em {pagina}"
        return achado.group(1)

    def post(self, url, dados, pagina_token="/login", **kwargs):
        dados = dict(dados)
        dados["csrf_token"] = self.token(pagina_token)
        return self.client.post(url, data=dados, **kwargs)

    def login(self, usuario="admin", senha=SENHA_ADMIN):
        return self.post("/login", {"usuario": usuario, "senha": senha})


@pytest.fixture()
def web(app_mod, banco):
    return ClienteWeb(app_mod.app.test_client())


@pytest.fixture()
def admin(web):
    resp = web.login()
    assert resp.status_code == 302
    return web


@pytest.fixture()
def tesoureiro(app_mod, banco):
    cliente = ClienteWeb(app_mod.app.test_client())
    resp = cliente.login("tesoureiro", SENHA_TESOUREIRO)
    assert resp.status_code == 302
    return cliente
