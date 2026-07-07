import io
import os
from datetime import date
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, flash, session, jsonify,
    make_response,
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
)
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_conn, init_db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-troque-em-producao")

TAXA_SEDE = 0.15
TAXA_FUNDO = 0.03
TAXA_REGIONAL = 0.05

CATEGORIAS_SAIDA = [
    "Aluguel",
    "Programa de Rádio",
    "Água e Energia Elétrica",
    "Telefone",
    "Correio e Xerox (Cópias)",
    "Despesas de Viagens e Combustível",
    "Realização de Eventos",
    "Realização de Santa Ceia",
    "Conservação e Reparo da Igreja",
    "Aquisição de Bens Materiais",
    "Porcentagens",
    "Outros",
]

# Última categoria usada como fallback para registros antigos ou desconhecidos
_CATEGORIA_FALLBACK = CATEGORIAS_SAIDA[-1]

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


def _totais_por_tipo(entradas):
    """Recebe lista de rows de entradas e retorna dict com totais por tipo e o bruto geral."""
    totais = {"dizimo": 0.0, "oferta_nominal": 0.0, "oferta_coletiva": 0.0}
    for e in entradas:
        totais[e["tipo"]] = totais.get(e["tipo"], 0.0) + float(e["valor"])
    totais["total_ofertas"] = totais["oferta_nominal"] + totais["oferta_coletiva"]
    totais["total_bruto"] = totais["dizimo"] + totais["total_ofertas"]
    return totais


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
    hoje = date.today()
    mes = request.args.get("mes") or f"{hoje.year:04d}-{hoje.month:02d}"

    conn = get_conn()
    contas = _contas_ativas(conn)
    saldos = [(c, _saldo_conta(conn, c["id"])) for c in contas]
    saldo_total = sum(s for _, s in saldos)

    entradas_mes = conn.execute(
        """SELECT e.*, c.nome AS conta_nome, u.nome AS usuario_nome,
                  COALESCE(d.nome, e.nome_avulso) AS nome_pessoa
           FROM entradas e
           JOIN contas c ON c.id = e.conta_id
           JOIN usuarios u ON u.id = e.usuario_id
           LEFT JOIN dizimistas d ON d.id = e.dizimista_id
           WHERE to_char(e.data,'YYYY-MM') = ?
           ORDER BY e.data DESC, e.criado_em DESC""",
        (mes,),
    ).fetchall()
    saidas_mes = conn.execute(
        """SELECT s.*, c.nome AS conta_nome, u.nome AS usuario_nome
           FROM saidas s
           JOIN contas c ON c.id = s.conta_id
           JOIN usuarios u ON u.id = s.usuario_id
           WHERE to_char(s.data,'YYYY-MM') = ?
           ORDER BY s.data DESC, s.criado_em DESC""",
        (mes,),
    ).fetchall()
    totais_entradas = _totais_por_tipo(entradas_mes)
    total_saidas_mes = sum(float(s["valor"]) for s in saidas_mes)
    conn.close()
    return render_template(
        "dashboard.html",
        saldos=saldos,
        saldo_total=saldo_total,
        entradas_mes=entradas_mes,
        saidas_mes=saidas_mes,
        totais_entradas=totais_entradas,
        total_saidas_mes=total_saidas_mes,
        mes=mes,
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


@app.route("/entradas/<int:entrada_id>/editar", methods=["GET", "POST"])
@requer_papel("admin")
def editar_entrada(entrada_id):
    conn = get_conn()
    if request.method == "POST":
        tipo = request.form.get("tipo")
        dizimista_id = request.form.get("dizimista_id") or None
        nome_avulso = request.form.get("nome_avulso", "").strip() or None
        valor = request.form.get("valor", "0").replace(",", ".")
        conta_id = request.form.get("conta_id")
        data_lanc = request.form.get("data")
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
            return redirect(url_for("editar_entrada", entrada_id=entrada_id))
        conn.execute(
            """UPDATE entradas SET tipo=?, dizimista_id=?, nome_avulso=?, valor=?,
               conta_id=?, data=?, observacao=? WHERE id=?""",
            (tipo, dizimista_id, nome_avulso, valor_f, conta_id, data_lanc, observacao, entrada_id),
        )
        conn.commit()
        conn.close()
        flash("Entrada atualizada com sucesso.", "success")
        return redirect(url_for("extrato"))

    entrada = conn.execute("SELECT * FROM entradas WHERE id=?", (entrada_id,)).fetchone()
    if not entrada:
        conn.close()
        flash("Entrada não encontrada.", "error")
        return redirect(url_for("extrato"))
    contas = _contas_ativas(conn)
    dizimistas = conn.execute("SELECT * FROM dizimistas WHERE ativo=TRUE ORDER BY nome").fetchall()
    conn.close()
    return render_template("editar_entrada.html", entrada=entrada, contas=contas, dizimistas=dizimistas)


@app.route("/saidas/nova", methods=["GET", "POST"])
@requer_papel("admin", "tesoureiro")
def nova_saida():
    conn = get_conn()
    if request.method == "POST":
        categoria = request.form.get("categoria", _CATEGORIA_FALLBACK)
        motivo = request.form.get("motivo", "").strip()
        valor = request.form.get("valor", "0").replace(",", ".")
        conta_id = request.form.get("conta_id")
        data_lanc = request.form.get("data") or date.today().isoformat()
        observacao = request.form.get("observacao", "").strip() or None

        if categoria not in CATEGORIAS_SAIDA:
            categoria = _CATEGORIA_FALLBACK
        try:
            valor_f = float(valor)
            if valor_f <= 0 or not motivo:
                raise ValueError
        except ValueError:
            flash("Preencha a descrição e um valor válido.", "error")
            return redirect(url_for("nova_saida"))

        conn.execute(
            """INSERT INTO saidas (categoria, motivo, valor, conta_id, data, observacao, usuario_id)
               VALUES (?,?,?,?,?,?,?)""",
            (categoria, motivo, valor_f, conta_id, data_lanc, observacao, session["usuario_id"]),
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
    return render_template("nova_saida.html", contas=contas, categorias=CATEGORIAS_SAIDA, hoje=date.today().isoformat())


@app.route("/saidas/<int:saida_id>/editar", methods=["GET", "POST"])
@requer_papel("admin")
def editar_saida(saida_id):
    conn = get_conn()
    if request.method == "POST":
        categoria = request.form.get("categoria", _CATEGORIA_FALLBACK)
        motivo = request.form.get("motivo", "").strip()
        valor = request.form.get("valor", "0").replace(",", ".")
        conta_id = request.form.get("conta_id")
        data_lanc = request.form.get("data")
        observacao = request.form.get("observacao", "").strip() or None
        if categoria not in CATEGORIAS_SAIDA:
            categoria = _CATEGORIA_FALLBACK
        try:
            valor_f = float(valor)
            if valor_f <= 0 or not motivo:
                raise ValueError
        except ValueError:
            flash("Preencha a descrição e um valor válido.", "error")
            return redirect(url_for("editar_saida", saida_id=saida_id))
        conn.execute(
            "UPDATE saidas SET categoria=?, motivo=?, valor=?, conta_id=?, data=?, observacao=? WHERE id=?",
            (categoria, motivo, valor_f, conta_id, data_lanc, observacao, saida_id),
        )
        conn.commit()
        conn.close()
        flash("Saída atualizada com sucesso.", "success")
        return redirect(url_for("extrato"))

    saida = conn.execute("SELECT * FROM saidas WHERE id=?", (saida_id,)).fetchone()
    if not saida:
        conn.close()
        flash("Saída não encontrada.", "error")
        return redirect(url_for("extrato"))
    contas = _contas_ativas(conn)
    conn.close()
    return render_template("editar_saida.html", saida=saida, contas=contas, categorias=CATEGORIAS_SAIDA)


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


@app.route("/transferencias/<int:transf_id>/editar", methods=["GET", "POST"])
@requer_papel("admin")
def editar_transferencia(transf_id):
    conn = get_conn()
    if request.method == "POST":
        origem = request.form.get("conta_origem_id")
        destino = request.form.get("conta_destino_id")
        valor = request.form.get("valor", "0").replace(",", ".")
        data_lanc = request.form.get("data")
        motivo = request.form.get("motivo", "").strip() or None
        try:
            valor_f = float(valor)
            if valor_f <= 0 or origem == destino:
                raise ValueError
        except ValueError:
            flash("Verifique os valores: contas devem ser diferentes e valor maior que zero.", "error")
            return redirect(url_for("editar_transferencia", transf_id=transf_id))
        conn.execute(
            """UPDATE transferencias SET conta_origem_id=?, conta_destino_id=?,
               valor=?, data=?, motivo=? WHERE id=?""",
            (origem, destino, valor_f, data_lanc, motivo, transf_id),
        )
        conn.commit()
        conn.close()
        flash("Transferência atualizada com sucesso.", "success")
        return redirect(url_for("extrato"))

    transf = conn.execute("SELECT * FROM transferencias WHERE id=?", (transf_id,)).fetchone()
    if not transf:
        conn.close()
        flash("Transferência não encontrada.", "error")
        return redirect(url_for("extrato"))
    contas = _contas_ativas(conn)
    conn.close()
    return render_template("editar_transferencia.html", transf=transf, contas=contas)


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

    totais_entradas = _totais_por_tipo(entradas)
    total_saidas = sum(float(s["valor"]) for s in saidas)
    conn.close()
    return render_template(
        "extrato.html",
        contas=contas,
        entradas=entradas,
        saidas=saidas,
        transferencias=transferencias,
        totais_entradas=totais_entradas,
        total_saidas=total_saidas,
        conta_id=conta_id,
        data_ini=data_ini,
        data_fim=data_fim,
    )


def _dados_fechamento(mes):
    """Retorna dict com todos os dados do fechamento de um mês."""
    conn = get_conn()
    entradas = conn.execute(
        """SELECT e.*, c.nome AS conta_nome, COALESCE(d.nome, e.nome_avulso) AS nome_pessoa
           FROM entradas e
           JOIN contas c ON c.id = e.conta_id
           LEFT JOIN dizimistas d ON d.id = e.dizimista_id
           WHERE to_char(e.data,'YYYY-MM') = ?
           ORDER BY e.data, e.criado_em""",
        (mes,),
    ).fetchall()
    saidas = conn.execute(
        """SELECT s.*, c.nome AS conta_nome
           FROM saidas s
           JOIN contas c ON c.id = s.conta_id
           WHERE to_char(s.data,'YYYY-MM') = ?
           ORDER BY s.data, s.criado_em""",
        (mes,),
    ).fetchall()
    conn.close()

    total_bruto = sum(float(e["valor"]) for e in entradas)
    total_saidas = sum(float(s["valor"]) for s in saidas)
    valor_sede = total_bruto * TAXA_SEDE
    valor_fundo = total_bruto * TAXA_FUNDO
    valor_regional = total_bruto * TAXA_REGIONAL
    valor_total_taxas = valor_sede + valor_fundo + valor_regional

    totais_entradas = _totais_por_tipo(entradas)

    totais_categorias = {cat: 0.0 for cat in CATEGORIAS_SAIDA}
    for s in saidas:
        cat = s["categoria"] or _CATEGORIA_FALLBACK
        if cat not in totais_categorias:
            cat = _CATEGORIA_FALLBACK
        totais_categorias[cat] += float(s["valor"])

    return dict(
        mes=mes,
        entradas=entradas,
        saidas=saidas,
        total_bruto=total_bruto,
        total_saidas=total_saidas,
        totais_entradas=totais_entradas,
        totais_categorias=totais_categorias,
        categorias_saida=CATEGORIAS_SAIDA,
        valor_sede=valor_sede,
        valor_fundo=valor_fundo,
        valor_regional=valor_regional,
        valor_total_taxas=valor_total_taxas,
        taxa_sede=TAXA_SEDE,
        taxa_fundo=TAXA_FUNDO,
        taxa_regional=TAXA_REGIONAL,
    )


@app.route("/fechamento")
def fechamento():
    hoje = date.today()
    mes = request.args.get("mes") or f"{hoje.year:04d}-{hoje.month:02d}"
    dados = _dados_fechamento(mes)
    return render_template("fechamento.html", **dados)


@app.route("/fechamento/pdf")
def fechamento_pdf():
    hoje = date.today()
    mes = request.args.get("mes") or f"{hoje.year:04d}-{hoje.month:02d}"
    dados = _dados_fechamento(mes)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    azul = colors.HexColor("#1a4d8f")
    verde = colors.HexColor("#2e8b3a")
    laranja = colors.HexColor("#e8650f")

    titulo_style = ParagraphStyle("titulo", parent=styles["Title"],
                                  fontSize=16, textColor=azul, spaceAfter=4)
    sub_style = ParagraphStyle("sub", parent=styles["Normal"],
                               fontSize=10, textColor=colors.grey, spaceAfter=12)
    secao_style = ParagraphStyle("secao", parent=styles["Heading2"],
                                 fontSize=12, textColor=azul, spaceBefore=12, spaceAfter=4)

    MESES_PT = {
        "01": "Janeiro","02": "Fevereiro","03": "Março","04": "Abril",
        "05": "Maio","06": "Junho","07": "Julho","08": "Agosto",
        "09": "Setembro","10": "Outubro","11": "Novembro","12": "Dezembro",
    }
    ano, mes_num = mes.split("-")
    mes_label = f"{MESES_PT[mes_num]}/{ano}"

    TIPO_LABEL = {
        "dizimo": "Dízimo",
        "oferta_nominal": "Oferta Nominal",
        "oferta_coletiva": "Oferta Coletiva",
    }

    def fmt(v):
        return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    story = []
    story.append(Paragraph("TESOURARIA ISOSED", titulo_style))
    story.append(Paragraph(f"Fechamento Mensal — {mes_label}", sub_style))
    story.append(HRFlowable(width="100%", thickness=1, color=azul))
    story.append(Spacer(1, 0.4*cm))

    # --- Entradas ---
    story.append(Paragraph("Entradas", secao_style))
    e_data = [["Data", "Tipo", "Pessoa/Descrição", "Conta", "Valor"]]
    for e in dados["entradas"]:
        e_data.append([
            str(e["data"]),
            TIPO_LABEL.get(e["tipo"], e["tipo"]),
            e["nome_pessoa"] or "-",
            e["conta_nome"],
            fmt(float(e["valor"])),
        ])
    e_data.append(["", "", "", "TOTAL", fmt(dados["total_bruto"])])

    t_e = Table(e_data, colWidths=[2.2*cm, 3.5*cm, 5.5*cm, 3*cm, 2.8*cm])
    t_e.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), azul),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#eef3fb")]),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#d4e8d4")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t_e)
    story.append(Spacer(1, 0.5*cm))

    # --- Despesas por categoria ---
    story.append(Paragraph("Despesas por Categoria", secao_style))
    cat_data = [["Categoria", "Total"]]
    for cat in CATEGORIAS_SAIDA:
        val = dados["totais_categorias"].get(cat, 0.0)
        if val > 0:
            cat_data.append([cat, fmt(val)])
    cat_data.append(["TOTAL SAÍDAS", fmt(dados["total_saidas"])])

    t_cat = Table(cat_data, colWidths=[12.5*cm, 4*cm])
    t_cat.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), azul),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTSIZE", (0, 1), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#fff4ee")]),
        ("BACKGROUND", (0, -1), (-1, -1), laranja),
        ("TEXTCOLOR", (0, -1), (-1, -1), colors.white),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t_cat)
    story.append(Spacer(1, 0.5*cm))

    # --- Saídas detalhadas ---
    story.append(Paragraph("Saídas Detalhadas", secao_style))
    s_data = [["Data", "Categoria", "Descrição", "Conta", "Valor"]]
    for s in dados["saidas"]:
        s_data.append([
            str(s["data"]),
            s["categoria"] or _CATEGORIA_FALLBACK,
            s["motivo"],
            s["conta_nome"],
            fmt(float(s["valor"])),
        ])
    s_data.append(["", "", "", "TOTAL", fmt(dados["total_saidas"])])

    t_s = Table(s_data, colWidths=[2*cm, 4.5*cm, 4.5*cm, 2.8*cm, 2.7*cm])
    t_s.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), azul),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#fff4ee")]),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#fde8d8")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t_s)
    story.append(Spacer(1, 0.5*cm))

    # --- Taxas ---
    story.append(Paragraph("Repasses às Sedes", secao_style))
    taxas_data = [
        ["Destino", "% sobre Bruto", "Valor"],
        ["Sede Mundial (Maringá)", f"{int(TAXA_SEDE*100)}%", fmt(dados["valor_sede"])],
        ["Fundo (Sede Mundial)", f"{int(TAXA_FUNDO*100)}%", fmt(dados["valor_fundo"])],
        ["Sede Regional (Francisco Beltrão)", f"{int(TAXA_REGIONAL*100)}%", fmt(dados["valor_regional"])],
        ["TOTAL DAS TAXAS (23%)", "", fmt(dados["valor_total_taxas"])],
    ]
    t_t = Table(taxas_data, colWidths=[9*cm, 3*cm, 3*cm])
    t_t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), azul),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTSIZE", (0, 1), (-1, -1), 9),
        ("BACKGROUND", (0, -1), (-1, -1), laranja),
        ("TEXTCOLOR", (0, -1), (-1, -1), colors.white),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t_t)
    story.append(Spacer(1, 0.4*cm))

    saldo_mes = dados["total_bruto"] - dados["total_saidas"]
    resumo_data = [
        ["Total Bruto Entradas", fmt(dados["total_bruto"])],
        ["Total Saídas", fmt(dados["total_saidas"])],
        ["Saldo do Mês", fmt(saldo_mes)],
    ]
    t_r = Table(resumo_data, colWidths=[9*cm, 6*cm])
    t_r.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -1), (-1, -1), verde if saldo_mes >= 0 else colors.HexColor("#fadbd8")),
        ("TEXTCOLOR", (0, -1), (-1, -1), colors.white if saldo_mes >= 0 else colors.HexColor("#922b21")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEABOVE", (0, 0), (-1, 0), 0.5, azul),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t_r)

    story.append(Spacer(1, 0.6*cm))
    story.append(Paragraph(
        f"Emitido em {date.today().strftime('%d/%m/%Y')} por {session.get('usuario_nome', '')}",
        ParagraphStyle("rodape", parent=styles["Normal"], fontSize=8, textColor=colors.grey),
    ))

    doc.build(story)
    buf.seek(0)
    resp = make_response(buf.read())
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f"attachment; filename=fechamento_{mes}.pdf"
    return resp


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


@app.route("/dizimistas/<int:dizimista_id>/editar", methods=["GET", "POST"])
@requer_papel("admin")
def editar_dizimista(dizimista_id):
    conn = get_conn()
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        if nome:
            conn.execute(
                "UPDATE dizimistas SET nome = ? WHERE id = ?", (nome, dizimista_id)
            )
            conn.commit()
            flash("Dizimista atualizado.", "success")
        else:
            flash("Informe um nome válido.", "error")
        conn.close()
        return redirect(url_for("dizimistas"))

    dizimista = conn.execute(
        "SELECT * FROM dizimistas WHERE id = ?", (dizimista_id,)
    ).fetchone()
    conn.close()
    if not dizimista:
        flash("Dizimista não encontrado.", "error")
        return redirect(url_for("dizimistas"))
    return render_template("editar_dizimista.html", dizimista=dizimista)


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


@app.route("/admin/contas/<int:conta_id>/editar", methods=["GET", "POST"])
@requer_papel("admin")
def editar_conta(conta_id):
    conn = get_conn()
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        tipo = request.form.get("tipo")
        if nome and tipo:
            conn.execute(
                "UPDATE contas SET nome = ?, tipo = ? WHERE id = ?",
                (nome, tipo, conta_id),
            )
            conn.commit()
            flash("Conta atualizada.", "success")
        else:
            flash("Preencha nome e tipo.", "error")
        conn.close()
        return redirect(url_for("admin_contas"))

    conta = conn.execute("SELECT * FROM contas WHERE id = ?", (conta_id,)).fetchone()
    conn.close()
    if not conta:
        flash("Conta não encontrada.", "error")
        return redirect(url_for("admin_contas"))
    return render_template("editar_conta.html", conta=conta)


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
