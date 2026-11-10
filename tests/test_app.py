"""Testes de integração (Flask + PostgreSQL real): segurança e fechamento."""
import io
from decimal import Decimal

import psycopg2
import pytest
from werkzeug.security import generate_password_hash

from tests.conftest import SENHA_ADMIN, SENHA_TESOUREIRO

MES = "2026-10"


def _nova_entrada(cliente, valor="100,10", data=f"{MES}-05", tipo="dizimo"):
    return cliente.post("/entradas/nova", {
        "tipo": tipo, "valor": valor, "conta_id": "1", "data": data, "nome_avulso": "Fulano",
    })


def _efetivar(cliente, senha=SENHA_ADMIN, mes=MES):
    return cliente.post("/fechamento/efetivar", {"mes": mes, "senha": senha, "observacao": "conferido"})


def _reabrir(cliente, senha=SENHA_ADMIN, motivo="Pastor achou oferta não lançada", mes=MES):
    return cliente.post("/fechamento/reabrir", {"mes": mes, "senha": senha, "motivo": motivo})


def _texto_pdf(resp):
    from pypdf import PdfReader
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(resp.data)).pages)


# --------------------------------------------------------------- segurança

def test_cabecalhos_de_seguranca(web):
    resp = web.client.get("/login")
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Cache-Control"] == "no-store"


def test_health_continua_publico(web):
    resp = web.client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_post_sem_csrf_e_rejeitado(admin, sql):
    resp = admin.client.post("/entradas/nova", data={
        "tipo": "dizimo", "valor": "50", "conta_id": "1", "data": f"{MES}-05"})
    assert resp.status_code == 302
    assert sql("SELECT COUNT(*) AS n FROM entradas")[0]["n"] == 0


def test_login_nao_redireciona_para_fora(web):
    resp = web.client.post("/login?next=https://site-malicioso.com/x", data={
        "usuario": "admin", "senha": SENHA_ADMIN, "csrf_token": web.token()})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


def test_login_respeita_next_interno(web):
    resp = web.client.post("/login?next=/extrato", data={
        "usuario": "admin", "senha": SENHA_ADMIN, "csrf_token": web.token()})
    assert resp.headers["Location"] == "/extrato"


def test_limite_de_tentativas_por_usuario(web):
    for _ in range(5):
        assert web.login("admin", "senha-errada").status_code == 200
    # Mesmo com a senha certa, o usuário fica bloqueado até a janela passar.
    assert web.login("admin", SENHA_ADMIN).status_code == 429


def test_senha_fraca_obriga_troca(web, sql):
    sql("UPDATE usuarios SET senha_hash=%s WHERE usuario='admin'", (generate_password_hash("admin123"),))
    resp = web.login("admin", "admin123")
    assert resp.headers["Location"] == "/conta"
    assert web.client.get("/extrato").headers["Location"] == "/conta"
    resp = web.post("/conta", {"senha_atual": "admin123", "nova_senha": "Nova-Senha-Segura",
                               "confirmar_senha": "Nova-Senha-Segura"})
    assert resp.status_code == 302
    assert web.client.get("/extrato").status_code == 200


def test_valor_no_formato_brasileiro(admin, sql):
    _nova_entrada(admin, valor="1.234,56")
    assert sql("SELECT valor FROM entradas")[0]["valor"] == Decimal("1234.56")


def test_valor_ambiguo_e_recusado(admin, sql):
    _nova_entrada(admin, valor="1.234")
    assert sql("SELECT COUNT(*) AS n FROM entradas")[0]["n"] == 0


def test_filtro_de_data_invalido_nao_quebra(admin):
    assert admin.client.get("/extrato?data_ini=2026-99-99&conta_id=abc").status_code == 200
    assert admin.client.get("/fechamento?mes=xx").status_code == 200


# ------------------------------------------------------------- fechamento

