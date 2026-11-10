"""Camada de segurança HTTP da Tesouraria ISOSED.

Objetivo : CSRF, limite de tentativas, cabeçalhos de segurança, proxy
           reverso, política de senha, redirecionamento seguro e captcha
           opcional (Cloudflare Turnstile) no cadastro público.
Dependências: Flask-WTF, Flask-Limiter, Werkzeug.
Versão   : 1.0 (2026-11)
"""
from __future__ import annotations

import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from flask import Flask, flash, g, redirect, render_template, request, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

log = logging.getLogger("tesouraria.seguranca")

csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)

TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TAMANHO_MINIMO_SENHA = 8
# Senhas que nunca são aceitas e que, se usadas no login, obrigam a troca.
SENHAS_PROIBIDAS = frozenset({
    "admin123", "admin", "12345678", "123456789", "1234567890", "senha123",
    "password", "tesouraria", "isosed123", "igreja123", "mudar123",
})

# Política de conteúdo: o app usa CSS/JS inline e eventos inline (onclick),
# por isso 'unsafe-inline'. Ainda assim bloqueia scripts de outros domínios,
# envio de dados para fora (connect-src/form-action) e uso em iframe.
_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "frame-src https://challenges.cloudflare.com",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
])


def init_seguranca(app: Flask) -> None:
    """Liga todas as proteções HTTP no app."""
    proxies = app.config["TRUSTED_PROXIES"]
    if proxies > 0:
        # O Render fica na frente do gunicorn: sem isso o IP de todo mundo
        # seria o do proxy (limite de tentativas inútil) e o esquema seria http.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxies, x_proto=proxies, x_host=proxies)

    csrf.init_app(app)
    limiter.init_app(app)

    @app.before_request
    def _atribuir_request_id() -> None:
        g.request_id = secrets.token_hex(6)

    @app.after_request
    def _cabecalhos_seguranca(resp):
        resp.headers.setdefault("Content-Security-Policy", _CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.is_secure:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        # Páginas com dados financeiros/pessoais não devem ficar em cache.
        if resp.mimetype in ("text/html", "application/pdf", "application/json"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.errorhandler(CSRFError)
    def _csrf_invalido(exc: CSRFError):
        log.warning("csrf_rejeitado path=%s motivo=%s", request.path, exc.description)
        flash("O formulário expirou ou foi enviado de fora do sistema. Tente novamente.", "error")
        return redirect(url_destino_segura(request.referrer) or url_for("dashboard"))

    @app.errorhandler(429)
    def _muitas_tentativas(exc):
        log.warning("limite_excedido path=%s limite=%s", request.path, getattr(exc, "description", ""))
        return render_template(
            "erro.html",
            titulo="Muitas tentativas",
            mensagem="Você fez muitas tentativas em pouco tempo. Aguarde alguns minutos e tente novamente.",
        ), 429


def url_destino_segura(destino: Optional[str]) -> Optional[str]:
    """Aceita só caminhos internos ('/algo'), evitando redirecionamento aberto.

    Também aceita URL absoluta do próprio host (caso do Referer).
    """
    if not destino:
        return None
    partes = urllib.parse.urlsplit(destino)
    if partes.scheme or partes.netloc:
        if partes.netloc != request.host or partes.scheme not in ("http", "https"):
            return None
        destino = partes.path + (f"?{partes.query}" if partes.query else "")
    if not destino.startswith("/") or destino.startswith("//") or "\\" in destino:
        return None
    return destino


def senha_e_fraca(senha: str) -> bool:
    """True se a senha não atende a política (usado no login para forçar troca)."""
    return len(senha) < TAMANHO_MINIMO_SENHA or senha.lower() in SENHAS_PROIBIDAS


def validar_nova_senha(senha: str, usuario: str, confirmacao: Optional[str] = None) -> list[str]:
    """Retorna a lista de problemas da senha (vazia = aprovada)."""
    erros = []
    if len(senha) < TAMANHO_MINIMO_SENHA:
        erros.append(f"A senha deve ter pelo menos {TAMANHO_MINIMO_SENHA} caracteres.")
    if senha.lower() in SENHAS_PROIBIDAS:
        erros.append("Essa senha é muito comum. Escolha outra.")
    if usuario and senha.lower() == usuario.lower():
        erros.append("A senha não pode ser igual ao usuário.")
    if senha.isdigit() and len(set(senha)) <= 2:
        erros.append("A senha não pode ser uma sequência repetida de números.")
    if confirmacao is not None and senha != confirmacao:
        erros.append("A confirmação não confere com a nova senha.")
    return erros


def verificar_turnstile(token: str, ip: Optional[str], segredo: str, timeout_s: int) -> bool:
    """Confere o token do Cloudflare Turnstile.

    Falha fechada: erro de rede ou resposta inválida = não aprovado (o
    usuário vê uma mensagem pedindo para tentar de novo). Sem retry, pois o
    usuário está esperando na tela.
    """
    if not token:
        return False
    corpo = urllib.parse.urlencode({"secret": segredo, "response": token, "remoteip": ip or ""}).encode()
    req = urllib.request.Request(TURNSTILE_URL, data=corpo, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (URL fixa https)
            resultado = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.error("turnstile_indisponivel erro=%s", exc.__class__.__name__)
        return False
    if not resultado.get("success"):
        log.warning("turnstile_reprovado codigos=%s", resultado.get("error-codes"))
        return False
    return True
