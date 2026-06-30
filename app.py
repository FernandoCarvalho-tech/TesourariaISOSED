import os
from datetime import date
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, flash, session, jsonify
)
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_conn, init_db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-troque-em-producao")

TAXA_SEDE = 0.15
TAXA_FUNDO = 0.03
TAXA_REGIONAL = 0.05

with app.app_context():
    init_db()


@app.before_request
def exigir_login():
    rotas_publicas = {"login", "static", "manifest", "service_worker"}
    if request.endpoint in rotas_publicas or request.endpoint is None:
        return
    if not session.get("logado"):
        return redirect(url_for("login", next=request.path))


def requer_papel(*papeis):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if session.get("papel") not in papeis:
                flash("Você não tem permissão para acessar esta página.", "error")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return wrapper
    return decorator


@app.route("/manifest.json")
def manifest():
    return app.send_static_file("manifest.json")


@app.route("/service-worker.js")
def service_worker():
    return app.send_static_file("service-worker.js")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        senha = request.form.get("senha", "")
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM usuarios WHERE usuario=? AND ativo=TRUE", (usuario,)
        ).fetchone()
        conn.close()
        if row and check_password_hash(row["senha_hash"], senha):
            session["logado"] = True
            session["usuario_id"] = row["id"]
            session["usuario_nome"] = row["nome"]
            session["papel"] = row["papel"]
            destino = request.args.get("next") or url_for("dashboard")
            return redirect(destino)
        flash("Usuário ou senha inválidos.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _contas_ativas(conn):
    return conn.execute("SELECT * FROM contas WHERE ativo=TRUE ORDER BY nome").fetchall()


def _saldo_conta(conn, conta_id):
    entradas = conn.execute(
        "SELECT COALESCE(SUM(valor),0) AS total FROM entradas WHERE conta_id=?", (conta_id,)
    ).fetchone()["total"]
    saidas = conn.execute(
        "SELECT COALESCE(SUM(valor),0) AS total FROM saidas WHERE conta_id=?", (conta_id,)
    ).fetchone()["total"]
    transf_saida = conn.execute(
        "SELECT COALESCE(SUM(valor),0) AS total FROM transferencias WHERE conta_origem_id=?",
        (conta_id,),
    ).fetchone()["total"]
    transf_entrada = conn.execute(
        "SELECT COALESCE(SUM(valor),0) AS total FROM transferencias WHERE conta_destino_id=?",
        (conta_id,),
    ).fetchone()["total"]
    return float(entradas) - float(saidas) - float(transf_saida) + float(transf_entrada)


@app.route("/")
def dashboard():
    conn = get_conn()
    contas = _contas_ativas(conn)
    saldos = [(c, _saldo_conta(conn, c["id"])) for c in contas]
    saldo_total = sum(s for _, s in saldos)

    ultimas_entradas = conn.execute(
        """SELECT e.*, c.nome AS conta_nome, u.nome AS usuario_nome,
                  COALESCE(d.nome, e.nome_avulso) AS nome_pessoa
           FROM entradas e
           JOIN contas c ON c.id = e.conta_id
           JOIN usuarios u ON u.id = e.usuario_id
           LEFT JOIN dizimistas d ON d.id = e.dizimista_id
           ORDER BY e.criado_em DESC LIMIT 5"""
    ).fetchall()
    ultimas_saidas = conn.execute(
        """SELECT s.*, c.nome AS conta_nome, u.nome AS usuario_nome
           FROM saidas s
           JOIN contas c ON c.id = s.conta_id
           JOIN usuarios u ON u.id = s.usuario_id
           ORDER BY s.criado_em DESC LIMIT 5"""
    ).fetchall()
    conn.close()
    return render_template(
        "dashboard.html",
        saldos=saldos,
        saldo_total=saldo_total,
        ultimas_entradas=ultimas_entradas,
        ultimas_saidas=ultimas_saidas,
    )


@app.route("/entradas/nova", methods=["GET", "POST"])
@requer_papel("admin", "tesoureiro")
def nova_entrada():
    conn = get_conn()
    if request.method == "POST":
        tipo = request.form.get("tipo")
        dizimista_id = request.form.get("dizimista_id") or None
        nome_avulso = request.form.get("nome_avulso", "").strip() or None
        valor = request.form.get("valor", "0").replace(",", ".")
        conta_id = request.form.get("conta_id")
        data_lanc = request.form.get("data") or date.today().isoformat()
        observacao = request.form.get("observacao", "").strip() or None

        if tipo == "oferta_coletiva":
            dizimista_id = None
            nome_avulso = None

        try:
            valor_f = float(valor)
            if valor_f <= 0:
                raise ValueError
        except ValueError:
            flash("Valor inválido.", "error")
            return redirect(url_for("nova_entrada"))

        conn.execute(
            """INSERT INTO entradas
               (tipo, dizimista_id, nome_avulso, valor, conta_id, data, observacao, usuario_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (tipo, dizimista_id, nome_avulso, valor_f, conta_id, data_lanc, observacao,
             session["usuario_id"]),
        )
        conn.commit()
        conn.close()
        flash("Entrada registrada com sucesso.", "success")
        return redirect(url_for("nova_entrada"))

    contas = _contas_ativas(conn)
    dizimistas = conn.execute(
        "SELECT * FROM dizimistas WHERE ativo=TRUE ORDER BY nome"
    ).fetchall()
    conn.close()
    return render_template(
        "nova_entrada.html", contas=contas, dizimistas=dizimistas, hoje=date.today().isoformat()
    )


@app.route("/saidas/nova", methods=["GET", "POST"])
@requer_papel("admin", "tesoureiro")
def nova_saida():
    conn = get_conn()
    if request.method == "POST":
        motivo = request.form.get("motivo", "").strip()
        valor = request.form.get("valor", "0").replace(",", ".")
        conta_id = request.form.get("conta_id")
        data_lanc = request.form.get("data") or date.today().isoformat()
        observacao = request.form.get("observacao", "").strip() or None

        try:
            valor_f = float(valor)
            if valor_f <= 0 or not motivo:
                raise ValueError
        except ValueError:
            flash("Preencha motivo e um valor válido.", "error")
            return redirect(url_for("nova_saida"))

        conn.execute(
            """INSERT INTO saidas (motivo, valor, conta_id, data, observacao, usuario_id)
               VALUES (?,?,?,?,?,?)""",
            (motivo, valor_f, conta_id, data_lanc, observacao, session["usuario_id"]),
        )
        conn.commit()
        saldo_atual = _saldo_conta(conn, conta_id)
        conn.close()
        flash("Saída registrada com sucesso.", "success")
        if saldo_atual < 0:
            valor_fmt = f"{saldo_atual:.2f}".replace(".", ",")
            flash(f"Atenção: a conta selecionada ficou com saldo negativo (R$ {valor_fmt})", "error")
        return redirect(url_for("nova_saida"))

    contas = _contas_ativas(conn)
    conn.close()
    return render_template("nova_saida.html", contas=contas, hoje=date.today().isoformat())


@app.route("/transferencias/nova", methods=["GET", "POST"])
@requer_papel("admin", "tesoureiro")
def nova_transferencia():
    conn = get_conn()
    if request.method == "POST":
        origem = request.form.get("conta_origem_id")
        destino = request.form.get("conta_destino_id")
        valor = request.form.get("valor", "0").replace(",", ".")
        data_lanc = request.form.get("data") or date.today().isoformat()
        motivo = request.form.get("motivo", "").strip() or None

        try:
            valor_f = float(valor)
            if valor_f <= 0 or origem == destino:
                raise ValueError
        except ValueError:
            flash("Verifique os valores: contas devem ser diferentes e valor maior que zero.", "error")
            return redirect(url_for("nova_transferencia"))

        conn.execute(
            """INSERT INTO transferencias
               (conta_origem_id, conta_destino_id, valor, data, motivo, usuario_id)
               VALUES (?,?,?,?,?,?)""",
            (origem, destino, valor_f, data_lanc, motivo, session["usuario_id"]),
        )
        conn.commit()
        saldo_origem = _saldo_conta(conn, origem)
        conn.close()
        flash("Transferência registrada com sucesso.", "success")
        if saldo_origem < 0:
            valor_fmt = f"{saldo_origem:.2f}".replace(".", ",")
            flash(f"Atenção: a conta de origem ficou com saldo negativo (R$ {valor_fmt})", "error")
        return redirect(url_for("nova_transferencia"))

    contas = _contas_ativas(conn)
    conn.close()
    return render_template("nova_transferencia.html", contas=contas, hoje=date.today().isoformat())


@app.route("/extrato")
def extrato():
    conn = get_conn()
    contas = _contas_ativas(conn)

    conta_id = request.args.get("conta_id") or ""
    data_ini = request.args.get("data_ini") or ""
    data_fim = request.args.get("data_fim") or ""

    filtros_entrada = []
    params_entrada = []
    filtros_saida = []
    params_saida = []
    filtros_transf = []
    params_transf = []

    if conta_id:
        filtros_entrada.append("e.conta_id = ?")
        params_entrada.append(conta_id)
        filtros_saida.append("s.conta_id = ?")
        params_saida.append(conta_id)
        filtros_transf.append("(t.conta_origem_id = ? OR t.conta_destino_id = ?)")
        params_transf.extend([conta_id, conta_id])
    if data_ini:
        filtros_entrada.append("e.data >= ?")
        params_entrada.append(data_ini)
        filtros_saida.append("s.data >= ?")
        params_saida.append(data_ini)
        filtros_transf.append("t.data >= ?")
        params_transf.append(data_ini)
    if data_fim:
        filtros_entrada.append("e.data <= ?")
        params_entrada.append(data_fim)
        filtros_saida.append("s.data <= ?")
        params_saida.append(data_fim)
        filtros_transf.append("t.data <= ?")
        params_transf.append(data_fim)

    where_e = (" WHERE " + " AND ".join(filtros_entrada)) if filtros_entrada else ""
    where_s = (" WHERE " + " AND ".join(filtros_saida)) if filtros_saida else ""
    where_t = (" WHERE " + " AND ".join(filtros_transf)) if filtros_transf else ""

    entradas = conn.execute(
        f"""SELECT e.*, c.nome AS conta_nome, u.nome AS usuario_nome,
                   COALESCE(d.nome, e.nome_avulso) AS nome_pessoa
            FROM entradas e
            JOIN contas c ON c.id = e.conta_id
            JOIN usuarios u ON u.id = e.usuario_id
            LEFT JOIN dizimistas d ON d.id = e.dizimista_id
            {where_e}
            ORDER BY e.data DESC, e.criado_em DESC""",
        params_entrada,
    ).fetchall()

    saidas = conn.execute(
        f"""SELECT s.*, c.nome AS conta_nome, u.nome AS usuario_nome
            FROM saidas s
            JOIN contas c ON c.id = s.conta_id
            JOIN usuarios u ON u.id = s.usuario_id
            {where_s}
            ORDER BY s.data DESC, s.criado_em DESC""",
        params_saida,
    ).fetchall()

    transferencias = conn.execute(
        f"""SELECT t.*, co.nome AS conta_origem_nome, cd.nome AS conta_destino_nome,
                   u.nome AS usuario_nome
            FROM transferencias t
            JOIN contas co ON co.id = t.conta_origem_id
            JOIN contas cd ON cd.id = t.conta_destino_id
            JOIN usuarios u ON u.id = t.usuario_id
            {where_t}
            ORDER BY t.data DESC, t.criado_em DESC""",
        params_transf,
    ).fetchall()

    conn.close()
    return render_template(
        "extrato.html",
        contas=contas,
        entradas=entradas,
        saidas=saidas,
        transferencias=transferencias,
        conta_id=conta_id,
        data_ini=data_ini,
        data_fim=data_fim,
    )


@app.route("/fechamento")
def fechamento():
    conn = get_conn()
    hoje = date.today()
    mes = request.args.get("mes") or f"{hoje.year:04d}-{hoje.month:02d}"
    ano, mes_num = mes.split("-")
    data_ini = f"{ano}-{mes_num}-01"

    total_bruto = conn.execute(
        "SELECT COALESCE(SUM(valor),0) AS total FROM entradas WHERE to_char(data,'YYYY-MM') = ?",
        (mes,),
    ).fetchone()["total"]
    total_bruto = float(total_bruto)

    valor_sede = total_bruto * TAXA_SEDE
    valor_fundo = total_bruto * TAXA_FUNDO
    valor_regional = total_bruto * TAXA_REGIONAL
    valor_total_taxas = valor_sede + valor_fundo + valor_regional

    conn.close()
    return render_template(
        "fechamento.html",
        mes=mes,
        total_bruto=total_bruto,
        valor_sede=valor_sede,
        valor_fundo=valor_fundo,
        valor_regional=valor_regional,
        valor_total_taxas=valor_total_taxas,
        taxa_sede=TAXA_SEDE,
        taxa_fundo=TAXA_FUNDO,
        taxa_regional=TAXA_REGIONAL,
    )


@app.route("/dizimistas", methods=["GET", "POST"])
@requer_papel("admin", "tesoureiro")
def dizimistas():
    conn = get_conn()
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        if nome:
            conn.execute("INSERT INTO dizimistas (nome) VALUES (?)", (nome,))
            conn.commit()
            flash("Dizimista cadastrado.", "success")
        conn.close()
        return redirect(url_for("dizimistas"))

    lista = conn.execute("SELECT * FROM dizimistas ORDER BY nome").fetchall()
    conn.close()
    return render_template("dizimistas.html", dizimistas=lista)


@app.route("/dizimistas/<int:dizimista_id>/toggle", methods=["POST"])
@requer_papel("admin", "tesoureiro")
def toggle_dizimista(dizimista_id):
    conn = get_conn()
    conn.execute(
        "UPDATE dizimistas SET ativo = NOT ativo WHERE id = ?", (dizimista_id,)
    )
    conn.commit()
    conn.close()
    return redirect(url_for("dizimistas"))


@app.route("/admin/contas", methods=["GET", "POST"])
@requer_papel("admin")
def admin_contas():
    conn = get_conn()
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        tipo = request.form.get("tipo")
        if nome and tipo:
            conn.execute("INSERT INTO contas (nome, tipo) VALUES (?, ?)", (nome, tipo))
            conn.commit()
            flash("Conta cadastrada.", "success")
        conn.close()
        return redirect(url_for("admin_contas"))

    contas = conn.execute("SELECT * FROM contas ORDER BY nome").fetchall()
    conn.close()
    return render_template("admin_contas.html", contas=contas)


@app.route("/admin/contas/<int:conta_id>/toggle", methods=["POST"])
@requer_papel("admin")
def toggle_conta(conta_id):
    conn = get_conn()
    conn.execute("UPDATE contas SET ativo = NOT ativo WHERE id = ?", (conta_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_contas"))


@app.route("/admin/usuarios", methods=["GET", "POST"])
@requer_papel("admin")
def admin_usuarios():
    conn = get_conn()
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        usuario = request.form.get("usuario", "").strip()
        senha = request.form.get("senha", "")
        papel = request.form.get("papel")
        if nome and usuario and senha and papel:
            existente = conn.execute(
                "SELECT 1 FROM usuarios WHERE usuario = ?", (usuario,)
            ).fetchone()
            if existente:
                flash("Já existe um usuário com esse login.", "error")
            else:
                conn.execute(
                    """INSERT INTO usuarios (nome, usuario, senha_hash, papel)
                       VALUES (?,?,?,?)""",
                    (nome, usuario, generate_password_hash(senha), papel),
                )
                conn.commit()
                flash("Usuário criado com sucesso.", "success")
        else:
            flash("Preencha todos os campos.", "error")
        conn.close()
        return redirect(url_for("admin_usuarios"))

    usuarios = conn.execute("SELECT * FROM usuarios ORDER BY nome").fetchall()
    conn.close()
    return render_template("admin_usuarios.html", usuarios=usuarios)


@app.route("/admin/usuarios/<int:usuario_id>/toggle", methods=["POST"])
@requer_papel("admin")
def toggle_usuario(usuario_id):
    if usuario_id == session.get("usuario_id"):
        flash("Você não pode desativar seu próprio usuário.", "error")
        return redirect(url_for("admin_usuarios"))
    conn = get_conn()
    conn.execute("UPDATE usuarios SET ativo = NOT ativo WHERE id = ?", (usuario_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_usuarios"))


@app.route("/conta", methods=["GET", "POST"])
def minha_conta():
    conn = get_conn()
    if request.method == "POST":
        senha_atual = request.form.get("senha_atual", "")
        nova_senha = request.form.get("nova_senha", "")
        row = conn.execute(
            "SELECT * FROM usuarios WHERE id = ?", (session["usuario_id"],)
        ).fetchone()
        if not check_password_hash(row["senha_hash"], senha_atual):
            flash("Senha atual incorreta.", "error")
        elif not nova_senha:
            flash("Informe a nova senha.", "error")
        else:
            conn.execute(
                "UPDATE usuarios SET senha_hash = ? WHERE id = ?",
                (generate_password_hash(nova_senha), session["usuario_id"]),
            )
            conn.commit()
            flash("Senha atualizada com sucesso.", "success")
        conn.close()
        return redirect(url_for("minha_conta"))

    conn.close()
    return render_template("conta.html")


@app.route("/api/dizimistas")
def api_dizimistas():
    termo = request.args.get("q", "")
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, nome FROM dizimistas WHERE ativo=TRUE AND nome ILIKE ? ORDER BY nome LIMIT 10",
        (f"%{termo}%",),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    app.run(debug=True)
