"""Regras do fechamento mensal: prévia, efetivação, trava e reabertura.

Fluxo:
  1. Mês em ABERTO  -> o PDF sai como PRÉVIA (para o pastor conferir).
  2. EFETIVAR       -> exige a senha de quem efetiva; congela os totais
                       (snapshot) e TRAVA o mês: nenhum lançamento com data
                       naquele mês pode ser criado, editado ou excluído.
  3. REABRIR        -> só admin, exige senha + motivo; fica registrado no
                       histórico (fechamentos_log) com usuário, data e IP.

A trava existe em duas camadas: aqui (mensagem amigável) e em triggers no
banco (db.py), que barram qualquer escrita mesmo por outro caminho.

Versão: 1.0 (2026-11)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional, Union

from validacao import mes_de

log = logging.getLogger("tesouraria.fechamento")

# Datas do fechamento gravadas no horário de Brasília (o Postgres do
# Supabase roda em UTC; sem isso o "efetivado em" sairia 3h adiantado).
FUSO = os.environ.get("APP_TIMEZONE", "America/Sao_Paulo")

STATUS_FECHADO = "fechado"
STATUS_REABERTO = "reaberto"
TAMANHO_MINIMO_MOTIVO = 10
TAMANHO_MAXIMO_MOTIVO = 500

# Totais do mês que entram no snapshot e na conferência de divergência.
# O saldo acumulado fica de fora da conferência porque depende de meses
# anteriores que podem continuar abertos.
CHAVES_CONFERENCIA = (
    "dizimos", "ofertas_nominais", "ofertas_coletivas", "total_bruto", "total_saidas",
    "valor_sede", "valor_fundo", "valor_regional", "dizimo_pastor", "total_enviado_sede",
    "qtd_entradas", "qtd_saidas",
)


class MesFechadoError(Exception):
    """Tentativa de alterar lançamento de um mês já efetivado."""

    def __init__(self, mes: str):
        super().__init__(mes)
        self.mes = mes

    @property
    def mensagem(self) -> str:
        return (f"O mês {formatar_mes(self.mes)} já foi efetivado e está travado. "
                "Para alterar, um administrador precisa reabri-lo na tela de Fechamento.")


def formatar_mes(mes: str) -> str:
    return f"{mes[5:7]}/{mes[:4]}"


def obter_situacao(conn, mes: str) -> Optional[dict]:
    """Situação do mês (None = nunca foi efetivado)."""
    row = conn.execute(
        """SELECT f.mes, f.status, f.totais, f.fechado_em, f.atualizado_em,
                  u.nome AS fechado_por_nome
           FROM fechamentos f
           LEFT JOIN usuarios u ON u.id = f.fechado_por
           WHERE f.mes = ?""",
        (mes,),
    ).fetchone()
    return dict(row) if row else None


def mes_esta_fechado(conn, mes: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM fechamentos WHERE mes = ? AND status = ?", (mes, STATUS_FECHADO)
    ).fetchone()
    return row is not None


def meses_fechados(conn) -> set[str]:
    rows = conn.execute("SELECT mes FROM fechamentos WHERE status = ?", (STATUS_FECHADO,)).fetchall()
    return {r["mes"] for r in rows}


def garantir_meses_abertos(conn, referencias: Iterable[Union[date, str, None]]) -> None:
    """Levanta MesFechadoError se alguma data/mês informado estiver travado."""
    for ref in referencias:
        if ref is None:
            continue
        mes = mes_de(ref) if isinstance(ref, date) else ref
        if mes_esta_fechado(conn, mes):
            raise MesFechadoError(mes)


def snapshot_totais(dados: dict) -> dict:
    """Extrai do resultado de _dados_fechamento os totais a congelar (como texto)."""
    totais = dados["totais_entradas"]
    valores = {
        "dizimos": totais["dizimo"],
        "ofertas_nominais": totais["oferta_nominal"],
        "ofertas_coletivas": totais["oferta_coletiva"],
        "total_bruto": dados["total_bruto"],
        "total_saidas": dados["total_saidas"],
        "valor_sede": dados["valor_sede"],
        "valor_fundo": dados["valor_fundo"],
        "valor_regional": dados["valor_regional"],
        "dizimo_pastor": dados["dizimo_pastor"],
        "total_enviado_sede": dados["total_enviado_sede"],
        "saldo_acumulado": dados["saldo_acumulado"],
        "qtd_entradas": len(dados["entradas"]),
        "qtd_saidas": len(dados["saidas"]),
        "taxa_sede": dados["taxa_sede"],
        "taxa_fundo": dados["taxa_fundo"],
        "taxa_regional": dados["taxa_regional"],
    }
    return {k: str(v) for k, v in valores.items()}


def divergencias(snapshot: Optional[dict], atual: dict) -> list[str]:
    """Chaves cujo valor atual difere do congelado na efetivação."""
    if not snapshot:
        return []
    diferentes = []
    for chave in CHAVES_CONFERENCIA:
        antes, agora = snapshot.get(chave), atual.get(chave)
        if antes is None or agora is None:
            continue
        try:
            if Decimal(antes) != Decimal(agora):
                diferentes.append(chave)
        except ArithmeticError:
            diferentes.append(chave)
    return diferentes


def efetivar(conn, mes: str, usuario_id: int, totais: dict, observacao: Optional[str], ip: Optional[str]) -> bool:
    """Efetiva (trava) o mês. Retorna False se ele já estava efetivado.

    Upsert atômico: duas efetivações simultâneas não duplicam nem
    sobrescrevem um mês já fechado.
    """
    totais_json = json.dumps(totais)
    row = conn.execute(
        """INSERT INTO fechamentos (mes, status, totais, fechado_em, fechado_por, atualizado_em)
           VALUES (?, ?, CAST(? AS JSONB), TIMEZONE(?, NOW()), ?, TIMEZONE(?, NOW()))
           ON CONFLICT (mes) DO UPDATE
              SET status = EXCLUDED.status, totais = EXCLUDED.totais,
                  fechado_em = EXCLUDED.fechado_em, fechado_por = EXCLUDED.fechado_por,
                  atualizado_em = EXCLUDED.atualizado_em
            WHERE fechamentos.status <> ?
           RETURNING mes""",
        (mes, STATUS_FECHADO, totais_json, FUSO, usuario_id, FUSO, STATUS_FECHADO),
    ).fetchone()
    if row is None:
        return False
    _registrar(conn, mes, "efetivar", usuario_id, observacao, ip, totais_json)
    log.info("fechamento_efetivado mes=%s usuario_id=%s", mes, usuario_id)
    return True


def reabrir(conn, mes: str, usuario_id: int, motivo: str, ip: Optional[str]) -> bool:
    """Reabre um mês efetivado. Retorna False se ele não estava efetivado."""
    row = conn.execute(
        """UPDATE fechamentos SET status = ?, atualizado_em = TIMEZONE(?, NOW())
           WHERE mes = ? AND status = ? RETURNING mes""",
        (STATUS_REABERTO, FUSO, mes, STATUS_FECHADO),
    ).fetchone()
    if row is None:
        return False
    _registrar(conn, mes, "reabrir", usuario_id, motivo, ip, None)
    log.warning("fechamento_reaberto mes=%s usuario_id=%s", mes, usuario_id)
    return True


def validar_motivo(motivo: str) -> Optional[str]:
    """Mensagem de erro do motivo de reabertura, ou None se válido."""
    motivo = motivo.strip()
    if len(motivo) < TAMANHO_MINIMO_MOTIVO:
        return f"Descreva o motivo da reabertura (mínimo {TAMANHO_MINIMO_MOTIVO} caracteres)."
    if len(motivo) > TAMANHO_MAXIMO_MOTIVO:
        return f"O motivo pode ter no máximo {TAMANHO_MAXIMO_MOTIVO} caracteres."
    return None


def historico(conn, mes: str) -> list[dict]:
    rows = conn.execute(
        """SELECT l.acao, l.motivo, l.criado_em, u.nome AS usuario_nome
           FROM fechamentos_log l
           JOIN usuarios u ON u.id = l.usuario_id
           WHERE l.mes = ?
           ORDER BY l.criado_em DESC, l.id DESC""",
        (mes,),
    ).fetchall()
    return [dict(r) for r in rows]


def _registrar(conn, mes, acao, usuario_id, motivo, ip, totais_json) -> None:
    conn.execute(
        """INSERT INTO fechamentos_log (mes, acao, motivo, usuario_id, ip, totais, criado_em)
           VALUES (?, ?, ?, ?, ?, CAST(? AS JSONB), TIMEZONE(?, NOW()))""",
        (mes, acao, motivo, usuario_id, ip, totais_json, FUSO),
    )
