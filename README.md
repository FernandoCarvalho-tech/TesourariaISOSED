# Tesouraria ISOSED

Sistema web da tesouraria da igreja: caixa (entradas, saídas e transferências entre contas), fechamento mensal com repasses à sede e cadastro de membros e dizimistas. Funciona como PWA no celular.

## Arquitetura

| Arquivo | Responsabilidade |
|---|---|
| `app.py` | Rotas Flask, telas e geração de PDF (ReportLab) |
| `config.py` | Leitura e validação das variáveis de ambiente; logging |
| `seguranca.py` | CSRF, limite de tentativas, cabeçalhos HTTP, política de senha, captcha |
| `validacao.py` | Validação de valores, datas, CPF, CEP e do cadastro público (funções puras) |
| `fechamentos.py` | Regras do fechamento: prévia, efetivação, trava do mês e reabertura |
| `db.py` | Conexão Postgres e criação/migração do schema (inclui triggers da trava) |

Stack: Flask 3, PostgreSQL (Supabase), gunicorn no Render, deploy automático a cada push na `main`.

## Fluxo do fechamento mensal

1. **Prévia.** Enquanto o mês está aberto, o PDF sai com a marca d'água *PRÉVIA — NÃO ENVIAR À SEDE*. É o PDF para o pastor conferir.
2. **Efetivar.** Depois da conferência, admin ou tesoureiro clica em *Efetivar fechamento* e confirma com a própria senha. Os totais ficam congelados e o mês é **travado**: ninguém inclui, edita ou exclui lançamento com data nele, nem altera o dízimo do pastor daquele mês. O PDF passa a ser o oficial ("Fechamento efetivado em … por …").
3. **Reabrir.** Só o admin, com senha e motivo (mínimo 10 caracteres). Fica registrado no histórico do mês (quem, quando, IP e motivo). Depois de corrigir, efetiva de novo.

A trava existe no app (mensagem amigável) e no banco (triggers `trg_trava_mes_*`), que barram qualquer escrita no mês efetivado, inclusive direto pelo SQL Editor do Supabase.

> Para corrigir dados de um mês efetivado direto no banco, reabra pelo sistema primeiro.
> Em emergência: `UPDATE fechamentos SET status='reaberto' WHERE mes='AAAA-MM';` e registre o motivo.

## Configuração

Todas as variáveis estão em [`.env.example`](.env.example). As obrigatórias são:

- `DATABASE_URL`
- `SECRET_KEY` (mínimo 32 caracteres). **O app não sobe sem ela.**

Para gerar a chave:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Banco novo (sem usuários): defina `ADMIN_INICIAL_USUARIO` e `ADMIN_INICIAL_SENHA` para criar o primeiro admin e depois apague as duas do Render. Não existe mais senha padrão no código.

## Rodar localmente

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export DATABASE_URL=postgresql://postgres@localhost:5432/tesouraria
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
export APP_ENV=dev          # permite cookie de sessão em http://localhost
flask --app app run --debug
```

## Testes

```bash
# Unitários (não precisam de banco)
pytest tests/test_validacao.py

# Todos, incluindo integração: use um banco DESCARTÁVEL (é truncado a cada teste)
createdb tesouraria_test
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/tesouraria_test pytest
```

## Deploy (Render)

- **Build:** `pip install -r requirements.txt`
- **Start:** `gunicorn app:app` (Procfile)
- **Health check:** `/health` (faz `SELECT 1`; o UptimeRobot deve apontar para ela)

As tabelas e triggers novas são criadas sozinhas na subida (`init_db`, idempotente e serializada com advisory lock).

Antes do primeiro deploy desta versão, confira no Render > Environment se `SECRET_KEY` existe e tem 32+ caracteres. Se faltar, o deploy falha e o Render mantém a versão anterior no ar.

## Segurança

- **Sessão:** cookie `Secure`, `HttpOnly` e `SameSite=Lax`. Expira após `SESSAO_INATIVIDADE_MIN` sem uso e é regerada no login.
- **CSRF:** token em todos os formulários (Flask-WTF).
- **Limite de tentativas:**
  - login: por IP, e por usuário contando só as falhas;
  - confirmação de senha no fechamento;
  - cadastro público.
- **Senha:** mínimo de 8 caracteres e lista de senhas proibidas. Quem entra com senha fraca ou padrão (ex.: `admin123`) é obrigado a trocar antes de usar o sistema.
- **Cabeçalhos:** CSP, `X-Frame-Options`, HSTS, `nosniff` e `no-store` em páginas com dados.
- **Cadastro público:**
  - valida CPF (dígitos verificadores), datas, CEP e telefone;
  - campo-armadilha contra robôs;
  - captcha opcional (Turnstile).
- **SQL:** sempre parametrizado.

## Troubleshooting

| Sintoma | Causa provável / ação |
|---|---|
| Deploy falha com `SECRET_KEY ausente ou curta demais` | Defina a variável no Render (ver Configuração) |
| "O formulário expirou…" ao salvar | Sessão expirou ou a página ficou aberta depois de um novo deploy com outra `SECRET_KEY`: recarregue a página |
| "Muitas tentativas" | Limite de tentativas atingido; aguarde a janela (ex.: 15 min para login) |
| "O mês … já foi efetivado e está travado" | Reabra o mês na tela de Fechamento (admin, com senha e motivo) |
| Erro 503 em `/health` | Banco fora do ar ou pausado (Supabase free pausa por inatividade) |
| Horário "Lançado em" adiantado 3h | Pendência conhecida: as colunas antigas usam `NOW()` do banco (UTC). As datas do fechamento já usam `APP_TIMEZONE` |
