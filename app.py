from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import sqlite3
from pathlib import Path
import os
import json
import secrets
from datetime import date, datetime, timedelta
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

BASE = Path(__file__).resolve().parent

# Caminho do banco. Em produção (Render), aponte a variável de ambiente
# FG_DB_PATH para o arquivo dentro do disco persistente, por exemplo:
#   FG_DB_PATH=/var/data/fg_web.db
# Localmente, sem a variável, continua usando o arquivo ao lado do app.py.
DB = Path(os.environ.get("FG_DB_PATH", str(BASE / "fg_web.db")))

app = Flask(__name__)

# Chave usada para assinar o cookie de sessão (login). Em produção,
# defina a variável de ambiente FG_SECRET_KEY com um valor fixo —
# senão, toda vez que o serviço reiniciar, todo mundo é deslogado.
app.secret_key = os.environ.get("FG_SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=30)


# =========================================================
# CONEXÃO SQLITE
# =========================================================

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


# =========================================================
# BANCO
# =========================================================

def init():
    c = conn()

    c.executescript("""
    PRAGMA foreign_keys=ON;

    CREATE TABLE IF NOT EXISTS clientes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        legacy_id INTEGER,
        nome TEXT NOT NULL,
        cpf_cnpj TEXT,
        celular TEXT,
        telefone TEXT,
        email TEXT,
        cep TEXT,
        endereco TEXT,
        numero TEXT,
        complemento TEXT,
        bairro TEXT,
        cidade TEXT,
        uf TEXT,
        observacoes TEXT
    );

    CREATE TABLE IF NOT EXISTS equipamentos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        legacy_id INTEGER,
        cliente_id INTEGER NOT NULL,
        tipo TEXT,
        marca TEXT,
        modelo TEXT,
        serie TEXT,
        patrimonio TEXT,
        data_compra TEXT,
        nf TEXT,
        acessorios TEXT,
        observacoes TEXT,
        FOREIGN KEY(cliente_id) REFERENCES clientes(id)
    );

    CREATE TABLE IF NOT EXISTS ordens_servico (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        legacy_id INTEGER,
        numero_os INTEGER UNIQUE,
        cliente_id INTEGER NOT NULL,
        equipamento_id INTEGER NOT NULL,
        defeito TEXT,
        problema_identificado TEXT,
        observacoes TEXT,
        situacao TEXT DEFAULT 'Aguardando avaliação',
        data_entrada TEXT DEFAULT (datetime('now','-3 hours')),
        FOREIGN KEY(cliente_id) REFERENCES clientes(id),
        FOREIGN KEY(equipamento_id) REFERENCES equipamentos(id)
    );

    CREATE TABLE IF NOT EXISTS os_historico (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        os_id INTEGER NOT NULL,
        evento TEXT NOT NULL,
        criado_em TEXT DEFAULT (datetime('now','-3 hours')),
        FOREIGN KEY(os_id) REFERENCES ordens_servico(id)
    );

    CREATE TABLE IF NOT EXISTS os_servicos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        os_id INTEGER NOT NULL,
        descricao TEXT NOT NULL,
        quantidade REAL NOT NULL DEFAULT 1,
        valor REAL NOT NULL DEFAULT 0,
        criado_em TEXT DEFAULT (datetime('now','-3 hours')),
        FOREIGN KEY(os_id) REFERENCES ordens_servico(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS os_pecas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        os_id INTEGER NOT NULL,
        descricao TEXT NOT NULL,
        quantidade REAL NOT NULL DEFAULT 1,
        valor REAL NOT NULL DEFAULT 0,
        criado_em TEXT DEFAULT (datetime('now','-3 hours')),
        FOREIGN KEY(os_id) REFERENCES ordens_servico(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS os_orcamento_servicos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        os_id INTEGER NOT NULL,
        descricao TEXT NOT NULL,
        quantidade REAL NOT NULL DEFAULT 1,
        valor REAL NOT NULL DEFAULT 0,
        criado_em TEXT DEFAULT (datetime('now','-3 hours')),
        FOREIGN KEY(os_id) REFERENCES ordens_servico(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS os_orcamento_pecas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        os_id INTEGER NOT NULL,
        descricao TEXT NOT NULL,
        quantidade REAL NOT NULL DEFAULT 1,
        valor REAL NOT NULL DEFAULT 0,
        criado_em TEXT DEFAULT (datetime('now','-3 hours')),
        FOREIGN KEY(os_id) REFERENCES ordens_servico(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS financeiro_os (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        os_id INTEGER NOT NULL UNIQUE,
        pago INTEGER NOT NULL DEFAULT 0,
        data_pagamento TEXT,
        forma_pagamento TEXT,
        observacao TEXT,
        FOREIGN KEY(os_id) REFERENCES ordens_servico(id) ON DELETE CASCADE
    );

    -- Cópia fiel das tabelas do Access antigo (uma linha = uma linha
    -- exportada do MDB, guardada como JSON). Populadas uma única vez
    -- pelo script migrar_mdb.py, rodado localmente com WSL. Depois
    -- disso o app não precisa mais do Access nem do WSL para consultar
    -- clientes, equipamentos e OS antigos.
    CREATE TABLE IF NOT EXISTS mdb_clientes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dados TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS mdb_equipamentos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dados TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS mdb_ordens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dados TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS mdb_os_pecas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dados TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS mdb_os_servicos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dados TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome TEXT NOT NULL,
        usuario TEXT NOT NULL UNIQUE,
        senha_hash TEXT NOT NULL,
        papel TEXT NOT NULL DEFAULT 'funcionario',
        ativo INTEGER NOT NULL DEFAULT 1,
        criado_em TEXT DEFAULT (datetime('now','-3 hours'))
    );
    """)

    # Migração segura do banco já existente.
    cols = {row[1] for row in c.execute("PRAGMA table_info(ordens_servico)").fetchall()}
    if "finalizado" not in cols:
        c.execute("ALTER TABLE ordens_servico ADD COLUMN finalizado INTEGER NOT NULL DEFAULT 0")
    if "data_finalizacao" not in cols:
        c.execute("ALTER TABLE ordens_servico ADD COLUMN data_finalizacao TEXT")
    if "problema_identificado" not in cols:
        c.execute("ALTER TABLE ordens_servico ADD COLUMN problema_identificado TEXT")

    # Custos internos por item — não interferem no valor cobrado ao cliente.
    serv_cols = {row[1] for row in c.execute("PRAGMA table_info(os_servicos)").fetchall()}
    if "custo" not in serv_cols:
        c.execute("ALTER TABLE os_servicos ADD COLUMN custo REAL NOT NULL DEFAULT 0")

    pec_cols = {row[1] for row in c.execute("PRAGMA table_info(os_pecas)").fetchall()}
    if "custo" not in pec_cols:
        c.execute("ALTER TABLE os_pecas ADD COLUMN custo REAL NOT NULL DEFAULT 0")

    # Contador oficial de produção. A primeira OS nova será 000001.
    c.execute("""
        CREATE TABLE IF NOT EXISTS controle_os (
            id INTEGER PRIMARY KEY CHECK (id=1),
            proximo_numero INTEGER NOT NULL
        )
    """)
    c.execute(
        "INSERT OR IGNORE INTO controle_os (id, proximo_numero) VALUES (1, 14561)"
    )

    # Cria o primeiro usuário administrador, se ainda não existir nenhum.
    tem_usuario = c.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
    if not tem_usuario:
        usuario_inicial = os.environ.get("FG_ADMIN_USUARIO", "admin")
        senha_inicial = os.environ.get("FG_ADMIN_SENHA", "admin123")
        c.execute(
            "INSERT INTO usuarios (nome, usuario, senha_hash, papel) VALUES (?,?,?,?)",
            ("Administrador", usuario_inicial, generate_password_hash(senha_inicial), "admin")
        )
        print("=" * 60)
        print(f"FG_WEB: usuário administrador criado -> {usuario_inicial} / {senha_inicial}")
        print("Troque essa senha assim que possível (tela de Usuários).")
        print("=" * 60)

    c.commit()
    c.close()


# =========================================================
# AUXILIARES
# =========================================================

def formatar_os(valor):
    """Número oficial de OS para exibição: sempre 6 dígitos."""
    try:
        return f"{int(valor):06d}"
    except (TypeError, ValueError):
        return str(valor or "")


def limpar(valor):
    if valor is None:
        return ""
    return str(valor).strip()


def agora_br():
    """Hora atual no fuso do Brasil (UTC-3, sem horário de verão).
    Usado sempre que o código grava um horário na mão — não dá pra
    confiar no DEFAULT da coluna porque ele só vale para tabelas
    criadas do zero, não para as que já existiam no banco."""
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")


# =========================================================
# AUTENTICAÇÃO
# =========================================================

def login_obrigatorio(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("usuario_id"):
            if request.path.startswith("/api/"):
                return jsonify(erro="Sessão expirada. Faça login novamente."), 401
            return redirect(url_for("tela_login", proximo=request.path))
        return view(*args, **kwargs)
    return wrapper


def apenas_admin(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("usuario_id"):
            if request.path.startswith("/api/"):
                return jsonify(erro="Sessão expirada. Faça login novamente."), 401
            return redirect(url_for("tela_login", proximo=request.path))
        if session.get("papel") != "admin":
            if request.path.startswith("/api/"):
                return jsonify(erro="Só o administrador pode acessar isso."), 403
            return redirect(url_for("home"))
        return view(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def tela_login():
    if session.get("usuario_id"):
        return redirect(url_for("home"))

    erro = None

    if request.method == "POST":
        usuario = limpar(request.form.get("usuario"))
        senha = request.form.get("senha") or ""

        c = conn()
        row = c.execute(
            "SELECT * FROM usuarios WHERE usuario=? AND ativo=1",
            (usuario,)
        ).fetchone()
        c.close()

        if row and check_password_hash(row["senha_hash"], senha):
            session.clear()
            session["usuario_id"] = row["id"]
            session["usuario_nome"] = row["nome"]
            session["papel"] = row["papel"]
            session.permanent = True
            proximo = request.args.get("proximo") or url_for("home")
            return redirect(proximo)

        erro = "Usuário ou senha inválidos."

    return render_template("login.html", erro=erro)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("tela_login"))


@app.get("/usuarios")
@apenas_admin
def tela_usuarios():
    return render_template("usuarios.html", active="usuarios")


@app.get("/api/usuarios")
@apenas_admin
def listar_usuarios():
    c = conn()
    rows = c.execute(
        "SELECT id, nome, usuario, papel, ativo, criado_em FROM usuarios ORDER BY nome"
    ).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])


@app.post("/api/usuarios")
@apenas_admin
def criar_usuario():
    d = request.get_json() or {}
    nome = limpar(d.get("nome"))
    usuario = limpar(d.get("usuario"))
    senha = d.get("senha") or ""
    papel = d.get("papel") if d.get("papel") in ("admin", "funcionario") else "funcionario"

    if not nome or not usuario or not senha:
        return jsonify(erro="Nome, usuário e senha são obrigatórios."), 400
    if len(senha) < 6:
        return jsonify(erro="A senha precisa ter pelo menos 6 caracteres."), 400

    c = conn()
    existe = c.execute("SELECT id FROM usuarios WHERE usuario=?", (usuario,)).fetchone()
    if existe:
        c.close()
        return jsonify(erro="Já existe um usuário com esse login."), 400

    c.execute(
        "INSERT INTO usuarios (nome, usuario, senha_hash, papel) VALUES (?,?,?,?)",
        (nome, usuario, generate_password_hash(senha), papel)
    )
    c.commit()
    c.close()
    return jsonify(ok=True)


@app.put("/api/usuarios/<int:usuario_id>")
@apenas_admin
def editar_usuario(usuario_id):
    d = request.get_json() or {}
    c = conn()
    existente = c.execute("SELECT * FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    if not existente:
        c.close()
        return jsonify(erro="Usuário não encontrado."), 404

    nome = limpar(d.get("nome")) or existente["nome"]
    papel = d.get("papel") if d.get("papel") in ("admin", "funcionario") else existente["papel"]
    ativo = 1 if d.get("ativo", bool(existente["ativo"])) else 0

    # Impede desativar/rebaixar o último admin ativo do sistema.
    if (papel != "admin" or not ativo) and existente["papel"] == "admin":
        outros_admins = c.execute(
            "SELECT COUNT(*) FROM usuarios WHERE papel='admin' AND ativo=1 AND id!=?",
            (usuario_id,)
        ).fetchone()[0]
        if not outros_admins:
            c.close()
            return jsonify(erro="Precisa existir pelo menos um administrador ativo."), 400

    if d.get("senha"):
        if len(d["senha"]) < 6:
            c.close()
            return jsonify(erro="A senha precisa ter pelo menos 6 caracteres."), 400
        c.execute(
            "UPDATE usuarios SET nome=?, papel=?, ativo=?, senha_hash=? WHERE id=?",
            (nome, papel, ativo, generate_password_hash(d["senha"]), usuario_id)
        )
    else:
        c.execute(
            "UPDATE usuarios SET nome=?, papel=?, ativo=? WHERE id=?",
            (nome, papel, ativo, usuario_id)
        )

    c.commit()
    c.close()
    return jsonify(ok=True)


def cliente_mdb_para_json(row):
    return {
        "id": None,
        "legacy": True,
        "legacy_id": limpar(row.get("CODIGO")),
        "nome": limpar(row.get("NOME")),
        "cpf_cnpj": limpar(row.get("CPF_CNPJ")),
        "celular": limpar(row.get("CELULAR")),
        "telefone": limpar(row.get("TELEFONE")),
        "email": limpar(row.get("EMAIL")),
        "cep": limpar(row.get("CEP")),
        "endereco": limpar(row.get("ENDERECO")),
        "numero": limpar(row.get("NUMERO")),
        "complemento": limpar(row.get("COMPLEM")),
        "bairro": limpar(row.get("BAIRRO")),
        "cidade": limpar(row.get("CIDADE")),
        "uf": limpar(row.get("UF")),
        "observacoes": limpar(row.get("OBSERVACAO"))
    }


# =========================================================
# CLIENTES DO MDB
# =========================================================

_mdb_clientes_cache = None


def _carregar_mdb_tabela(tabela_sqlite, nome_exibicao):
    """Lê uma tabela-espelho do Access (populada por migrar_mdb.py) e
    devolve a lista de linhas no mesmo formato que o antigo
    csv.DictReader produzia — sem exigir WSL/Access em produção."""
    c = conn()
    try:
        rows = c.execute(f"SELECT dados FROM {tabela_sqlite}").fetchall()
    except sqlite3.OperationalError:
        rows = []
    c.close()

    linhas = [json.loads(r["dados"]) for r in rows]
    print(f"MDB (local): {len(linhas)} linhas de {nome_exibicao}.")
    return linhas


def carregar_clientes_mdb():
    global _mdb_clientes_cache

    if _mdb_clientes_cache is not None:
        return _mdb_clientes_cache

    clientes = [
        row for row in _carregar_mdb_tabela("mdb_clientes", "clientes")
        if limpar(row.get("NOME"))
    ]
    _mdb_clientes_cache = clientes
    return clientes


def pesquisar_clientes_mdb(q):
    q = limpar(q).casefold()

    if not q:
        return []

    clientes = carregar_clientes_mdb()
    encontrados = []

    campos = [
        "NOME",
        "CPF_CNPJ",
        "TELEFONE",
        "CELULAR",
        "EMAIL",
        "CEP",
        "ENDERECO",
        "NUMERO",
        "COMPLEM",
        "BAIRRO",
        "CIDADE",
        "UF"
    ]

    for row in clientes:
        if any(q in limpar(row.get(campo)).casefold() for campo in campos):
            encontrados.append(cliente_mdb_para_json(row))

        if len(encontrados) >= 30:
            break

    return encontrados


# =========================================================
# EQUIPAMENTOS ANTIGOS DO MDB
#
# O MDB antigo usa a tabela ORDEMS e nela ficam os dados
# do equipamento da OS:
# COD_CLIENTE, COD_EQUIP, APARELHO, MARCA, MODELO,
# SERIE, PATRIMONIO, ACESSORIO...
# =========================================================

_mdb_ordens_cache = None


def carregar_os_mdb():
    global _mdb_ordens_cache

    if _mdb_ordens_cache is not None:
        return _mdb_ordens_cache

    ordens = _carregar_mdb_tabela("mdb_ordens", "OS antigas")
    _mdb_ordens_cache = ordens
    return ordens


# =========================================================
# ITENS DA OS ANTIGA - PEÇAS E SERVIÇOS
# =========================================================

_mdb_os_pecas_cache = None
_mdb_os_servicos_cache = None


def carregar_os_pecas_mdb():
    global _mdb_os_pecas_cache

    if _mdb_os_pecas_cache is not None:
        return _mdb_os_pecas_cache

    _mdb_os_pecas_cache = _carregar_mdb_tabela("mdb_os_pecas", "peças (OS antigas)")
    return _mdb_os_pecas_cache


def carregar_os_servicos_mdb():
    global _mdb_os_servicos_cache

    if _mdb_os_servicos_cache is not None:
        return _mdb_os_servicos_cache

    _mdb_os_servicos_cache = _carregar_mdb_tabela("mdb_os_servicos", "serviços (OS antigas)")
    return _mdb_os_servicos_cache


_mdb_equipamentos_cache = None


def carregar_equipamentos_mdb():
    """Carrega o cadastro real de equipamentos da tabela EQUIPAMENTOS do MDB
    (já copiada localmente para mdb_equipamentos por migrar_mdb.py)."""
    global _mdb_equipamentos_cache

    if _mdb_equipamentos_cache is not None:
        return _mdb_equipamentos_cache

    equipamentos = _carregar_mdb_tabela("mdb_equipamentos", "equipamentos antigos")
    _mdb_equipamentos_cache = equipamentos
    return equipamentos


def equipamento_mdb_para_json(row, cliente_legacy_id=None):
    codigo = limpar(row.get("CODIGO"))
    descricao = limpar(row.get("DESCRICAO"))

    # O cadastro 1946, por exemplo, existe no MDB, mas todos os campos
    # descritivos estão vazios. Nunca devolvemos um equipamento antigo
    # sem uma identificação visual: usamos o próprio código do MDB.
    identificacao = descricao or (f"Equipamento antigo #{codigo}" if codigo else "Equipamento antigo")

    return {
        "id": None,
        "legacy": True,
        "legacy_id": codigo,
        "codigo_equipamento": codigo,
        "tipo": identificacao,
        "aparelho": identificacao,
        "marca": limpar(row.get("MARCA")),
        "modelo": limpar(row.get("MODELO")),
        "serie": limpar(row.get("SERIE")),
        "patrimonio": limpar(row.get("PAT")),
        "acessorios": limpar(row.get("OBSERVACOES")),
        "observacoes": limpar(row.get("OBSERVACOES")),
        "data_compra": limpar(row.get("DATA_COMPRA")),
        "revenda": limpar(row.get("REVENDA")),
        "num_nf": limpar(row.get("NUM_NF")),
        "num_certgar": limpar(row.get("NUM_CERTGAR")),
        "cliente_legacy_id": cliente_legacy_id or limpar(row.get("COD_CLIENTE"))
    }


def pesquisar_equipamentos_mdb(legacy_id):
    """
    Descobre os COD_EQUIP usados pelo cliente nas ORDEMS e busca
    os dados reais desses equipamentos na tabela EQUIPAMENTOS.

    A ORDEMS serve somente para descobrir o vínculo cliente -> equipamento.
    A descrição, marca, modelo, série e patrimônio vêm da tabela
    EQUIPAMENTOS do MDB.
    """
    legacy_id = limpar(legacy_id)
    if not legacy_id:
        return []

    ordens = carregar_os_mdb()
    codigos = []
    vistos = set()

    for row in ordens:
        if limpar(row.get("COD_CLIENTE")) != legacy_id:
            continue
        codigo = limpar(row.get("COD_EQUIP"))
        if codigo and codigo not in vistos:
            vistos.add(codigo)
            codigos.append(codigo)

    if not codigos:
        return []

    cadastro = carregar_equipamentos_mdb()
    por_codigo = {limpar(r.get("CODIGO")): r for r in cadastro}

    encontrados = []
    for codigo in codigos:
        row = por_codigo.get(codigo)
        if row is None:
            # Mantém a referência mesmo se o cadastro do equipamento não existir.
            encontrados.append({
                "id": None,
                "legacy": True,
                "legacy_id": codigo,
                "codigo_equipamento": codigo,
                "tipo": f"Equipamento antigo #{codigo}",
                "aparelho": f"Equipamento antigo #{codigo}",
                "marca": "",
                "modelo": "",
                "serie": "",
                "patrimonio": "",
                "acessorios": "",
                "observacoes": "",
                "cliente_legacy_id": legacy_id
            })
        else:
            encontrados.append(
                equipamento_mdb_para_json(row, legacy_id)
            )

    return encontrados

# =========================================================
# OS ANTIGAS
# =========================================================

def os_mdb_para_json(row):
    return {
        "id": None,
        "legacy": True,
        "legacy_id": limpar(row.get("CODIGO")),
        "numero_os": limpar(row.get("CODIGO")),
        "cliente_legacy_id": limpar(row.get("COD_CLIENTE")),
        "entrada": limpar(row.get("ENTRADA")),
        "pronto": limpar(row.get("PRONTO")),
        "saida": limpar(row.get("SAIDA")),
        "garantia": limpar(row.get("GARANTIA")),
        "situacao": limpar(row.get("SITUACAO")),
        "valor_mao": limpar(row.get("V_MAO")),
        "valor_pecas": limpar(row.get("V_PECAS")),
        "valor_deslocamento": limpar(row.get("V_DESLOCA")),
        "valor_terceiro": limpar(row.get("V_TERCEIRO")),
        "valor_outros": limpar(row.get("V_OUTROS")),
        "cod_equip": limpar(row.get("COD_EQUIP")),
        "aparelho": limpar(row.get("APARELHO")),
        "marca": limpar(row.get("MARCA")),
        "modelo": limpar(row.get("MODELO")),
        "serie": limpar(row.get("SERIE")),
        "patrimonio": limpar(row.get("PATRIMONIO")),
        "acessorio": limpar(row.get("ACESSORIO")),
        "defeito": limpar(row.get("DEFEITO")),
        "laudo": limpar(row.get("LAUDO")),
        "obs_aparelho": limpar(row.get("OBS_APARELHO")),
        "kilomet": limpar(row.get("KILOMET")),
        "num_nf_ped": limpar(row.get("NUM_NF_PED")),
        "nf_numero": limpar(row.get("NF_NUMERO")),
        "os_reaberta": limpar(row.get("OS_REABERTA")),
        "os_outros": limpar(row.get("OS_OUTROS"))
    }


def pesquisar_os_mdb(cod_cliente):
    cod_cliente = limpar(cod_cliente)

    if not cod_cliente:
        return []

    ordens = carregar_os_mdb()
    encontrados = []

    for row in ordens:
        if limpar(row.get("COD_CLIENTE")) == cod_cliente:
            encontrados.append(os_mdb_para_json(row))

    def chave(x):
        try:
            return int(x["numero_os"])
        except Exception:
            return 0

    encontrados.sort(key=chave, reverse=True)

    return encontrados


# =========================================================
# TELA PRINCIPAL
# =========================================================

@app.route("/")
@login_obrigatorio
def home():
    return render_template("index.html", active="dashboard")


@app.route("/consulta")
@login_obrigatorio
def consulta():
    return render_template("consulta.html", active="consulta")

@app.route("/clientes")
@login_obrigatorio
def tela_clientes():
    return render_template("clientes.html", active="clientes")


@app.route("/impressao/<int:os_id>")
@login_obrigatorio
def impressao_os(os_id):
    return render_template("impressao.html", os_id=os_id)


@app.route("/atendimento/<int:os_id>")
@login_obrigatorio
def atendimento(os_id):
    return render_template("atendimento.html", os_id=os_id, active="consulta")



@app.put("/api/equipamentos/<int:equipamento_id>")
@login_obrigatorio
def editar_equipamento(equipamento_id):
    d = request.get_json() or {}
    c = conn()

    equipamento = c.execute(
        "SELECT id, cliente_id FROM equipamentos WHERE id=?",
        (equipamento_id,)
    ).fetchone()

    if not equipamento:
        c.close()
        return jsonify(erro="Equipamento não encontrado."), 404

    # Não permitir alterar um equipamento de uma OS finalizada.
    os_aberta = c.execute(
        """
        SELECT o.id
        FROM ordens_servico o
        WHERE o.equipamento_id=?
          AND COALESCE(o.finalizado, 0)=0
        ORDER BY o.id DESC
        LIMIT 1
        """,
        (equipamento_id,)
    ).fetchone()

    if not os_aberta:
        # Também permitimos edição quando não há OS aberta, mas a partir da tela
        # de atendimento normalmente sempre haverá uma OS em edição.
        pass

    tipo = str(d.get("tipo") or "").strip()
    marca = str(d.get("marca") or "").strip()
    modelo = str(d.get("modelo") or "").strip()
    serie = str(d.get("serie") or "").strip()
    patrimonio = str(d.get("patrimonio") or "").strip()
    data_compra = str(d.get("data_compra") or "").strip()
    nf = str(d.get("nf") or "").strip()
    acessorios = str(d.get("acessorios") or "").strip()
    observacoes = str(d.get("observacoes") or "").strip()

    if not tipo and not marca and not modelo:
        c.close()
        return jsonify(erro="Informe pelo menos o tipo, a marca ou o modelo do equipamento."), 400

    c.execute(
        """
        UPDATE equipamentos
        SET tipo=?, marca=?, modelo=?, serie=?, patrimonio=?,
            data_compra=?, nf=?, acessorios=?, observacoes=?
        WHERE id=?
        """,
        (
            tipo, marca, modelo, serie, patrimonio,
            data_compra, nf, acessorios, observacoes,
            equipamento_id
        )
    )

    # Registra a alteração no histórico da OS aberta relacionada ao equipamento.
    if os_aberta:
        c.execute(
            "INSERT INTO os_historico(os_id, evento, criado_em) VALUES (?, ?, ?)",
            (os_aberta["id"], "Dados/configurações do equipamento atualizados", agora_br())
        )

    c.commit()
    c.close()

    return jsonify(
        ok=True,
        equipamento={
            "id": equipamento_id,
            "tipo": tipo,
            "marca": marca,
            "modelo": modelo,
            "serie": serie,
            "patrimonio": patrimonio,
            "data_compra": data_compra,
            "nf": nf,
            "acessorios": acessorios,
            "observacoes": observacoes
        }
    )


# =========================================================
# CLIENTES
# =========================================================

@app.get("/api/clientes")
@login_obrigatorio
def clientes():
    q = (request.args.get("q") or "").strip()
    por_pagina = 30
    try:
        pagina = max(int(request.args.get("pagina") or 1), 1)
    except ValueError:
        pagina = 1
    offset = (pagina - 1) * por_pagina

    c = conn()

    if not q:
        total = c.execute("SELECT COUNT(*) FROM clientes").fetchone()[0]
        total_paginas = max((total + por_pagina - 1) // por_pagina, 1)
        pagina = min(pagina, total_paginas)
        offset = (pagina - 1) * por_pagina

        rows = c.execute(
            """
            SELECT *
            FROM clientes
            ORDER BY nome
            LIMIT ? OFFSET ?
            """,
            (por_pagina, offset)
        ).fetchall()

        resultado = []

        for x in rows:
            d = dict(x)
            d["legacy"] = False
            resultado.append(d)

        c.close()
        return jsonify({
            "resultados": resultado,
            "total": total,
            "pagina": pagina,
            "total_paginas": total_paginas,
            "por_pagina": por_pagina
        })

    like = f"%{q}%"

    total = c.execute(
        """
        SELECT COUNT(*)
        FROM clientes
        WHERE
            nome LIKE ?
            OR cpf_cnpj LIKE ?
            OR celular LIKE ?
            OR telefone LIKE ?
            OR email LIKE ?
            OR cep LIKE ?
            OR endereco LIKE ?
            OR numero LIKE ?
            OR complemento LIKE ?
            OR bairro LIKE ?
            OR cidade LIKE ?
            OR uf LIKE ?
        """,
        [like] * 12
    ).fetchone()[0]
    total_paginas = max((total + por_pagina - 1) // por_pagina, 1)
    pagina = min(pagina, total_paginas)
    offset = (pagina - 1) * por_pagina

    rows = c.execute(
        """
        SELECT *
        FROM clientes
        WHERE
            nome LIKE ?
            OR cpf_cnpj LIKE ?
            OR celular LIKE ?
            OR telefone LIKE ?
            OR email LIKE ?
            OR cep LIKE ?
            OR endereco LIKE ?
            OR numero LIKE ?
            OR complemento LIKE ?
            OR bairro LIKE ?
            OR cidade LIKE ?
            OR uf LIKE ?
        ORDER BY nome
        LIMIT ? OFFSET ?
        """,
        [like] * 12 + [por_pagina, offset]
    ).fetchall()

    resultado = []

    for x in rows:
        d = dict(x)
        d["legacy"] = False
        resultado.append(d)

    c.close()

    # Também pesquisa o MDB mesmo quando já encontrou no banco novo.
    # Isso permite que dois clientes com o mesmo nome (um novo e um antigo)
    # apareçam juntos na tela. Só na primeira página, pra não repetir
    # os mesmos legados em toda página da busca.
    if pagina == 1:
        antigos = pesquisar_clientes_mdb(q)

        # Evita duplicar um cliente antigo que já foi importado para o FG_WEB.
        legacy_ids_novos = {
            limpar(x.get("legacy_id"))
            for x in resultado
            if x.get("legacy_id") is not None
        }

        for antigo in antigos:
            if limpar(antigo.get("legacy_id")) not in legacy_ids_novos:
                resultado.append(antigo)

    return jsonify({
        "resultados": resultado,
        "total": total,
        "pagina": pagina,
        "total_paginas": total_paginas,
        "por_pagina": por_pagina
    })



# =========================================================
# IMPORTAR CLIENTE ANTIGO PARA O FG_WEB
# =========================================================

@app.post("/api/clientes/legacy")
@login_obrigatorio
def importar_cliente_legacy():
    d = request.get_json() or {}
    legacy_id = limpar(d.get("legacy_id"))

    if not legacy_id:
        return jsonify(
            erro="Código do cliente antigo é obrigatório."
        ), 400

    c = conn()

    # Se já foi importado anteriormente, apenas devolve o cadastro novo.
    cliente = c.execute(
        """
        SELECT *
        FROM clientes
        WHERE legacy_id=?
        """,
        (legacy_id,)
    ).fetchone()

    if cliente:
        c.close()
        return jsonify({
            "cliente": dict(cliente)
        })

    # Procura o cadastro original no MDB.
    antigos = carregar_clientes_mdb()
    antigo = next(
        (
            x for x in antigos
            if limpar(x.get("CODIGO")) == legacy_id
        ),
        None
    )

    if not antigo:
        c.close()
        return jsonify(
            erro="Cliente antigo não encontrado no MDB."
        ), 404

    dados = cliente_mdb_para_json(antigo)

    cur = c.execute(
        """
        INSERT INTO clientes
        (
            legacy_id,
            nome,
            cpf_cnpj,
            celular,
            telefone,
            email,
            cep,
            endereco,
            numero,
            complemento,
            bairro,
            cidade,
            uf,
            observacoes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            legacy_id,
            dados["nome"],
            dados["cpf_cnpj"],
            dados["celular"],
            dados["telefone"],
            dados["email"],
            dados["cep"],
            dados["endereco"],
            dados["numero"],
            dados["complemento"],
            dados["bairro"],
            dados["cidade"],
            dados["uf"],
            dados["observacoes"]
        )
    )

    c.commit()

    cliente = c.execute(
        """
        SELECT *
        FROM clientes
        WHERE id=?
        """,
        (cur.lastrowid,)
    ).fetchone()

    c.close()

    return jsonify({
        "cliente": dict(cliente)
    })


# =========================================================
# NOVO CLIENTE
# =========================================================

@app.post("/api/clientes")
@login_obrigatorio
def add_cliente():
    d = request.get_json() or {}

    nome = (d.get("nome") or "").strip()

    if not nome:
        return jsonify(erro="Nome é obrigatório."), 400

    c = conn()

    cur = c.execute(
        """
        INSERT INTO clientes
        (
            nome,
            cpf_cnpj,
            celular,
            telefone,
            email,
            cep,
            endereco,
            numero,
            complemento,
            bairro,
            cidade,
            uf,
            observacoes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            d.get(k)
            for k in [
                "nome",
                "cpf_cnpj",
                "celular",
                "telefone",
                "email",
                "cep",
                "endereco",
                "numero",
                "complemento",
                "bairro",
                "cidade",
                "uf",
                "observacoes"
            ]
        ]
    )

    c.commit()

    row = c.execute(
        "SELECT * FROM clientes WHERE id=?",
        (cur.lastrowid,)
    ).fetchone()

    c.close()

    resultado = dict(row)
    resultado["legacy"] = False

    return jsonify(resultado)


# =========================================================
# EDITAR CLIENTE
# =========================================================

@app.put("/api/clientes/<int:cid>")
@login_obrigatorio
def editar_cliente(cid):
    d = request.get_json() or {}

    nome = (d.get("nome") or "").strip()

    if not nome:
        return jsonify(erro="Nome é obrigatório."), 400

    campos = [
        "nome",
        "cpf_cnpj",
        "celular",
        "telefone",
        "email",
        "cep",
        "endereco",
        "numero",
        "complemento",
        "bairro",
        "cidade",
        "uf",
        "observacoes"
    ]

    valores = [d.get(k) for k in campos]
    valores.append(cid)

    c = conn()

    existente = c.execute(
        "SELECT id FROM clientes WHERE id=?",
        (cid,)
    ).fetchone()

    if not existente:
        c.close()
        return jsonify(erro="Cliente não encontrado."), 404

    c.execute(
        """
        UPDATE clientes
        SET
            nome=?,
            cpf_cnpj=?,
            celular=?,
            telefone=?,
            email=?,
            cep=?,
            endereco=?,
            numero=?,
            complemento=?,
            bairro=?,
            cidade=?,
            uf=?,
            observacoes=?
        WHERE id=?
        """,
        valores
    )

    c.commit()

    row = c.execute(
        "SELECT * FROM clientes WHERE id=?",
        (cid,)
    ).fetchone()

    c.close()

    resultado = dict(row)
    resultado["legacy"] = False

    return jsonify(resultado)


# =========================================================
# EQUIPAMENTOS DO CLIENTE NOVO
# =========================================================

@app.get("/api/clientes/<int:cid>/equipamentos")
@login_obrigatorio
def equips(cid):
    c = conn()

    rows = c.execute(
        """
        SELECT *
        FROM equipamentos
        WHERE cliente_id=?
        ORDER BY id DESC
        """,
        (cid,)
    ).fetchall()

    c.close()

    return jsonify([dict(x) for x in rows])


# =========================================================
# EQUIPAMENTOS DO CLIENTE ANTIGO
# =========================================================

@app.get("/api/clientes/legacy/<int:legacy_id>/equipamentos")
@login_obrigatorio
def equips_legacy(legacy_id):
    equipamentos = pesquisar_equipamentos_mdb(legacy_id)

    return jsonify(equipamentos)


# =========================================================
# NOVO EQUIPAMENTO
# =========================================================

@app.post("/api/equipamentos")
@login_obrigatorio
def add_equip():
    d = request.get_json() or {}

    c = conn()

    # -----------------------------------------------------
    # CLIENTE NOVO
    # -----------------------------------------------------
    if d.get("cliente_id"):

        cliente = c.execute(
            """
            SELECT *
            FROM clientes
            WHERE id=?
            """,
            (d.get("cliente_id"),)
        ).fetchone()

        if not cliente:
            c.close()
            return jsonify(
                erro="Cliente não encontrado."
            ), 404

        cliente_id = cliente["id"]

    # -----------------------------------------------------
    # CLIENTE ANTIGO
    # -----------------------------------------------------
    elif d.get("cliente_legacy_id"):

        legacy_id = limpar(
            d.get("cliente_legacy_id")
        )

        cliente = c.execute(
            """
            SELECT *
            FROM clientes
            WHERE legacy_id=?
            """,
            (legacy_id,)
        ).fetchone()

        # Ainda não foi importado para o FG_WEB.
        if not cliente:

            antigos = carregar_clientes_mdb()

            antigo = next(
                (
                    x for x in antigos
                    if limpar(x.get("CODIGO")) == legacy_id
                ),
                None
            )

            if not antigo:
                c.close()
                return jsonify(
                    erro="Cliente antigo não encontrado no MDB."
                ), 404

            dados_cliente = cliente_mdb_para_json(
                antigo
            )

            cur_cliente = c.execute(
                """
                INSERT INTO clientes
                (
                    legacy_id,
                    nome,
                    cpf_cnpj,
                    celular,
                    telefone,
                    email,
                    cep,
                    endereco,
                    numero,
                    complemento,
                    bairro,
                    cidade,
                    uf,
                    observacoes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    legacy_id,
                    dados_cliente["nome"],
                    dados_cliente["cpf_cnpj"],
                    dados_cliente["celular"],
                    dados_cliente["telefone"],
                    dados_cliente["email"],
                    dados_cliente["cep"],
                    dados_cliente["endereco"],
                    dados_cliente["numero"],
                    dados_cliente["complemento"],
                    dados_cliente["bairro"],
                    dados_cliente["cidade"],
                    dados_cliente["uf"],
                    dados_cliente["observacoes"]
                )
            )

            cliente = c.execute(
                """
                SELECT *
                FROM clientes
                WHERE id=?
                """,
                (cur_cliente.lastrowid,)
            ).fetchone()

        cliente_id = cliente["id"]

    else:

        c.close()

        return jsonify(
            erro="Cliente obrigatório."
        ), 400


    # -----------------------------------------------------
    # GRAVA EQUIPAMENTO
    # -----------------------------------------------------

    cur = c.execute(
        """
        INSERT INTO equipamentos
        (
            legacy_id,
            cliente_id,
            tipo,
            marca,
            modelo,
            serie,
            patrimonio,
            data_compra,
            nf,
            acessorios,
            observacoes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            d.get("legacy_id"),
            cliente_id,
            d.get("tipo"),
            d.get("marca"),
            d.get("modelo"),
            d.get("serie"),
            d.get("patrimonio"),
            d.get("data_compra"),
            d.get("nf"),
            d.get("acessorios"),
            d.get("observacoes")
        )
    )

    c.commit()

    row = c.execute(
        """
        SELECT *
        FROM equipamentos
        WHERE id=?
        """,
        (cur.lastrowid,)
    ).fetchone()

    cliente_row = c.execute(
        """
        SELECT *
        FROM clientes
        WHERE id=?
        """,
        (cliente_id,)
    ).fetchone()

    c.close()

    return jsonify({
        "cliente": dict(cliente_row),
        "equipamento": dict(row)
    })


# =========================================================
# IMPORTAR EQUIPAMENTO ANTIGO PARA O FG_WEB
# =========================================================

@app.post("/api/equipamentos/legacy")
@login_obrigatorio
def importar_equipamento_legacy():

    d = request.get_json() or {}

    legacy_cliente_id = limpar(
        d.get("cliente_legacy_id")
    )

    if not legacy_cliente_id:
        return jsonify(
            erro="Código do cliente antigo é obrigatório."
        ), 400

    c = conn()

    # -----------------------------------------------------
    # LOCALIZA OU CRIA O CLIENTE NO FG_WEB
    # -----------------------------------------------------

    cliente = c.execute(
        """
        SELECT *
        FROM clientes
        WHERE legacy_id=?
        """,
        (legacy_cliente_id,)
    ).fetchone()

    if not cliente:

        antigos = carregar_clientes_mdb()

        antigo = next(
            (
                x for x in antigos
                if limpar(x.get("CODIGO")) ==
                   legacy_cliente_id
            ),
            None
        )

        if not antigo:
            c.close()
            return jsonify(
                erro="Cliente antigo não encontrado no MDB."
            ), 404

        dados = cliente_mdb_para_json(antigo)

        cur_cliente = c.execute(
            """
            INSERT INTO clientes
            (
                legacy_id,
                nome,
                cpf_cnpj,
                celular,
                telefone,
                email,
                cep,
                endereco,
                numero,
                complemento,
                bairro,
                cidade,
                uf,
                observacoes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                legacy_cliente_id,
                dados["nome"],
                dados["cpf_cnpj"],
                dados["celular"],
                dados["telefone"],
                dados["email"],
                dados["cep"],
                dados["endereco"],
                dados["numero"],
                dados["complemento"],
                dados["bairro"],
                dados["cidade"],
                dados["uf"],
                dados["observacoes"]
            )
        )

        cliente = c.execute(
            """
            SELECT *
            FROM clientes
            WHERE id=?
            """,
            (cur_cliente.lastrowid,)
        ).fetchone()


    cliente_id = cliente["id"]

    # -----------------------------------------------------
    # VERIFICA SE O EQUIPAMENTO JÁ FOI IMPORTADO
    # -----------------------------------------------------

    legacy_equip_id = limpar(
        d.get("legacy_id")
    )

    equipamento = None

    if legacy_equip_id:

        equipamento = c.execute(
            """
            SELECT *
            FROM equipamentos
            WHERE cliente_id=?
              AND legacy_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                cliente_id,
                legacy_equip_id
            )
        ).fetchone()


    # -----------------------------------------------------
    # CRIA O EQUIPAMENTO SE NECESSÁRIO
    # -----------------------------------------------------

    if not equipamento:

        cur_equip = c.execute(
            """
            INSERT INTO equipamentos
            (
                legacy_id,
                cliente_id,
                tipo,
                marca,
                modelo,
                serie,
                patrimonio,
                acessorios,
                observacoes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                legacy_equip_id or None,
                cliente_id,
                d.get("tipo"),
                d.get("marca"),
                d.get("modelo"),
                d.get("serie"),
                d.get("patrimonio"),
                d.get("acessorios"),
                d.get("observacoes")
            )
        )

        equipamento = c.execute(
            """
            SELECT *
            FROM equipamentos
            WHERE id=?
            """,
            (cur_equip.lastrowid,)
        ).fetchone()


    c.commit()
    c.close()

    return jsonify({
        "cliente": dict(cliente),
        "equipamento": dict(equipamento)
    })


# =========================================================
# HISTÓRICO DE OS DO CLIENTE NOVO
# =========================================================

@app.get("/api/clientes/<int:cid>/historico")
@login_obrigatorio
def historico_cliente(cid):
    c = conn()

    cliente = c.execute(
        "SELECT * FROM clientes WHERE id=?",
        (cid,)
    ).fetchone()

    if not cliente:
        c.close()
        return jsonify([])

    cliente = dict(cliente)

    rows = c.execute(
        """
        SELECT *
        FROM ordens_servico
        WHERE cliente_id=?
          AND COALESCE(finalizado, 0)=0
        ORDER BY id DESC
        """,
        (cid,)
    ).fetchall()

    novas = []

    for x in rows:
        d = dict(x)
        d["legacy"] = False
        novas.append(d)

    ja_migradas = {
        str(r["legacy_id"])
        for r in c.execute(
            "SELECT legacy_id FROM ordens_servico WHERE legacy_id IS NOT NULL"
        ).fetchall()
    }

    c.close()

    antigas = []

    if cliente.get("legacy_id") is not None:
        antigas = [
            x for x in pesquisar_os_mdb(cliente["legacy_id"])
            if str(x.get("situacao_codigo") or "") not in ("10", "11")
            and str(x.get("legacy_id") or "") not in ja_migradas
        ]

    return jsonify(novas + antigas)


@app.get("/api/clientes/legacy/<int:legacy_id>/historico")
@login_obrigatorio
def historico_cliente_legacy(legacy_id):
    c = conn()
    ja_migradas = {
        str(r["legacy_id"])
        for r in c.execute(
            "SELECT legacy_id FROM ordens_servico WHERE legacy_id IS NOT NULL"
        ).fetchall()
    }
    c.close()

    antigas = [
        x for x in pesquisar_os_mdb(legacy_id)
        if str(x.get("situacao_codigo") or "") not in ("10", "11")
        and str(x.get("legacy_id") or "") not in ja_migradas
    ]
    return jsonify(antigas)


# =========================================================
# CONSULTA DE ORDENS DE SERVIÇO
# =========================================================

@app.get("/api/os/consulta")
@login_obrigatorio
def consulta_os():

    modo = (request.args.get("modo") or "novas").strip().lower()
    q = (request.args.get("q") or "").strip()
    situacao_filtro = (request.args.get("situacao") or "").strip()
    campo_busca = (request.args.get("campo") or "nome").strip().lower()
    if campo_busca not in ("nome", "os", "equipamento", "telefone"):
        campo_busca = "nome"

    if modo not in ("novas", "todas"):
        modo = "novas"

    resultados = []

    # -----------------------------------------------------
    # OS NOVAS - SQLITE
    # -----------------------------------------------------

    c = conn()

    sql = """
        SELECT
            o.id,
            o.legacy_id,
            o.numero_os,
            o.finalizado,
            o.defeito,
            o.observacoes,
            o.situacao,
            o.data_entrada,
            c.nome AS cliente_nome,
            c.legacy_id AS cliente_legacy_id,
            e.tipo,
            e.marca,
            e.modelo,
            e.serie,
            e.patrimonio
        FROM ordens_servico o
        INNER JOIN clientes c
            ON c.id = o.cliente_id
        INNER JOIN equipamentos e
            ON e.id = o.equipamento_id
    """

    parametros = []

    filtros = []

    # "Novas" = somente OS abertas. "Todas" = abertas + finalizadas.
    if modo == "novas":
        filtros.append("COALESCE(o.finalizado, 0)=0")

    if situacao_filtro:
        if situacao_filtro == "Pronto":
            # Card "PRONTOS" do painel junta as duas situações de pronto.
            filtros.append("o.situacao LIKE 'Pronto,%'")
        else:
            filtros.append("o.situacao = ?")
            parametros.append(situacao_filtro)

    if q:
        campo_map = {
            "nome": ["c.nome"],
            "os": ["CAST(o.numero_os AS TEXT)"],
            "equipamento": ["e.tipo", "e.marca", "e.modelo", "e.serie", "e.patrimonio"],
            "telefone": ["c.celular", "c.telefone"],
        }
        colunas_busca = campo_map.get(campo_busca, campo_map["nome"])
        filtros.append(
            "(" + " OR ".join(f"{col} LIKE ?" for col in colunas_busca) + ")"
        )
        like = f"%{q}%"
        parametros.extend([like] * len(colunas_busca))

    if filtros:
        sql += " WHERE " + " AND ".join(filtros)

    sql += " ORDER BY o.numero_os DESC"

    rows = c.execute(sql, parametros).fetchall()

    for row in rows:
        d = dict(row)
        d["legacy"] = False
        d["numero_os"] = formatar_os(d.get("numero_os"))
        d["editavel"] = not bool(d.get("finalizado"))
        resultados.append(d)

    c.close()

    # -----------------------------------------------------
    # OS ANTIGAS - MDB
    # -----------------------------------------------------

    if modo == "todas":

        # Evita duplicar uma OS que já foi migrada/convertida pra
        # dentro do sistema novo (mesmo número/legacy_id).
        numeros_novos = {
            str(d.get("legacy_id"))
            for d in resultados
            if d.get("legacy_id") is not None
        } | {
            str(d.get("numero_os") or "").lstrip("0")
            for d in resultados
        }

        clientes_mdb = carregar_clientes_mdb()

        clientes_por_codigo = {
            limpar(x.get("CODIGO")):
            limpar(x.get("NOME"))
            for x in clientes_mdb
        }

        situacoes = {
            "1": "Aguardando avaliação do técnico",
            "3": "Aguardando autorização do orçamento",
            "4": "Equipamento Condenado",
            "6": "Autorizado, Reparo em andamento",
            "7": "Autorizado, Aguardando peça",
            "8": "Pronto, avisar cliente",
            "9": "Pronto, cliente avisado",
            "10": "Equipamento entregue reparado",
            "11": "Equipamento devolvido sem reparo"
        }

        q_casefold = q.casefold()

        for row in carregar_os_mdb():

            numero = limpar(row.get("CODIGO"))

            if numero in numeros_novos:
                continue

            cliente_id = limpar(row.get("COD_CLIENTE"))
            aparelho = limpar(row.get("APARELHO"))
            marca = limpar(row.get("MARCA"))
            modelo = limpar(row.get("MODELO"))
            serie = limpar(row.get("SERIE"))
            patrimonio = limpar(row.get("PATRIMONIO"))
            defeito = limpar(row.get("DEFEITO"))

            nome_cliente = clientes_por_codigo.get(
                cliente_id, ""
            )

            situacao_codigo = limpar(
                row.get("SITUACAO")
            )

            situacao_nome = situacoes.get(
                situacao_codigo,
                situacao_codigo
            )

            if q:

                mapa_texto = {
                    "nome": [nome_cliente],
                    "os": [numero],
                    "equipamento": [aparelho, marca, modelo, serie, patrimonio],
                    "telefone": [],
                }
                partes = mapa_texto.get(campo_busca, [nome_cliente])
                texto = " ".join(partes).casefold()

                if q_casefold not in texto:
                    continue

            resultados.append({
                "id": None,
                "legacy": True,
                "legacy_id": numero,
                "numero_os": numero,
                "cliente_nome": nome_cliente,
                "cliente_legacy_id": cliente_id,
                "tipo": aparelho,
                "marca": marca,
                "modelo": modelo,
                "serie": serie,
                "patrimonio": patrimonio,
                "situacao": situacao_nome,
                "situacao_codigo": situacao_codigo,
                "data_entrada": limpar(
                    row.get("ENTRADA")
                ),
                "defeito": defeito
            })

    def chave_os(item):
        try:
            return int(item.get("numero_os") or 0)
        except Exception:
            return 0

    resultados.sort(
        key=chave_os,
        reverse=True
    )

    total = len(resultados)
    por_pagina = 30
    try:
        pagina = max(int(request.args.get("pagina") or 1), 1)
    except ValueError:
        pagina = 1
    total_paginas = max((total + por_pagina - 1) // por_pagina, 1)
    pagina = min(pagina, total_paginas)
    inicio = (pagina - 1) * por_pagina

    return jsonify({
        "resultados": resultados[inicio:inicio + por_pagina],
        "total": total,
        "pagina": pagina,
        "total_paginas": total_paginas,
        "por_pagina": por_pagina
    })


# =========================================================
# DETALHES COMPLETOS DE UMA OS ANTIGA
# LÊ DIRETAMENTE DO MDB. NÃO IMPORTA NADA.
# =========================================================

@app.get("/api/os/consulta-antiga/<int:legacy_id>")
@login_obrigatorio
def consulta_os_antiga(legacy_id):

    codigo = str(legacy_id)

    ordem = None

    for row in carregar_os_mdb():
        if limpar(row.get("CODIGO")) == codigo:
            ordem = row
            break

    if ordem is None:
        return jsonify(
            erro="OS antiga não encontrada."
        ), 404

    clientes = carregar_clientes_mdb()

    cliente_nome = ""

    for cliente in clientes:
        if limpar(cliente.get("CODIGO")) == limpar(
            ordem.get("COD_CLIENTE")
        ):
            cliente_nome = limpar(
                cliente.get("NOME")
            )
            break

    situacoes = {
        "1": "Aguardando avaliação do técnico",
        "3": "Aguardando autorização do orçamento",
        "4": "Equipamento Condenado",
        "6": "Autorizado, Reparo em andamento",
        "7": "Autorizado, Aguardando peça",
        "8": "Pronto, avisar cliente",
        "9": "Pronto, cliente avisado",
        "10": "Equipamento entregue reparado",
        "11": "Equipamento devolvido sem reparo"
    }

    situacao_codigo = limpar(
        ordem.get("SITUACAO")
    )

    # -----------------------------------------------------
    # PEÇAS
    # -----------------------------------------------------

    pecas = []

    for row in carregar_os_pecas_mdb():

        if limpar(row.get("COD_OS")) != codigo:
            continue

        try:
            qtd = float(
                limpar(row.get("QTD")) or 0
            )
        except Exception:
            qtd = 0

        try:
            valor = float(
                limpar(row.get("VALOR")) or 0
            )
        except Exception:
            valor = 0

        try:
            custo = float(
                limpar(row.get("CUSTO")) or 0
            )
        except Exception:
            custo = 0

        pecas.append({
            "codigo": limpar(row.get("CODIGO")),
            "cod_peca": limpar(row.get("COD_PECA")),
            "descricao": limpar(row.get("DESCRICAO")),
            "qtd": qtd,
            "valor": valor,
            "custo": custo,
            "data": limpar(row.get("DIA")),
            "tecnico": limpar(row.get("TECNICO")),
            "lotes": limpar(row.get("LOTES")),
            "seriais": limpar(row.get("SERIAIS_IN"))
        })

    # -----------------------------------------------------
    # SERVIÇOS
    # -----------------------------------------------------

    servicos = []

    for row in carregar_os_servicos_mdb():

        if limpar(row.get("OS_NUM")) != codigo:
            continue

        try:
            qtd = float(
                limpar(row.get("QTD")) or 0
            )
        except Exception:
            qtd = 0

        try:
            total = float(
                limpar(row.get("TOTAL")) or 0
            )
        except Exception:
            total = 0

        try:
            custo = float(
                limpar(row.get("CUSTO")) or 0
            )
        except Exception:
            custo = 0

        servicos.append({
            "codigo": limpar(row.get("CODIGO")),
            "descricao": limpar(row.get("DESCRICAO")),
            "total": total,
            "inicio": limpar(row.get("INICIO")),
            "fim": limpar(row.get("FIM")),
            "tecnico": limpar(row.get("TECNICO")),
            "tipo": limpar(row.get("TIPO")),
            "cod_serv": limpar(row.get("COD_SERV")),
            "qtd": qtd,
            "custo": custo
        })

    # -----------------------------------------------------
    # DADOS PRINCIPAIS
    # -----------------------------------------------------

    resultado = os_mdb_para_json(ordem)

    resultado["cliente_nome"] = cliente_nome

    resultado["situacao_codigo"] = situacao_codigo

    resultado["situacao_nome"] = situacoes.get(
        situacao_codigo,
        situacao_codigo
    )

    resultado["entrada"] = limpar(
        ordem.get("ENTRADA")
    )

    resultado["pronto"] = limpar(
        ordem.get("PRONTO")
    )

    resultado["saida"] = limpar(
        ordem.get("SAIDA")
    )

    resultado["garantia"] = limpar(
        ordem.get("GARANTIA")
    )

    resultado["obs_servico"] = limpar(
        ordem.get("OBS_SERVICO")
    )

    resultado["valor_mao"] = limpar(
        ordem.get("V_MAO")
    )

    resultado["valor_pecas"] = limpar(
        ordem.get("V_PECAS")
    )

    resultado["valor_deslocamento"] = limpar(
        ordem.get("V_DESLOCA")
    )

    resultado["valor_terceiro"] = limpar(
        ordem.get("V_TERCEIRO")
    )

    resultado["valor_outros"] = limpar(
        ordem.get("V_OUTROS")
    )

    resultado["os_sinal"] = limpar(
        ordem.get("OS_SINAL")
    )

    resultado["frete"] = limpar(
        ordem.get("V_FRETE")
    )

    resultado["seguro"] = limpar(
        ordem.get("V_SEGURO")
    )

    resultado["laudo"] = limpar(
        ordem.get("LAUDO")
    )

    resultado["observacoes_aparelho"] = limpar(
        ordem.get("OBS_APARELHO")
    )

    resultado["acessorio"] = limpar(
        ordem.get("ACESSORIO")
    )

    resultado["pecas"] = pecas
    resultado["servicos"] = servicos

    return jsonify(resultado)


# =========================================================
# DETALHES DE UMA OS NOVA
# =========================================================

@app.get("/api/os/<int:os_id>")
@login_obrigatorio
def get_os(os_id):
    c = conn()

    row = c.execute(
        """
        SELECT
            o.*,
            c.nome AS cliente_nome,
            c.cpf_cnpj AS cliente_cpf_cnpj,
            c.celular AS cliente_celular,
            c.telefone AS cliente_telefone,
            c.email AS cliente_email,
            c.cep AS cliente_cep,
            c.endereco AS cliente_endereco,
            c.numero AS cliente_numero,
            c.complemento AS cliente_complemento,
            c.bairro AS cliente_bairro,
            c.cidade AS cliente_cidade,
            c.uf AS cliente_uf,
            c.observacoes AS cliente_observacoes,
            e.tipo AS equipamento_tipo,
            e.marca AS equipamento_marca,
            e.modelo AS equipamento_modelo,
            e.serie AS equipamento_serie,
            e.patrimonio AS equipamento_patrimonio,
            e.data_compra AS equipamento_data_compra,
            e.nf AS equipamento_nf,
            e.acessorios AS equipamento_acessorios,
            e.observacoes AS equipamento_observacoes
        FROM ordens_servico o
        INNER JOIN clientes c ON c.id=o.cliente_id
        INNER JOIN equipamentos e ON e.id=o.equipamento_id
        WHERE o.id=?
        """,
        (os_id,)
    ).fetchone()

    if not row:
        c.close()
        return jsonify(erro="OS não encontrada."), 404

    resultado = dict(row)
    resultado["legacy"] = False
    resultado["numero_os"] = formatar_os(resultado.get("numero_os"))
    resultado["editavel"] = not bool(resultado.get("finalizado"))
    resultado["cliente"] = {
        "id": resultado.get("cliente_id"),
        "nome": resultado.get("cliente_nome") or "",
        "cpf_cnpj": resultado.get("cliente_cpf_cnpj") or "",
        "celular": resultado.get("cliente_celular") or "",
        "telefone": resultado.get("cliente_telefone") or "",
        "email": resultado.get("cliente_email") or "",
        "cep": resultado.get("cliente_cep") or "",
        "endereco": resultado.get("cliente_endereco") or "",
        "numero": resultado.get("cliente_numero") or "",
        "complemento": resultado.get("cliente_complemento") or "",
        "bairro": resultado.get("cliente_bairro") or "",
        "cidade": resultado.get("cliente_cidade") or "",
        "uf": resultado.get("cliente_uf") or "",
        "observacoes": resultado.get("cliente_observacoes") or ""
    }
    resultado["equipamento"] = {
        "id": resultado.get("equipamento_id"),
        "tipo": resultado.get("equipamento_tipo") or "",
        "marca": resultado.get("equipamento_marca") or "",
        "modelo": resultado.get("equipamento_modelo") or "",
        "serie": resultado.get("equipamento_serie") or "",
        "patrimonio": resultado.get("equipamento_patrimonio") or "",
        "data_compra": resultado.get("equipamento_data_compra") or "",
        "nf": resultado.get("equipamento_nf") or "",
        "acessorios": resultado.get("equipamento_acessorios") or "",
        "observacoes": resultado.get("equipamento_observacoes") or ""
    }

    servicos = c.execute(
        "SELECT id, descricao, quantidade, valor, criado_em FROM os_servicos WHERE os_id=? ORDER BY id",
        (os_id,)
    ).fetchall()
    pecas = c.execute(
        "SELECT id, descricao, quantidade, valor, criado_em FROM os_pecas WHERE os_id=? ORDER BY id",
        (os_id,)
    ).fetchall()
    orc_servicos = c.execute(
        "SELECT id, descricao, quantidade, valor, criado_em FROM os_orcamento_servicos WHERE os_id=? ORDER BY id",
        (os_id,)
    ).fetchall()
    orc_pecas = c.execute(
        "SELECT id, descricao, quantidade, valor, criado_em FROM os_orcamento_pecas WHERE os_id=? ORDER BY id",
        (os_id,)
    ).fetchall()
    historico = c.execute(
        "SELECT id, evento, criado_em FROM os_historico WHERE os_id=? ORDER BY id DESC",
        (os_id,)
    ).fetchall()

    resultado["servicos"] = [dict(x) for x in servicos]
    resultado["pecas"] = [dict(x) for x in pecas]
    resultado["orcamento_servicos"] = [dict(x) for x in orc_servicos]
    resultado["orcamento_pecas"] = [dict(x) for x in orc_pecas]
    resultado["historico"] = [dict(x) for x in historico]
    resultado["total_orcamento_servicos"] = round(sum(float(x["quantidade"] or 0) * float(x["valor"] or 0) for x in orc_servicos), 2)
    resultado["total_orcamento_pecas"] = round(sum(float(x["quantidade"] or 0) * float(x["valor"] or 0) for x in orc_pecas), 2)
    resultado["total_orcamento"] = round(resultado["total_orcamento_servicos"] + resultado["total_orcamento_pecas"], 2)
    resultado["total_servicos"] = round(sum(float(x["quantidade"] or 0) * float(x["valor"] or 0) for x in servicos), 2)
    resultado["total_pecas"] = round(sum(float(x["quantidade"] or 0) * float(x["valor"] or 0) for x in pecas), 2)
    resultado["total_os"] = round(resultado["total_servicos"] + resultado["total_pecas"], 2)

    c.close()
    return jsonify(resultado)


@app.delete("/api/os/<int:os_id>")
@login_obrigatorio
def excluir_os(os_id):
    c = conn()
    try:
        # Confirma que a OS existe e obtém o estado atual.
        row = c.execute(
            "SELECT id, numero_os, finalizado FROM ordens_servico WHERE id=?",
            (os_id,)
        ).fetchone()

        if not row:
            return jsonify(erro="OS não encontrada."), 404

        if int(row["finalizado"] or 0) == 1:
            return jsonify(
                erro="Esta OS já foi finalizada e não pode ser excluída."
            ), 400

        numero = f"{int(row['numero_os']):06d}"

        # O histórico NÃO usa ON DELETE CASCADE, portanto precisa sair primeiro.
        c.execute("DELETE FROM os_historico WHERE os_id=?", (os_id,))

        # Dependências da OS.
        c.execute("DELETE FROM os_servicos WHERE os_id=?", (os_id,))
        c.execute("DELETE FROM os_pecas WHERE os_id=?", (os_id,))
        c.execute("DELETE FROM os_orcamento_servicos WHERE os_id=?", (os_id,))
        c.execute("DELETE FROM os_orcamento_pecas WHERE os_id=?", (os_id,))
        c.execute("DELETE FROM financeiro_os WHERE os_id=?", (os_id,))

        # Finalmente a OS.
        cur = c.execute("DELETE FROM ordens_servico WHERE id=?", (os_id,))

        if cur.rowcount != 1:
            c.rollback()
            return jsonify(erro="A OS não foi excluída."), 400

        c.commit()

        return jsonify(
            ok=True,
            numero_os=numero,
            mensagem=f"OS {numero} excluída com sucesso."
        )

    except Exception as e:
        c.rollback()
        return jsonify(erro=f"Erro ao excluir a OS: {e}"), 500
    finally:
        c.close()


@app.put("/api/os/<int:os_id>")
@login_obrigatorio
def atualizar_os(os_id):
    d = request.get_json() or {}
    permitidos = {"situacao", "defeito", "problema_identificado", "observacoes"}
    campos = {k: d.get(k) for k in permitidos if k in d}

    if not campos:
        return jsonify(erro="Nenhuma informação para atualizar."), 400

    c = conn()
    atual = c.execute("SELECT * FROM ordens_servico WHERE id=?", (os_id,)).fetchone()
    if not atual:
        c.close()
        return jsonify(erro="OS não encontrada."), 404

    if int(atual["finalizado"] or 0):
        c.close()
        return jsonify(erro="Esta OS já foi finalizada e não pode mais ser alterada."), 400

    alteracoes = []
    sets = []
    valores = []
    for campo, valor in campos.items():
        if campo == "situacao":
            valor = str(valor or "").strip()
            if not valor:
                c.close()
                return jsonify(erro="Informe a situação da OS."), 400
            if valor != atual["situacao"]:
                alteracoes.append(f"Situação: {atual['situacao']} → {valor}")
        elif campo == "defeito" and (valor or "") != (atual["defeito"] or ""):
            alteracoes.append("Defeito/solicitação atualizado")
        elif campo == "problema_identificado" and (valor or "") != (atual["problema_identificado"] or ""):
            alteracoes.append("Problema identificado atualizado")
        elif campo == "observacoes" and (valor or "") != (atual["observacoes"] or ""):
            alteracoes.append("Observações/laudo atualizado")
        sets.append(f"{campo}=?")
        valores.append(valor)

    valores.append(os_id)
    c.execute(f"UPDATE ordens_servico SET {', '.join(sets)} WHERE id=?", valores)

    # Ao autorizar o reparo, aproveita o orçamento como base da execução.
    nova_situacao = str(campos.get("situacao") or atual["situacao"] or "").strip()
    if nova_situacao == "Autorizado, Reparo em andamento" and atual["situacao"] != nova_situacao:
        qtd_exec_serv = c.execute("SELECT COUNT(*) AS n FROM os_servicos WHERE os_id=?", (os_id,)).fetchone()["n"]
        qtd_exec_pecas = c.execute("SELECT COUNT(*) AS n FROM os_pecas WHERE os_id=?", (os_id,)).fetchone()["n"]
        if int(qtd_exec_serv or 0) == 0 and int(qtd_exec_pecas or 0) == 0:
            c.execute("""INSERT INTO os_servicos(os_id, descricao, quantidade, valor) SELECT os_id, descricao, quantidade, valor FROM os_orcamento_servicos WHERE os_id=?""", (os_id,))
            c.execute("""INSERT INTO os_pecas(os_id, descricao, quantidade, valor) SELECT os_id, descricao, quantidade, valor FROM os_orcamento_pecas WHERE os_id=?""", (os_id,))
            if c.execute("SELECT changes()").fetchone()[0] or True:
                alteracoes.append("Orçamento aproveitado como base da execução")

    for evento in alteracoes:
        c.execute("INSERT INTO os_historico(os_id, evento, criado_em) VALUES (?,?,?)", (os_id, evento, agora_br()))
    c.commit()
    c.close()
    return jsonify(ok=True)


@app.post("/api/os/<int:os_id>/servicos")
@login_obrigatorio
def adicionar_servico(os_id):
    d = request.get_json() or {}
    descricao = str(d.get("descricao") or "").strip()
    try:
        quantidade = float(d.get("quantidade") or 1)
        valor = float(d.get("valor") or 0)
    except (TypeError, ValueError):
        return jsonify(erro="Quantidade ou valor inválido."), 400

    if not descricao:
        return jsonify(erro="Informe o serviço realizado."), 400
    if quantidade <= 0 or valor < 0:
        return jsonify(erro="Quantidade deve ser maior que zero e valor não pode ser negativo."), 400

    c = conn()
    os = c.execute("SELECT finalizado FROM ordens_servico WHERE id=?", (os_id,)).fetchone()
    if not os:
        c.close(); return jsonify(erro="OS não encontrada."), 404
    if int(os["finalizado"] or 0):
        c.close(); return jsonify(erro="Esta OS já foi finalizada."), 400

    cur = c.execute(
        "INSERT INTO os_servicos(os_id, descricao, quantidade, valor) VALUES (?,?,?,?)",
        (os_id, descricao, quantidade, valor)
    )
    c.execute("INSERT INTO os_historico(os_id, evento, criado_em) VALUES (?,?,?)", (os_id, f"Serviço adicionado: {descricao}", agora_br()))
    c.commit()
    novo_id = cur.lastrowid
    c.close()
    return jsonify(ok=True, id=novo_id)


@app.delete("/api/os/<int:os_id>/servicos/<int:item_id>")
@login_obrigatorio
def excluir_servico(os_id, item_id):
    c = conn()
    os = c.execute("SELECT finalizado FROM ordens_servico WHERE id=?", (os_id,)).fetchone()
    if not os:
        c.close(); return jsonify(erro="OS não encontrada."), 404
    if int(os["finalizado"] or 0):
        c.close(); return jsonify(erro="Esta OS já foi finalizada."), 400
    item = c.execute("SELECT descricao FROM os_servicos WHERE id=? AND os_id=?", (item_id, os_id)).fetchone()
    if not item:
        c.close(); return jsonify(erro="Serviço não encontrado."), 404
    c.execute("DELETE FROM os_servicos WHERE id=? AND os_id=?", (item_id, os_id))
    c.execute("INSERT INTO os_historico(os_id, evento, criado_em) VALUES (?,?,?)", (os_id, f"Serviço removido: {item['descricao']}", agora_br()))
    c.commit(); c.close()
    return jsonify(ok=True)


@app.post("/api/os/<int:os_id>/pecas")
@login_obrigatorio
def adicionar_peca(os_id):
    d = request.get_json() or {}
    descricao = str(d.get("descricao") or "").strip()
    try:
        quantidade = float(d.get("quantidade") or 1)
        valor = float(d.get("valor") or 0)
    except (TypeError, ValueError):
        return jsonify(erro="Quantidade ou valor inválido."), 400

    if not descricao:
        return jsonify(erro="Informe a peça."), 400
    if quantidade <= 0 or valor < 0:
        return jsonify(erro="Quantidade deve ser maior que zero e valor não pode ser negativo."), 400

    c = conn()
    os = c.execute("SELECT finalizado FROM ordens_servico WHERE id=?", (os_id,)).fetchone()
    if not os:
        c.close(); return jsonify(erro="OS não encontrada."), 404
    if int(os["finalizado"] or 0):
        c.close(); return jsonify(erro="Esta OS já foi finalizada."), 400

    cur = c.execute(
        "INSERT INTO os_pecas(os_id, descricao, quantidade, valor) VALUES (?,?,?,?)",
        (os_id, descricao, quantidade, valor)
    )
    c.execute("INSERT INTO os_historico(os_id, evento, criado_em) VALUES (?,?,?)", (os_id, f"Peça adicionada: {descricao}", agora_br()))
    c.commit()
    novo_id = cur.lastrowid
    c.close()
    return jsonify(ok=True, id=novo_id)


@app.put("/api/os/<int:os_id>/servicos/<int:item_id>")
@login_obrigatorio
def editar_servico(os_id, item_id):
    d = request.get_json() or {}
    descricao = str(d.get("descricao") or "").strip()
    try:
        quantidade = float(d.get("quantidade") or 1)
        valor = float(d.get("valor") or 0)
    except (TypeError, ValueError):
        return jsonify(erro="Quantidade ou valor inválido."), 400
    if not descricao:
        return jsonify(erro="Informe o serviço realizado."), 400
    if quantidade <= 0 or valor < 0:
        return jsonify(erro="Quantidade deve ser maior que zero e valor não pode ser negativo."), 400
    c = conn()
    os = c.execute("SELECT finalizado FROM ordens_servico WHERE id=?", (os_id,)).fetchone()
    if not os:
        c.close(); return jsonify(erro="OS não encontrada."), 404
    if int(os["finalizado"] or 0):
        c.close(); return jsonify(erro="Esta OS já foi finalizada."), 400
    item = c.execute("SELECT descricao FROM os_servicos WHERE id=? AND os_id=?", (item_id, os_id)).fetchone()
    if not item:
        c.close(); return jsonify(erro="Serviço não encontrado."), 404
    c.execute("UPDATE os_servicos SET descricao=?, quantidade=?, valor=? WHERE id=? AND os_id=?", (descricao, quantidade, valor, item_id, os_id))
    c.execute("INSERT INTO os_historico(os_id, evento, criado_em) VALUES (?,?,?)", (os_id, f"Serviço editado: {descricao}", agora_br()))
    c.commit(); c.close()
    return jsonify(ok=True)


@app.put("/api/os/<int:os_id>/pecas/<int:item_id>")
@login_obrigatorio
def editar_peca(os_id, item_id):
    d = request.get_json() or {}
    descricao = str(d.get("descricao") or "").strip()
    try:
        quantidade = float(d.get("quantidade") or 1)
        valor = float(d.get("valor") or 0)
    except (TypeError, ValueError):
        return jsonify(erro="Quantidade ou valor inválido."), 400
    if not descricao:
        return jsonify(erro="Informe a peça."), 400
    if quantidade <= 0 or valor < 0:
        return jsonify(erro="Quantidade deve ser maior que zero e valor não pode ser negativo."), 400
    c = conn()
    os = c.execute("SELECT finalizado FROM ordens_servico WHERE id=?", (os_id,)).fetchone()
    if not os:
        c.close(); return jsonify(erro="OS não encontrada."), 404
    if int(os["finalizado"] or 0):
        c.close(); return jsonify(erro="Esta OS já foi finalizada."), 400
    item = c.execute("SELECT descricao FROM os_pecas WHERE id=? AND os_id=?", (item_id, os_id)).fetchone()
    if not item:
        c.close(); return jsonify(erro="Peça não encontrada."), 404
    c.execute("UPDATE os_pecas SET descricao=?, quantidade=?, valor=? WHERE id=? AND os_id=?", (descricao, quantidade, valor, item_id, os_id))
    c.execute("INSERT INTO os_historico(os_id, evento, criado_em) VALUES (?,?,?)", (os_id, f"Peça editada: {descricao}", agora_br()))
    c.commit(); c.close()
    return jsonify(ok=True)


@app.delete("/api/os/<int:os_id>/pecas/<int:item_id>")
@login_obrigatorio
def excluir_peca(os_id, item_id):
    c = conn()
    os = c.execute("SELECT finalizado FROM ordens_servico WHERE id=?", (os_id,)).fetchone()
    if not os:
        c.close(); return jsonify(erro="OS não encontrada."), 404
    if int(os["finalizado"] or 0):
        c.close(); return jsonify(erro="Esta OS já foi finalizada."), 400
    item = c.execute("SELECT descricao FROM os_pecas WHERE id=? AND os_id=?", (item_id, os_id)).fetchone()
    if not item:
        c.close(); return jsonify(erro="Peça não encontrada."), 404
    c.execute("DELETE FROM os_pecas WHERE id=? AND os_id=?", (item_id, os_id))
    c.execute("INSERT INTO os_historico(os_id, evento, criado_em) VALUES (?,?,?)", (os_id, f"Peça removida: {item['descricao']}", agora_br()))
    c.commit(); c.close()
    return jsonify(ok=True)


# =========================================================
# ORÇAMENTO DA OS
# =========================================================

@app.post("/api/os/<int:os_id>/orcamento/servicos")
@login_obrigatorio
def adicionar_orcamento_servico(os_id):
    d = request.get_json() or {}
    descricao = str(d.get("descricao") or "").strip()
    try:
        quantidade = float(d.get("quantidade") or 1)
        valor = float(d.get("valor") or 0)
    except (TypeError, ValueError):
        return jsonify(erro="Quantidade ou valor inválido."), 400
    if not descricao:
        return jsonify(erro="Informe o serviço do orçamento."), 400
    if quantidade <= 0 or valor < 0:
        return jsonify(erro="Quantidade deve ser maior que zero e valor não pode ser negativo."), 400
    c = conn()
    row = c.execute("SELECT finalizado FROM ordens_servico WHERE id=?", (os_id,)).fetchone()
    if not row:
        c.close(); return jsonify(erro="OS não encontrada."), 404
    if int(row["finalizado"] or 0):
        c.close(); return jsonify(erro="Esta OS já foi finalizada."), 400
    cur = c.execute("INSERT INTO os_orcamento_servicos(os_id, descricao, quantidade, valor) VALUES (?,?,?,?)", (os_id, descricao, quantidade, valor))
    c.execute("INSERT INTO os_historico(os_id, evento, criado_em) VALUES (?,?,?)", (os_id, f"Serviço orçado: {descricao}", agora_br()))
    c.commit(); c.close()
    return jsonify(ok=True, id=cur.lastrowid)


@app.delete("/api/os/<int:os_id>/orcamento/servicos/<int:item_id>")
@login_obrigatorio
def excluir_orcamento_servico(os_id, item_id):
    c = conn(); row = c.execute("SELECT finalizado FROM ordens_servico WHERE id=?", (os_id,)).fetchone()
    if not row: c.close(); return jsonify(erro="OS não encontrada."), 404
    if int(row["finalizado"] or 0): c.close(); return jsonify(erro="Esta OS já foi finalizada."), 400
    item = c.execute("SELECT descricao FROM os_orcamento_servicos WHERE id=? AND os_id=?", (item_id, os_id)).fetchone()
    if not item: c.close(); return jsonify(erro="Item do orçamento não encontrado."), 404
    c.execute("DELETE FROM os_orcamento_servicos WHERE id=? AND os_id=?", (item_id, os_id))
    c.execute("INSERT INTO os_historico(os_id, evento, criado_em) VALUES (?,?,?)", (os_id, f"Serviço do orçamento removido: {item['descricao']}", agora_br()))
    c.commit(); c.close(); return jsonify(ok=True)


@app.post("/api/os/<int:os_id>/orcamento/pecas")
@login_obrigatorio
def adicionar_orcamento_peca(os_id):
    d = request.get_json() or {}
    descricao = str(d.get("descricao") or "").strip()
    try:
        quantidade = float(d.get("quantidade") or 1)
        valor = float(d.get("valor") or 0)
    except (TypeError, ValueError):
        return jsonify(erro="Quantidade ou valor inválido."), 400
    if not descricao: return jsonify(erro="Informe a peça do orçamento."), 400
    if quantidade <= 0 or valor < 0: return jsonify(erro="Quantidade deve ser maior que zero e valor não pode ser negativo."), 400
    c = conn(); row = c.execute("SELECT finalizado FROM ordens_servico WHERE id=?", (os_id,)).fetchone()
    if not row: c.close(); return jsonify(erro="OS não encontrada."), 404
    if int(row["finalizado"] or 0): c.close(); return jsonify(erro="Esta OS já foi finalizada."), 400
    cur = c.execute("INSERT INTO os_orcamento_pecas(os_id, descricao, quantidade, valor) VALUES (?,?,?,?)", (os_id, descricao, quantidade, valor))
    c.execute("INSERT INTO os_historico(os_id, evento, criado_em) VALUES (?,?,?)", (os_id, f"Peça orçada: {descricao}", agora_br()))
    c.commit(); c.close(); return jsonify(ok=True, id=cur.lastrowid)


@app.delete("/api/os/<int:os_id>/orcamento/pecas/<int:item_id>")
@login_obrigatorio
def excluir_orcamento_peca(os_id, item_id):
    c = conn(); row = c.execute("SELECT finalizado FROM ordens_servico WHERE id=?", (os_id,)).fetchone()
    if not row: c.close(); return jsonify(erro="OS não encontrada."), 404
    if int(row["finalizado"] or 0): c.close(); return jsonify(erro="Esta OS já foi finalizada."), 400
    item = c.execute("SELECT descricao FROM os_orcamento_pecas WHERE id=? AND os_id=?", (item_id, os_id)).fetchone()
    if not item: c.close(); return jsonify(erro="Item do orçamento não encontrado."), 404
    c.execute("DELETE FROM os_orcamento_pecas WHERE id=? AND os_id=?", (item_id, os_id))
    c.execute("INSERT INTO os_historico(os_id, evento, criado_em) VALUES (?,?,?)", (os_id, f"Peça do orçamento removida: {item['descricao']}", agora_br()))
    c.commit(); c.close(); return jsonify(ok=True)


@app.get("/orcamento/<int:os_id>")
@login_obrigatorio
def tela_orcamento(os_id):
    return render_template("orcamento.html", os_id=os_id)


@app.post("/api/os/<int:os_id>/orcamento/whatsapp")
@login_obrigatorio
def registrar_orcamento_whatsapp(os_id):
    c = conn(); row = c.execute("SELECT finalizado FROM ordens_servico WHERE id=?", (os_id,)).fetchone()
    if not row: c.close(); return jsonify(erro="OS não encontrada."), 404
    if int(row["finalizado"] or 0): c.close(); return jsonify(erro="Esta OS já foi finalizada."), 400
    c.execute("INSERT INTO os_historico(os_id, evento, criado_em) VALUES (?, ?, ?)", (os_id, 'Orçamento preparado para WhatsApp', agora_br()))
    c.commit(); c.close(); return jsonify(ok=True)


SITUACOES_FINALIZACAO = {
    "Equipamento entregue reparado",
    "Equipamento Condenado",
    "Serviço não autorizado"
}


@app.post("/api/os/<int:os_id>/whatsapp")
@login_obrigatorio
def registrar_whatsapp(os_id):
    d = request.get_json() or {}
    situacao = str(d.get("situacao") or "").strip()

    if not situacao:
        return jsonify(erro="Situação não informada."), 400

    c = conn()
    row = c.execute(
        "SELECT finalizado FROM ordens_servico WHERE id=?",
        (os_id,)
    ).fetchone()

    if not row:
        c.close()
        return jsonify(erro="OS não encontrada."), 404

    if int(row["finalizado"] or 0):
        c.close()
        return jsonify(erro="Esta OS já foi finalizada."), 400

    c.execute(
        "INSERT INTO os_historico(os_id, evento, criado_em) VALUES (?,?,?)",
        (os_id, f"Aviso de WhatsApp preparado: {situacao}", agora_br())
    )
    c.commit()
    c.close()

    return jsonify(ok=True)


@app.post("/api/os/<int:os_id>/finalizar")
@login_obrigatorio
def finalizar_os(os_id):
    c = conn()
    row = c.execute("SELECT * FROM ordens_servico WHERE id=?", (os_id,)).fetchone()
    if not row:
        c.close(); return jsonify(erro="OS não encontrada."), 404
    if int(row["finalizado"] or 0):
        c.close(); return jsonify(erro="Esta OS já está finalizada."), 400

    situacao = str(row["situacao"] or "").strip()
    if situacao not in SITUACOES_FINALIZACAO:
        c.close()
        return jsonify(erro="Para finalizar a OS, altere a situação para Equipamento entregue reparado, Equipamento Condenado ou Serviço não autorizado."), 400

    c.execute(
        "UPDATE ordens_servico SET finalizado=1, data_finalizacao=(datetime('now','-3 hours')) WHERE id=?",
        (os_id,)
    )
    c.execute(
        "INSERT INTO os_historico(os_id, evento, criado_em) VALUES (?,?,?)",
        (os_id, f"OS finalizada: {situacao}", agora_br())
    )
    c.commit()
    c.close()
    return jsonify(ok=True, situacao=situacao)




# =========================================================
# FINANCEIRO
# =========================================================

@app.get("/financeiro")
@apenas_admin
def financeiro_page():
    return render_template("financeiro.html", active="financeiro")


@app.get("/api/financeiro/resumo")
@apenas_admin
def financeiro_resumo():
    c = conn()
    periodo = (request.args.get("periodo") or "mes").strip().lower()
    inicio = request.args.get("inicio")
    fim = request.args.get("fim")

    if periodo == "hoje":
        where = "date(COALESCE(o.data_finalizacao,o.data_entrada))=date('now','-3 hours')"
    elif periodo == "semana":
        hoje = date.today()
        seg = hoje - timedelta(days=hoje.weekday())
        dom = seg + timedelta(days=6)
        inicio, fim = seg.isoformat(), dom.isoformat()
        where = "date(COALESCE(o.data_finalizacao,o.data_entrada)) BETWEEN date(?) AND date(?)"
    elif periodo == "7dias":
        where = "date(COALESCE(o.data_finalizacao,o.data_entrada)) >= date('now','-3 hours','-6 day')"
    elif periodo == "ano":
        where = "strftime('%Y',COALESCE(o.data_finalizacao,o.data_entrada,'now'))=strftime('%Y','now')"
    elif periodo == "mes_especifico" and inicio:
        where = "strftime('%Y-%m',COALESCE(o.data_finalizacao,o.data_entrada,'now'))=?"
    elif periodo == "personalizado" and inicio and fim:
        where = "date(COALESCE(o.data_finalizacao,o.data_entrada)) BETWEEN date(?) AND date(?)"
    else:
        where = "strftime('%Y-%m',COALESCE(o.data_finalizacao,o.data_entrada,'now'))=strftime('%Y-%m','now')"

    if periodo == "semana":
        params = [inicio, fim]
    elif periodo == "mes_especifico" and inicio:
        params = [inicio]
    elif periodo == "personalizado" and inicio and fim:
        params = [inicio, fim]
    else:
        params = []

    base = f"""
        SELECT o.id,o.numero_os,o.situacao,o.finalizado,o.data_entrada,o.data_finalizacao,
               c.nome AS cliente,
               COALESCE((SELECT SUM(s.quantidade*s.valor) FROM os_servicos s WHERE s.os_id=o.id),0) AS servicos,
               COALESCE((SELECT SUM(p.quantidade*p.valor) FROM os_pecas p WHERE p.os_id=o.id),0) AS pecas,
               COALESCE((SELECT SUM(s.quantidade*s.valor) FROM os_servicos s WHERE s.os_id=o.id),0)+
               COALESCE((SELECT SUM(p.quantidade*p.valor) FROM os_pecas p WHERE p.os_id=o.id),0) AS total,
               COALESCE(f.pago,0) AS pago, f.data_pagamento, f.forma_pagamento
        FROM ordens_servico o
        JOIN clientes c ON c.id=o.cliente_id
        LEFT JOIN financeiro_os f ON f.os_id=o.id
        WHERE {where}
        ORDER BY COALESCE(o.data_finalizacao,o.data_entrada) DESC, o.id DESC
    """
    rows=c.execute(base,params).fetchall()

    faturamento=sum(float(r['total'] or 0) for r in rows if int(r['finalizado'] or 0))
    recebido=sum(float(r['total'] or 0) for r in rows if int(r['finalizado'] or 0) and int(r['pago'] or 0))
    aberto=sum(float(r['total'] or 0) for r in rows if int(r['finalizado'] or 0) and not int(r['pago'] or 0))
    custos=c.execute(f"""
        SELECT COALESCE(SUM((s.quantidade*s.custo)),0) AS v
        FROM os_servicos s JOIN ordens_servico o ON o.id=s.os_id WHERE {where}
    """,params).fetchone()['v']
    custos+=c.execute(f"""
        SELECT COALESCE(SUM((p.quantidade*p.custo)),0) AS v
        FROM os_pecas p JOIN ordens_servico o ON o.id=p.os_id WHERE {where}
    """,params).fetchone()['v']

    serv=sum(float(r['servicos'] or 0) for r in rows if int(r['finalizado'] or 0))
    pec=sum(float(r['pecas'] or 0) for r in rows if int(r['finalizado'] or 0))
    resultado=faturamento-float(custos or 0)

    out=[]
    for r in rows:
        d=dict(r)
        d['total']=round(float(d['total'] or 0),2); d['servicos']=round(float(d['servicos'] or 0),2); d['pecas']=round(float(d['pecas'] or 0),2)
        out.append(d)
    c.close()
    return jsonify({"periodo":periodo,"inicio":inicio,"fim":fim,"faturamento":round(faturamento,2),"recebido":round(recebido,2),"aberto":round(aberto,2),"custos":round(float(custos or 0),2),"resultado":round(resultado,2),"servicos":round(serv,2),"pecas":round(pec,2),"os":len(out),"movimentacoes":out})


@app.get("/api/financeiro/os/<int:os_id>")
@apenas_admin
def financeiro_os(os_id):
    c=conn()
    row=c.execute("""SELECT o.id,o.numero_os,o.situacao,o.finalizado,o.data_finalizacao,c.nome AS cliente
                    FROM ordens_servico o JOIN clientes c ON c.id=o.cliente_id WHERE o.id=?""",(os_id,)).fetchone()
    if not row: c.close(); return jsonify(erro="OS não encontrada."),404
    serv=c.execute("SELECT id,descricao,quantidade,valor,custo FROM os_servicos WHERE os_id=? ORDER BY id",(os_id,)).fetchall()
    pec=c.execute("SELECT id,descricao,quantidade,valor,custo FROM os_pecas WHERE os_id=? ORDER BY id",(os_id,)).fetchall()
    pay=c.execute("SELECT pago,data_pagamento,forma_pagamento,observacao FROM financeiro_os WHERE os_id=?",(os_id,)).fetchone()
    c.close()
    return jsonify({"os":dict(row),"servicos":[dict(x) for x in serv],"pecas":[dict(x) for x in pec],"pagamento":dict(pay) if pay else {"pago":0,"data_pagamento":"","forma_pagamento":"","observacao":""}})


@app.put("/api/financeiro/os/<int:os_id>/item")
@apenas_admin
def financeiro_item(os_id):
    d=request.get_json(silent=True) or {}
    tipo=str(d.get("tipo") or "").strip().lower(); item_id=int(d.get("item_id") or 0)
    try: custo=float(d.get("custo") or 0)
    except (TypeError,ValueError): return jsonify(erro="Custo inválido."),400
    if custo<0: return jsonify(erro="Custo não pode ser negativo."),400
    tabela="os_servicos" if tipo=="servico" else "os_pecas" if tipo=="peca" else None
    if not tabela: return jsonify(erro="Tipo de item inválido."),400
    c=conn(); row=c.execute(f"SELECT id FROM {tabela} WHERE id=? AND os_id=?",(item_id,os_id)).fetchone()
    if not row: c.close(); return jsonify(erro="Item não encontrado nessa OS."),404
    c.execute(f"UPDATE {tabela} SET custo=? WHERE id=? AND os_id=?",(custo,item_id,os_id)); c.commit(); c.close()
    return jsonify(ok=True)


@app.put("/api/financeiro/os/<int:os_id>/pagamento")
@apenas_admin
def financeiro_pagamento(os_id):
    d=request.get_json(silent=True) or {}
    pago=1 if d.get("pago") else 0
    data=(str(d.get("data_pagamento") or "").strip() or None)
    forma=(str(d.get("forma_pagamento") or "").strip() or None)
    obs=(str(d.get("observacao") or "").strip() or None)
    if not pago: data=None
    c=conn(); exists=c.execute("SELECT id FROM ordens_servico WHERE id=?",(os_id,)).fetchone()
    if not exists: c.close(); return jsonify(erro="OS não encontrada."),404
    c.execute("""INSERT INTO financeiro_os(os_id,pago,data_pagamento,forma_pagamento,observacao) VALUES(?,?,?,?,?)
                 ON CONFLICT(os_id) DO UPDATE SET pago=excluded.pago,data_pagamento=excluded.data_pagamento,forma_pagamento=excluded.forma_pagamento,observacao=excluded.observacao""",
              (os_id,pago,data,forma,obs))
    c.commit(); c.close(); return jsonify(ok=True)


# =========================================================
# HISTÓRICO FINANCEIRO — ÚLTIMOS 12 MESES
# =========================================================

@app.get("/api/financeiro/historico")
@apenas_admin
def financeiro_historico():
    c = conn()

    rows = c.execute("""
        SELECT
            strftime('%Y-%m', COALESCE(o.data_finalizacao, o.data_entrada, 'now')) AS mes,
            COALESCE(SUM(
                CASE WHEN COALESCE(o.finalizado,0)=1 THEN
                    COALESCE((SELECT SUM(s.quantidade*s.valor) FROM os_servicos s WHERE s.os_id=o.id),0) +
                    COALESCE((SELECT SUM(p.quantidade*p.valor) FROM os_pecas p WHERE p.os_id=o.id),0)
                ELSE 0 END
            ),0) AS faturamento,
            COALESCE(SUM(
                CASE WHEN COALESCE(o.finalizado,0)=1 AND COALESCE(f.pago,0)=1 THEN
                    COALESCE((SELECT SUM(s.quantidade*s.valor) FROM os_servicos s WHERE s.os_id=o.id),0) +
                    COALESCE((SELECT SUM(p.quantidade*p.valor) FROM os_pecas p WHERE p.os_id=o.id),0)
                ELSE 0 END
            ),0) AS recebido,
            COALESCE(SUM(
                CASE WHEN COALESCE(o.finalizado,0)=1 THEN
                    COALESCE((SELECT SUM(s.quantidade*s.custo) FROM os_servicos s WHERE s.os_id=o.id),0) +
                    COALESCE((SELECT SUM(p.quantidade*p.custo) FROM os_pecas p WHERE p.os_id=o.id),0)
                ELSE 0 END
            ),0) AS custos,
            COUNT(CASE WHEN COALESCE(o.finalizado,0)=1 THEN 1 END) AS os
        FROM ordens_servico o
        LEFT JOIN financeiro_os f ON f.os_id=o.id
        WHERE date(COALESCE(o.data_finalizacao,o.data_entrada,'now')) >= date('now','start of month','-11 months')
        GROUP BY strftime('%Y-%m', COALESCE(o.data_finalizacao,o.data_entrada,'now'))
        ORDER BY mes
    """).fetchall()

    mapa = {r["mes"]: dict(r) for r in rows}

    # Garante os 12 meses, inclusive meses sem movimento.
    meses = []
    for i in range(11, -1, -1):
        r = c.execute("""
            SELECT strftime('%Y-%m', date('now','start of month', ? || ' months')) AS mes
        """, (f"-{i}",)).fetchone()
        mes = r["mes"]
        bruto = mapa.get(mes, {})
        faturamento = float(bruto.get("faturamento") or 0)
        recebido = float(bruto.get("recebido") or 0)
        custos = float(bruto.get("custos") or 0)

        meses.append({
            "mes": mes,
            "faturamento": round(faturamento, 2),
            "recebido": round(recebido, 2),
            "custos": round(custos, 2),
            "resultado": round(faturamento - custos, 2),
            "os": int(bruto.get("os") or 0)
        })

    c.close()
    return jsonify({"meses": meses})


# =========================================================
# PAINEL / DASHBOARD
# =========================================================

@app.get("/api/dashboard")
@login_obrigatorio
def dashboard():
    c = conn()

    # Quantidade por situação nas OS novas.
    rows = c.execute("""
        SELECT
            COALESCE(NULLIF(TRIM(situacao), ''), 'Aguardando avaliação do técnico') AS situacao,
            COUNT(*) AS qtd
        FROM ordens_servico
        WHERE COALESCE(finalizado, 0)=0
        GROUP BY COALESCE(NULLIF(TRIM(situacao), ''), 'Aguardando avaliação do técnico')
    """).fetchall()

    por_situacao = {str(r["situacao"]): int(r["qtd"] or 0) for r in rows}

    # Totais financeiros das OS em execução/abertas.
    financeiro = c.execute("""
        SELECT
            COALESCE(SUM(
                CASE WHEN COALESCE(o.finalizado,0)=0 THEN
                    (SELECT COALESCE(SUM(s.quantidade * s.valor),0)
                       FROM os_servicos s WHERE s.os_id=o.id)
                    +
                    (SELECT COALESCE(SUM(p.quantidade * p.valor),0)
                       FROM os_pecas p WHERE p.os_id=o.id)
                ELSE 0 END
            ),0) AS total_aberto,
            COALESCE(SUM(
                CASE
                    WHEN date(COALESCE(o.data_finalizacao, o.data_entrada))
                         = date('now','-3 hours')
                    THEN
                        (SELECT COALESCE(SUM(s.quantidade * s.valor),0)
                           FROM os_servicos s WHERE s.os_id=o.id)
                        +
                        (SELECT COALESCE(SUM(p.quantidade * p.valor),0)
                           FROM os_pecas p WHERE p.os_id=o.id)
                    ELSE 0
                END
            ),0) AS total_hoje
        FROM ordens_servico o
    """).fetchone()

    # Últimas OS abertas, para acesso rápido pelo painel.
    ultimas = c.execute("""
        SELECT
            o.id,
            o.numero_os,
            o.situacao,
            o.data_entrada,
            COALESCE(o.finalizado,0) AS finalizado,
            c.nome AS cliente_nome,
            e.tipo AS equipamento_tipo,
            e.marca AS equipamento_marca,
            e.modelo AS equipamento_modelo,
            (
                SELECT COALESCE(SUM(s.quantidade * s.valor),0)
                FROM os_servicos s WHERE s.os_id=o.id
            ) +
            (
                SELECT COALESCE(SUM(p.quantidade * p.valor),0)
                FROM os_pecas p WHERE p.os_id=o.id
            ) AS total_os
        FROM ordens_servico o
        INNER JOIN clientes c ON c.id=o.cliente_id
        INNER JOIN equipamentos e ON e.id=o.equipamento_id
        WHERE COALESCE(o.finalizado,0)=0
        ORDER BY o.id DESC
        LIMIT 12
    """).fetchall()

    c.close()

    prontos = (
        por_situacao.get("Pronto, avisar cliente", 0)
        + por_situacao.get("Pronto, cliente avisado", 0)
    )

    aguardando_autorizacao = por_situacao.get(
        "Aguardando autorização do orçamento", 0
    )

    em_reparo = por_situacao.get(
        "Autorizado, Reparo em andamento", 0
    )

    aguardando_peca = por_situacao.get(
        "Autorizado, Aguardando peça", 0
    )

    em_analise = por_situacao.get(
        "Máquina em análise", 0
    )

    total_aberto = round(float(financeiro["total_aberto"] or 0), 2)
    total_hoje = round(float(financeiro["total_hoje"] or 0), 2)

    cards = {
        "em_analise": em_analise,
        "aguardando_autorizacao": aguardando_autorizacao,
        "em_reparo": em_reparo,
        "aguardando_peca": aguardando_peca,
        "prontos": prontos,
        "equipamentos_loja": sum(por_situacao.values()),
    }

    # Valores em dinheiro só aparecem pra quem é admin.
    if session.get("papel") == "admin":
        cards["total_aberto"] = total_aberto
        cards["total_hoje"] = total_hoje

    return jsonify({
        "cards": cards,
        "por_situacao": por_situacao,
        "ultimas": [dict(x) for x in ultimas]
    })


@app.get("/api/dashboard/avisos")
@login_obrigatorio
def dashboard_avisos():
    """OS abertas que precisam de atenção: muito tempo paradas ou já
    prontas aguardando aviso ao cliente."""
    c = conn()

    rows = c.execute(
        """
        SELECT
            o.id,
            o.numero_os,
            o.situacao,
            o.data_entrada,
            c.nome AS cliente_nome,
            e.tipo AS equipamento_tipo,
            e.marca AS equipamento_marca,
            e.modelo AS equipamento_modelo,
            CAST(julianday('now','-3 hours') - julianday(o.data_entrada) AS INTEGER) AS dias
        FROM ordens_servico o
        INNER JOIN clientes c ON c.id=o.cliente_id
        INNER JOIN equipamentos e ON e.id=o.equipamento_id
        WHERE COALESCE(o.finalizado,0)=0
        """
    ).fetchall()

    c.close()

    avisos = []

    for r in rows:
        situacao = str(r["situacao"] or "").strip()
        dias = max(int(r["dias"] or 0), 0)
        equipamento = " ".join(
            filter(None, [r["equipamento_tipo"], r["equipamento_marca"], r["equipamento_modelo"]])
        )

        if situacao in ("Pronto, avisar cliente",):
            nivel = "green"
            titulo = "Pronto para retirada — avisar cliente"
        elif situacao in ("Aguardando autorização do orçamento",) and dias >= 3:
            nivel = "orange"
            titulo = "Orçamento aguardando resposta do cliente"
        elif dias >= 15:
            nivel = "red"
            titulo = "OS parada há muito tempo"
        elif dias >= 8:
            nivel = "orange"
            titulo = "OS precisa de atenção"
        elif dias >= 4:
            nivel = "yellow"
            titulo = "OS aguardando andamento"
        else:
            continue

        avisos.append({
            "id": r["id"],
            "numero_os": formatar_os(r["numero_os"]),
            "cliente": r["cliente_nome"],
            "equipamento": equipamento,
            "situacao": situacao,
            "dias": dias,
            "nivel": nivel,
            "titulo": titulo
        })

    ordem_nivel = {"red": 0, "orange": 1, "yellow": 2, "green": 3}
    avisos.sort(key=lambda a: (ordem_nivel.get(a["nivel"], 9), -a["dias"]))

    return jsonify({"total": len(avisos), "avisos": avisos[:20]})


# =========================================================
# ABRIR OS
# =========================================================

@app.post("/api/os")
@login_obrigatorio
def add_os():
    d = request.get_json() or {}

    if not d.get("cliente_id") or not d.get("equipamento_id"):
        return jsonify(
            erro="Cliente e equipamento são obrigatórios."
        ), 400

    c = conn()

    cliente = c.execute(
        "SELECT * FROM clientes WHERE id=?",
        (d.get("cliente_id"),)
    ).fetchone()

    if not cliente:
        c.close()
        return jsonify(erro="Cliente não encontrado."), 404

    equipamento = c.execute(
        """
        SELECT *
        FROM equipamentos
        WHERE id=?
          AND cliente_id=?
        """,
        (
            d.get("equipamento_id"),
            d.get("cliente_id")
        )
    ).fetchone()

    if not equipamento:
        c.close()
        return jsonify(
            erro="Equipamento não encontrado para este cliente."
        ), 404

    # CONTADOR OFICIAL DE PRODUÇÃO
    # O sistema antigo terminou na OS 14560. O FG_WEB continua em 14561.
    # O número nunca volta para trás nem reutiliza OS excluída.
    c.execute("""
        CREATE TABLE IF NOT EXISTS controle_os (
            id INTEGER PRIMARY KEY CHECK (id=1),
            proximo_numero INTEGER NOT NULL
        )
    """)
    c.execute(
        "INSERT OR IGNORE INTO controle_os (id, proximo_numero) VALUES (1, 14561)"
    )

    contador = c.execute(
        "SELECT proximo_numero FROM controle_os WHERE id=1"
    ).fetchone()
    n = max(int(contador["proximo_numero"]), 14561)

    # Se o banco tiver OS novas abaixo da sequência oficial, pula para
    # depois da maior existente. Assim nunca repetimos um número.
    maior_existente = c.execute(
        "SELECT COALESCE(MAX(numero_os), 0) AS maior FROM ordens_servico"
    ).fetchone()["maior"]
    n = max(n, int(maior_existente or 0) + 1)

    # Segurança: nunca reutiliza um número que já exista.
    if c.execute(
        "SELECT 1 FROM ordens_servico WHERE numero_os=?",
        (n,)
    ).fetchone():
        c.rollback()
        c.close()
        return jsonify(
            erro=f"O número de OS {n:06d} já existe. Produção interrompida para evitar conflito."
        ), 409

    c.execute(
        "UPDATE controle_os SET proximo_numero=? WHERE id=1",
        (n + 1,)
    )

    cur = c.execute(
        """
        INSERT INTO ordens_servico
        (
            legacy_id,
            numero_os,
            cliente_id,
            equipamento_id,
            defeito,
            observacoes,
            data_entrada
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cliente["legacy_id"],
            n,
            d.get("cliente_id"),
            d.get("equipamento_id"),
            d.get("defeito"),
            d.get("observacoes"),
            agora_br()
        )
    )

    os_id = cur.lastrowid

    c.execute(
        """
        INSERT INTO os_historico
        (
            os_id,
            evento,
            criado_em
        )
        VALUES (?, ?, ?)
        """,
        (os_id, "OS aberta", agora_br())
    )

    c.commit()

    row = c.execute(
        "SELECT * FROM ordens_servico WHERE id=?",
        (os_id,)
    ).fetchone()

    c.close()

    resultado = dict(row)
    resultado["legacy"] = False
    resultado["numero_os"] = f"{int(resultado['numero_os']):06d}"

    return jsonify(resultado)


# =========================================================
# INICIALIZAÇÃO
# =========================================================

init()


if __name__ == "__main__":
    # Em produção (Render), quem sobe o app é o gunicorn (ver
    # requirements.txt / comando de start), então este bloco só roda
    # quando você executa "python app.py" localmente para testar.
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=os.environ.get("FLASK_DEBUG") == "1"
    )
