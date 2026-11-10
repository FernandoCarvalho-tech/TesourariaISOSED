"""Validação e normalização de entradas externas (formulários e parâmetros).

Objetivo : funções puras, sem Flask nem banco, para validar o que chega do
           usuário antes de gravar. Testáveis isoladamente (tests/test_validacao.py).
Versão   : 1.0 (2026-11)
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Mapping, Optional

CENTAVO = Decimal("0.01")
VALOR_MAXIMO = Decimal("9999999999.99")  # limite de NUMERIC(12,2)
_RE_MES = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

ESTADOS_CIVIS = ["Solteiro(a)", "Casado(a)", "Divorciado(a)", "Viúvo(a)", "Separado(a)", "União Estável"]
SEXOS = ["Masculino", "Feminino"]


def mes_valido(valor: Optional[str]) -> Optional[str]:
    """Retorna 'YYYY-MM' se válido, senão None."""
    valor = (valor or "").strip()
    return valor if _RE_MES.match(valor) else None


def mes_de(data_ref: date) -> str:
    return f"{data_ref.year:04d}-{data_ref.month:02d}"


def ler_data(valor: Optional[str]) -> Optional[date]:
    """Converte 'YYYY-MM-DD' em date; None se vazio ou inválido."""
    valor = (valor or "").strip()
    if not valor:
        return None
    try:
        return date.fromisoformat(valor)
    except ValueError:
        return None


def ler_valor(valor: Optional[str]) -> Optional[Decimal]:
    """Converte texto monetário ('1.234,56', '1234.56', '10') em Decimal > 0.

    Retorna None se vazio, inválido, <= 0, acima do limite da coluna ou com
    mais de 2 casas decimais.
    """
    texto = (valor or "").strip().replace(" ", "")
    if not texto:
        return None
    if "," in texto:
        # Formato brasileiro: ponto é milhar, vírgula é decimal.
        texto = texto.replace(".", "").replace(",", ".")
    try:
        numero = Decimal(texto)
    except InvalidOperation:
        return None
    if not numero.is_finite() or numero <= 0 or numero > VALOR_MAXIMO:
        return None
    if numero != numero.quantize(CENTAVO):
        return None
    return numero.quantize(CENTAVO)


def arredondar(valor: Decimal) -> Decimal:
    """Arredondamento monetário padrão (meio para cima) em centavos."""
    return valor.quantize(CENTAVO, rounding=ROUND_HALF_UP)


def somente_digitos(valor: Optional[str]) -> str:
    return re.sub(r"\D", "", valor or "")


def cpf_valido(cpf: Optional[str]) -> bool:
    """Valida os dígitos verificadores do CPF (aceita com ou sem máscara)."""
    numeros = somente_digitos(cpf)
    if len(numeros) != 11 or numeros == numeros[0] * 11:
        return False
    for posicao in (9, 10):
        soma = sum(int(numeros[i]) * (posicao + 1 - i) for i in range(posicao))
        digito = (soma * 10) % 11 % 10
        if digito != int(numeros[posicao]):
            return False
    return True


def formatar_cpf(cpf: str) -> str:
    n = somente_digitos(cpf)
    return f"{n[:3]}.{n[3:6]}.{n[6:9]}-{n[9:]}"


def mascarar_cpf(cpf: Optional[str]) -> str:
    """Para telas/logs: mostra só os 2 últimos dígitos (LGPD)."""
    n = somente_digitos(cpf)
    return f"***.***.***-{n[-2:]}" if len(n) == 11 else ""


# Campos do cadastro público: (nome, obrigatório, tamanho máximo)
CAMPOS_CADASTRO: list[tuple[str, bool, int]] = [
    ("nome", True, 200),
    ("sexo", True, 20),
    ("data_nascimento", True, 10),
    ("data_batizo", False, 10),
    ("naturalidade", True, 100),
    ("estado_civil", True, 30),
    ("nome_conjuge", False, 200),
    ("rg", False, 20),
    ("cpf", False, 14),
    ("profissao", False, 100),
    ("fone", True, 20),
    ("endereco", True, 200),
    ("numero", True, 10),
    ("cep", False, 9),
    ("bairro", True, 100),
    ("cargo_igreja", True, 100),
    ("funcao", True, 100),
]

_ROTULOS = {
    "nome": "Nome completo", "sexo": "Sexo", "data_nascimento": "Data de nascimento",
    "data_batizo": "Data do batismo", "naturalidade": "Naturalidade",
    "estado_civil": "Estado civil", "nome_conjuge": "Nome do cônjuge", "rg": "RG",
    "cpf": "CPF", "profissao": "Profissão", "fone": "Telefone", "endereco": "Endereço",
    "numero": "Número", "cep": "CEP", "bairro": "Bairro", "cargo_igreja": "Cargo na igreja",
    "funcao": "Função",
}


def validar_cadastro_membro(form: Mapping[str, str], hoje: date) -> tuple[dict, list[str]]:
    """Valida o formulário público de cadastro de membro.

    Retorna (dados_normalizados, erros). Se houver erros, os dados servem
    para re-preencher o formulário sem o usuário perder o que digitou.
    """
    dados: dict[str, Optional[str]] = {}
    erros: list[str] = []

    for campo, obrigatorio, tamanho in CAMPOS_CADASTRO:
        valor = (form.get(campo) or "").strip()
        if len(valor) > tamanho:
            erros.append(f"{_ROTULOS[campo]}: máximo de {tamanho} caracteres.")
            valor = valor[:tamanho]
        if obrigatorio and not valor:
            erros.append(f"{_ROTULOS[campo]} é obrigatório.")
        dados[campo] = valor or None

    if dados["sexo"] and dados["sexo"] not in SEXOS:
        erros.append("Sexo inválido.")
    if dados["estado_civil"] and dados["estado_civil"] not in ESTADOS_CIVIS:
        erros.append("Estado civil inválido.")

    nascimento = ler_data(dados["data_nascimento"])
    if dados["data_nascimento"] and (nascimento is None or not date(1900, 1, 1) <= nascimento <= hoje):
        erros.append("Data de nascimento inválida.")
    batismo = ler_data(dados["data_batizo"])
    if dados["data_batizo"]:
        if batismo is None or batismo > hoje:
            erros.append("Data do batismo inválida.")
        elif nascimento and batismo < nascimento:
            erros.append("Data do batismo não pode ser anterior ao nascimento.")

    if dados["cpf"]:
        if cpf_valido(dados["cpf"]):
            dados["cpf"] = formatar_cpf(dados["cpf"])
        else:
            erros.append("CPF inválido. Confira os números ou deixe em branco.")

    if dados["cep"]:
        cep = somente_digitos(dados["cep"])
        if len(cep) != 8:
            erros.append("CEP deve ter 8 dígitos.")
        else:
            dados["cep"] = f"{cep[:5]}-{cep[5:]}"

    if dados["fone"] and len(somente_digitos(dados["fone"])) not in (10, 11):
        erros.append("Telefone deve ter DDD + número (10 ou 11 dígitos).")

    return dados, erros
