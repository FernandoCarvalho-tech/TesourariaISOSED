# Changelog

## 1.1.0 — 2026-11-09

### Fechamento mensal
- Fluxo **prévia → efetivar → reabrir**:
  - o PDF do mês aberto sai com a marca d'água "PRÉVIA — NÃO ENVIAR À SEDE";
  - efetivar exige a senha de quem efetiva e congela os totais;
  - reabrir é só do admin, com senha e motivo.
- Mês efetivado fica travado para inclusão, edição e exclusão de lançamentos e para o dízimo do pastor. A trava vale no app e em triggers no banco.
- Histórico de efetivações e reaberturas por mês (`fechamentos_log`), com usuário, data, IP e motivo.
- Alerta na tela se os valores atuais divergirem dos congelados na efetivação.
- O extrato mostra 🔒 no lugar de Editar/Excluir nos meses fechados.
- Cálculos do fechamento em `Decimal`, com repasses arredondados em centavos (meio para cima).

### Segurança
- `SECRET_KEY` obrigatória (mínimo 32 caracteres): o app não sobe com a chave padrão.
- Removida a criação automática de `admin/admin123`. O primeiro admin vem de variável de ambiente, só em banco vazio.
- Quem entra com senha fraca ou padrão é obrigado a trocar. Nova política de senha, com confirmação na troca.
- Proteção CSRF em todos os formulários.
- Limite de tentativas no login (por IP e por usuário), no cadastro público e na confirmação de senha.
- Sessão:
  - cookie `Secure`/`HttpOnly`/`SameSite`;
  - expira por inatividade;
  - é regerada no login (contra fixação de sessão).
- Cabeçalhos de segurança: CSP, HSTS, `X-Frame-Options`, `nosniff` e `no-store`.
- `ProxyFix` para o IP e o HTTPS corretos atrás do proxy do Render.
- Login não redireciona mais para sites externos (`?next=`).
- Corrigido XSS armazenado: um nome enviado pelo cadastro público podia executar script na tela de pendentes.
- Cadastro público:
  - valida CPF, datas, CEP, telefone e tamanhos;
  - campo-armadilha anti-robô;
  - captcha Turnstile opcional;
  - mantém o que foi digitado quando há erro.
- Validação de valores, datas, tipos e IDs nos lançamentos. Entradas inválidas que antes causavam erro 500 agora mostram mensagem. "1.234" deixou de virar R$ 1,23.
- Conexões com o banco fechadas ao fim de toda requisição, inclusive quando há erro.
- Dependências com versões fixas; `requirements-dev.txt` separado.

### Outros
- Logging estruturado com ID da requisição e eventos de segurança: login, falhas, efetivação, reabertura e exclusões.
- Testes automatizados (`tests/`): 50 testes, unitários e de integração com Postgres.
- README e `.env.example`.