def test_efetivar_com_senha_errada_nao_fecha(admin, sql):
    _nova_entrada(admin)
    _efetivar(admin, senha="errada")
    assert sql("SELECT COUNT(*) AS n FROM fechamentos")[0]["n"] == 0


def test_fluxo_previa_efetivar_travar_reabrir(admin, tesoureiro, sql):
    _nova_entrada(admin, valor="100,10")

    # 1) Prévia: PDF marcado e nome do arquivo com _PREVIA
    resp = admin.client.get(f"/fechamento/pdf?mes={MES}")
    assert "_PREVIA" in resp.headers["Content-Disposition"]
    assert "PRÉVIA" in _texto_pdf(resp)

    # 2) Efetivar congela os totais com arredondamento em centavos
    _efetivar(admin)
    fech = sql("SELECT status, totais FROM fechamentos WHERE mes=%s", (MES,))[0]
    assert fech["status"] == "fechado"
    assert fech["totais"]["total_bruto"] == "100.10"
    assert fech["totais"]["valor_sede"] == "15.02"
    assert fech["totais"]["valor_regional"] == "5.01"

    resp = admin.client.get(f"/fechamento/pdf?mes={MES}")
    assert "_PREVIA" not in resp.headers["Content-Disposition"]
    texto = _texto_pdf(resp)
    assert "FECHAMENTO EFETIVADO" in texto and "PRÉVIA PARA" not in texto

    # 3) Mês travado: incluir, editar, excluir e dízimo do pastor são barrados
    entrada_id = sql("SELECT id FROM entradas")[0]["id"]
    _nova_entrada(tesoureiro, valor="50")
    admin.post(f"/entradas/{entrada_id}/editar", {
        "tipo": "dizimo", "valor": "999", "conta_id": "1", "data": f"{MES}-05"})
    admin.post(f"/entradas/{entrada_id}/excluir", {})
    admin.post("/fechamento/dizimo-pastor", {"mes": MES, "valor": "300"})
    assert sql("SELECT COUNT(*) AS n, SUM(valor) AS s FROM entradas")[0] == {"n": 1, "s": Decimal("100.10")}
    assert sql("SELECT COUNT(*) AS n FROM dizimo_pastor")[0]["n"] == 0
    # ...mas o mês seguinte continua livre
    _nova_entrada(tesoureiro, valor="50", data="2026-11-02")
    assert sql("SELECT COUNT(*) AS n FROM entradas")[0]["n"] == 2

    # Extrato mostra o cadeado no lugar dos botões
    html = admin.client.get("/extrato").get_data(as_text=True)
    assert "Mês fechado" in html

    # 4) Reabrir: tesoureiro não pode; admin precisa de motivo e senha
    _reabrir(tesoureiro, senha=SENHA_TESOUREIRO)
    _reabrir(admin, motivo="curto")
    _reabrir(admin, senha="errada")
    assert sql("SELECT status FROM fechamentos WHERE mes=%s", (MES,))[0]["status"] == "fechado"

    _reabrir(admin)
    assert sql("SELECT status FROM fechamentos WHERE mes=%s", (MES,))[0]["status"] == "reaberto"
    log = sql("SELECT acao, motivo FROM fechamentos_log WHERE mes=%s ORDER BY id", (MES,))
    assert [l["acao"] for l in log] == ["efetivar", "reabrir"]
    assert log[1]["motivo"] == "Pastor achou oferta não lançada"

    # 5) Reaberto aceita a correção e pode ser efetivado de novo
    _nova_entrada(tesoureiro, valor="50")
    _efetivar(tesoureiro, senha=SENHA_TESOUREIRO)
    fech = sql("SELECT status, totais FROM fechamentos WHERE mes=%s", (MES,))[0]
    assert fech["status"] == "fechado" and fech["totais"]["total_bruto"] == "150.10"
    html = admin.client.get(f"/fechamento?mes={MES}").get_data(as_text=True)
    assert "Histórico do mês" in html and "Reabrir este mês" in html


