"""Testes unitários das regras puras (não precisam de banco)."""
from datetime import date
from decimal import Decimal

import pytest

from config import ConfiguracaoInvalida, carregar_config
from fechamentos import divergencias, validar_motivo
from seguranca import senha_e_fraca, validar_nova_senha
from validacao import (
    arredondar, cpf_valido, formatar_cpf, ler_data, ler_valor, mascarar_cpf, mes_valido,
    validar_cadastro_membro,
)

CPF_VALIDO = "529.982.247-25"


@pytest.mark.parametrize("cpf,esperado", [
    (CPF_VALIDO, True),
    ("52998224725", True),
    ("529.982.247-24", False),   # dígito errado
    ("111.111.111-11", False),   # sequência repetida
    ("123", False),
    ("", False),
    (None, False),
])
def test_cpf_valido(cpf, esperado):
    assert cpf_valido(cpf) is esperado


def test_formatar_e_mascarar_cpf():
    assert formatar_cpf("52998224725") == CPF_VALIDO
    assert mascarar_cpf(CPF_VALIDO) == "***.***.***-25"
    assert mascarar_cpf("") == ""


@pytest.mark.parametrize("texto,esperado", [
    ("10", Decimal("10.00")),
    ("10,5", Decimal("10.50")),
    ("1.234,56", Decimal("1234.56")),
    ("1234.56", Decimal("1234.56")),
    ("0", None),
    ("-5", None),
    ("abc", None),
    ("", None),
    ("1.234", None),          # ambíguo: 3 casas decimais — antes virava R$ 1,23
    ("10,555", None),
    ("NaN", None),
    ("99999999999", None),    # acima de NUMERIC(12,2)
])
def test_ler_valor(texto, esperado):
    assert ler_valor(texto) == esperado


def test_arredondamento_meio_para_cima():
    bruto = Decimal("100.10")
    assert arredondar(bruto * Decimal("0.15")) == Decimal("15.02")   # 15.015
    assert arredondar(bruto * Decimal("0.05")) == Decimal("5.01")    # 5.005
    assert arredondar(bruto * Decimal("0.03")) == Decimal("3.00")    # 3.003


def test_mes_e_data():
    assert mes_valido("2026-10") == "2026-10"
    assert mes_valido("2026-13") is None
    assert mes_valido("2026-1") is None
    assert mes_valido("'; drop table") is None
    assert ler_data("2026-02-30") is None
    assert ler_data("2026-02-28") == date(2026, 2, 28)


def _form_cadastro(**sobrescreve):
    base = dict(nome="Maria da Silva", sexo="Feminino", data_nascimento="1990-05-10",
                naturalidade="Dois Vizinhos", estado_civil="Casado(a)", fone="(46) 99999-0000",
                endereco="Rua A", numero="10", bairro="Centro", cargo_igreja="Membro", funcao="Louvor")
    base.update(sobrescreve)
    return base


def test_cadastro_valido_normaliza_cpf_e_cep():
    dados, erros = validar_cadastro_membro(_form_cadastro(cpf="52998224725", cep="85660000"), date(2026, 11, 9))
    assert erros == []
    assert dados["cpf"] == CPF_VALIDO
    assert dados["cep"] == "85660-000"


def test_cadastro_rejeita_dados_invalidos():
    form = _form_cadastro(cpf="123.456.789-00", data_nascimento="2030-01-01", sexo="X",
                          data_batizo="1980-01-01", fone="123", nome="")
    dados, erros = validar_cadastro_membro(form, date(2026, 11, 9))
    texto = " ".join(erros)
    assert "CPF inválido" in texto
    assert "Data de nascimento inválida" in texto
    assert "Sexo inválido" in texto
    assert "Telefone" in texto
    assert "Nome completo é obrigatório" in texto
    assert dados["naturalidade"] == "Dois Vizinhos"  # mantém o que foi digitado


def test_cadastro_batismo_antes_do_nascimento():
    _, erros = validar_cadastro_membro(_form_cadastro(data_batizo="1980-01-01"), date(2026, 11, 9))
    assert any("anterior ao nascimento" in e for e in erros)


def test_politica_de_senha():
    assert senha_e_fraca("admin123")
    assert senha_e_fraca("curta")
    assert not senha_e_fraca("Uma-Senha-Boa")
    assert validar_nova_senha("Uma-Senha-Boa", "fernando", "Uma-Senha-Boa") == []
    assert validar_nova_senha("fernando1", "fernando1")  # igual ao usuário
    assert validar_nova_senha("11111111", "x")           # repetição
    assert validar_nova_senha("Uma-Senha-Boa", "x", "Outra-Coisa-1")  # confirmação


def test_secret_key_obrigatoria():
    with pytest.raises(ConfiguracaoInvalida):
        carregar_config({})
    with pytest.raises(ConfiguracaoInvalida):
        carregar_config({"SECRET_KEY": "curta"})
    config = carregar_config({"SECRET_KEY": "x" * 64})
    assert config["SESSION_COOKIE_SECURE"] is True
    assert config["SESSION_COOKIE_HTTPONLY"] is True


def test_turnstile_exige_as_duas_chaves():
    with pytest.raises(ConfiguracaoInvalida):
        carregar_config({"SECRET_KEY": "x" * 64, "TURNSTILE_SITE_KEY": "abc"})


def test_motivo_reabertura():
    assert validar_motivo("curto")
    assert validar_motivo("x" * 501)
    assert validar_motivo("Pastor achou oferta não lançada") is None


def test_divergencias_ignora_saldo_acumulado():
    antes = {"total_bruto": "100.00", "saldo_acumulado": "500.00", "qtd_entradas": "3"}
    depois = {"total_bruto": "100.00", "saldo_acumulado": "800.00", "qtd_entradas": "3"}
    assert divergencias(antes, depois) == []
    depois["total_bruto"] = "120.00"
    assert divergencias(antes, depois) == ["total_bruto"]
