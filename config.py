"""Configuração da aplicação Tesouraria ISOSED.

Objetivo : centralizar a leitura e a validação das variáveis de ambiente e a
           configuração de logging. Nada sensível fica no código.
Entradas : variáveis de ambiente (ver .env.example).
Saídas   : app.config preenchido; logging configurado.
Versão   : 1.0 (2026-11) — SECRET_KEY obrigatória, sessão com expiração,
           limites de tentativa configuráveis.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Mapping

from flask import Flask, g, has_request_context

TAMANHO_MINIMO_SECRET_KEY = 32


class ConfiguracaoInvalida(RuntimeError):
    """Erro de configuração que deve impedir a aplicação de subir."""


def _inteiro(env: Mapping[str, str], nome: str, padrao: int) -> int:
    valor = env.get(nome, "").strip()
    if not valor:
        return padrao
    try:
        return int(valor)
    except ValueError as exc:
        raise ConfiguracaoInvalida(f"{nome} deve ser um número inteiro (recebido: {valor!r}).") from exc


def carregar_config(env: Mapping[str, str] | None = None) -> dict:
    """Lê e valida as variáveis de ambiente.

    Falha cedo (ConfiguracaoInvalida) quando algo obrigatório está ausente,
    para o deploy quebrar no Render em vez de subir inseguro.
    """
    env = os.environ if env is None else env

    secret_key = env.get("SECRET_KEY", "")
    if len(secret_key) < TAMANHO_MINIMO_SECRET_KEY:
        raise ConfiguracaoInvalida(
            "SECRET_KEY ausente ou curta demais (mínimo "
            f"{TAMANHO_MINIMO_SECRET_KEY} caracteres). Gere uma com: "
            'python -c "import secrets; print(secrets.token_hex(32))"'
        )

    ambiente = env.get("APP_ENV", "producao").strip().lower()
    if ambiente not in {"producao", "dev"}:
        raise ConfiguracaoInvalida("APP_ENV deve ser 'producao' ou 'dev'.")

    turnstile_site = env.get("TURNSTILE_SITE_KEY", "").strip()
    turnstile_secret = env.get("TURNSTILE_SECRET_KEY", "").strip()
    if bool(turnstile_site) != bool(turnstile_secret):
        raise ConfiguracaoInvalida(
            "Defina TURNSTILE_SITE_KEY e TURNSTILE_SECRET_KEY juntas (ou nenhuma)."
        )

    inatividade_min = _inteiro(env, "SESSAO_INATIVIDADE_MIN", 240)
    if inatividade_min < 5:
        raise ConfiguracaoInvalida("SESSAO_INATIVIDADE_MIN deve ser de pelo menos 5 minutos.")

    return {
        "SECRET_KEY": secret_key,
        "APP_ENV": ambiente,
        # Sessão: cookie só via HTTPS em produção, inacessível a JS e não
        # enviado em POST de outros sites. Expira após o período de
        # inatividade (o cookie é renovado a cada requisição).
        "SESSION_COOKIE_SECURE": ambiente == "producao",
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "PERMANENT_SESSION_LIFETIME": timedelta(minutes=inatividade_min),
        "SESSION_REFRESH_EACH_REQUEST": True,
        # CSRF: token vale enquanto a sessão valer (evita erro em formulário
        # deixado aberto por mais de 1 hora, que é o padrão do Flask-WTF).
        "WTF_CSRF_TIME_LIMIT": None,
        # Não exige o cabeçalho Referer: alguns navegadores/PWA o omitem e o
        # token + SameSite=Lax já protegem contra CSRF.
        "WTF_CSRF_SSL_STRICT": False,
        # Limites de tentativa (sintaxe do Flask-Limiter).
        "RATELIMIT_STORAGE_URI": env.get("RATELIMIT_STORAGE_URI", "memory://"),
        "RATELIMIT_HEADERS_ENABLED": False,
        "LIMITE_LOGIN_IP": env.get("LIMITE_LOGIN_IP", "20 per 10 minutes"),
        "LIMITE_LOGIN_USUARIO": env.get("LIMITE_LOGIN_USUARIO", "5 per 15 minutes"),
        "LIMITE_CADASTRO_IP": env.get("LIMITE_CADASTRO_IP", "15 per hour"),
        "LIMITE_CONFIRMACAO_SENHA": env.get("LIMITE_CONFIRMACAO_SENHA", "10 per 15 minutes"),
        # Proxy reverso do Render: quantos saltos confiáveis no X-Forwarded-*.
        "TRUSTED_PROXIES": _inteiro(env, "TRUSTED_PROXIES", 1),
        # Captcha opcional no cadastro público (Cloudflare Turnstile).
        "TURNSTILE_SITE_KEY": turnstile_site,
        "TURNSTILE_SECRET_KEY": turnstile_secret,
        "TURNSTILE_TIMEOUT_S": _inteiro(env, "TURNSTILE_TIMEOUT_S", 5),
        "LOG_LEVEL": env.get("LOG_LEVEL", "INFO").upper(),
    }


class _FiltroRequestId(logging.Filter):
    """Injeta o ID da requisição em cada linha de log (ou '-' fora de requisição)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = getattr(g, "request_id", "-") if has_request_context() else "-"
        return True


def configurar_logging(nivel: str) -> None:
    """Logging estruturado no stdout (o Render coleta e retém os logs).

    Não há arquivo com rotação porque o disco do container é efêmero.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [req=%(request_id)s] %(name)s: %(message)s"
    ))
    handler.addFilter(_FiltroRequestId())
    raiz = logging.getLogger()
    raiz.handlers = [handler]
    raiz.setLevel(getattr(logging, nivel, logging.INFO))


def aplicar_config(app: Flask, env: Mapping[str, str] | None = None) -> None:
    """Carrega a configuração no app e configura o logging."""
    config = carregar_config(env)
    app.config.update(config)
    configurar_logging(config["LOG_LEVEL"])