def test_trigger_do_banco_barra_escrita_direta(admin, sql):
    _nova_entrada(admin)
    _efetivar(admin)
    with pytest.raises(psycopg2.Error, match="MES_FECHADO"):
        sql("INSERT INTO entradas (tipo, valor, conta_id, data, usuario_id) VALUES ('dizimo', 1, 1, %s, 1)",
            (f"{MES}-20",))
    with pytest.raises(psycopg2.Error, match="MES_FECHADO"):
        sql("DELETE FROM entradas")


def test_pdf_varios_meses_mistura_previa_e_oficial(admin):
    _nova_entrada(admin)
    _efetivar(admin)
    resp = admin.client.get(f"/fechamento/pdf-multiplo?mes_ini={MES}&mes_fim=2026-11")
    texto = _texto_pdf(resp)
    assert "FECHAMENTO EFETIVADO" in texto
    assert "PRÉVIA PARA CONFERÊNCIA" in texto


def test_visualizador_nao_ve_formularios_de_fechamento(app_mod, sql):
    sql("INSERT INTO usuarios (nome, usuario, senha_hash, papel) VALUES ('Vis','vis',%s,'visualizador')",
        (generate_password_hash("Visualiza-2026"),))
    from tests.conftest import ClienteWeb
    vis = ClienteWeb(app_mod.app.test_client())
    vis.login("vis", "Visualiza-2026")
    html = vis.client.get(f"/fechamento?mes={MES}").get_data(as_text=True)
    assert "Efetivar fechamento" not in html


# ------------------------------------------------------- cadastro público

def _form_cadastro(**sobrescreve):
    base = dict(nome="Maria da Silva", sexo="Feminino", data_nascimento="1990-05-10",
                naturalidade="Dois Vizinhos", estado_civil="Casado(a)", fone="(46) 99999-0000",
                endereco="Rua A", numero="10", bairro="Centro", cargo_igreja="Membro", funcao="Louvor")
    base.update(sobrescreve)
    return base


def test_cadastro_invalido_mantem_o_que_foi_digitado(web, sql):
    resp = web.post("/cadastro", _form_cadastro(cpf="123.456.789-00"), pagina_token="/cadastro")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 400
    assert "CPF inválido" in html
    assert 'value="Maria da Silva"' in html
    assert sql("SELECT COUNT(*) AS n FROM membros_pendentes")[0]["n"] == 0


def test_cadastro_valido_grava_cpf_formatado(web, sql):
    resp = web.post("/cadastro", _form_cadastro(cpf="52998224725"), pagina_token="/cadastro")
    assert resp.status_code == 200
    assert sql("SELECT cpf FROM membros_pendentes")[0]["cpf"] == "529.982.247-25"


def test_cadastro_armadilha_descarta_robo(web, sql):
    resp = web.post("/cadastro", _form_cadastro(website="http://spam"), pagina_token="/cadastro")
    assert resp.status_code == 200
    assert sql("SELECT COUNT(*) AS n FROM membros_pendentes")[0]["n"] == 0


def test_nome_com_script_nao_vira_codigo_na_tela_de_pendentes(web, admin, sql):
    nome = "x');alert(1);('"
    web.post("/cadastro", _form_cadastro(nome=nome), pagina_token="/cadastro")
    html = admin.client.get("/admin/membros/pendentes").get_data(as_text=True)
    assert "confirm('Rejeitar o cadastro de ' + this.dataset.nome" in html
    assert "alert(1);('?')" not in html


def test_limite_de_tentativas_na_confirmacao_de_senha(admin, sql):
    _nova_entrada(admin)
    for _ in range(10):
        assert _efetivar(admin, senha="errada").status_code == 302
    assert _efetivar(admin, senha=SENHA_ADMIN).status_code == 429
    assert sql("SELECT COUNT(*) AS n FROM fechamentos")[0]["n"] == 0
