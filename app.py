from flask import Flask, render_template, jsonify, request, redirect, url_for
from werkzeug.utils import secure_filename
import os
import re
import unicodedata
import uuid
import pyodbc

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

def access_int(value):
    """Converte IDs do Access/ODBC de forma robusta."""
    if value is None:
        return 0

    # Alguns campos AutoNumber/Number do Access podem chegar pelo ODBC
    # como bytes ou como string contendo bytes binários.
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value).rstrip(b'\x00')
        if not raw:
            return 0
        # Se forem bytes ASCII normais (ex.: b'258')
        try:
            txt = raw.decode('ascii').strip()
            if txt.isdigit():
                return int(txt)
        except Exception:
            pass
        # Inteiro binário little-endian do Access.
        return int.from_bytes(raw, byteorder='little', signed=False)

    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0
        # String numérica normal.
        try:
            return int(s)
        except ValueError:
            pass
        # String que contém bytes binários (ex.: '\x01\x00\x00\x00').
        try:
            raw = value.encode('latin1').rstrip(b'\x00')
            if not raw:
                return 0
            return int.from_bytes(raw, byteorder='little', signed=False)
        except Exception:
            raise ValueError(f"ID do Access inválido: {value!r}")

    return int(value)

app = Flask(__name__)

DB_PATH = r"C:\FG FOTOS\SGFE.accdb"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BALIZAMENTO_FOLDER = os.path.join(BASE_DIR, "balizamentos")
os.makedirs(BALIZAMENTO_FOLDER, exist_ok=True)
ALLOWED_BALIZAMENTO_EXTENSIONS = {"pdf"}


def _ensure_event_balizamento_fields(conn):
    """Cria, sem intervenção manual, os campos do arquivo de balizamento por evento."""
    cur = conn.cursor()
    campos = [
        ("BalizamentoArquivo", "TEXT(255)"),
        ("BalizamentoCaminho", "TEXT(255)"),
        ("BalizamentoData", "DATETIME"),
    ]
    for nome, tipo in campos:
        try:
            cur.execute(f"SELECT TOP 1 [{nome}] FROM tblEvento")
        except Exception:
            cur.execute(f"ALTER TABLE tblEvento ADD COLUMN [{nome}] {tipo}")
            conn.commit()


def _ensure_balizamento_provas(conn):
    """Tabela estruturada das ocorrências lidas do PDF do evento."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT TOP 1 IDBalizamento FROM tblBalizamentoProvas")
        return
    except Exception:
        pass
    cur.execute("""
        CREATE TABLE tblBalizamentoProvas (
            IDBalizamento AUTOINCREMENT PRIMARY KEY,
            IDEvento LONG,
            NumeroProva LONG,
            NomeProva TEXT(255),
            NumeroSerie LONG,
            Raia LONG,
            NomeAtleta TEXT(255),
            Registro TEXT(30),
            Pagina LONG
        )
    """)
    conn.commit()


def _normalizar_nome(texto):
    texto = unicodedata.normalize("NFKD", str(texto or ""))
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    texto = re.sub(r"\s+", " ", texto).strip().upper()
    return texto


def _extrair_provas_balizamento(pdf_path):
    """Extrai PROVA + SÉRIE + RAIA + ATLETA de balizamentos SGE/Piscina."""
    if PdfReader is None:
        raise RuntimeError("A biblioteca pypdf não está instalada no ambiente do SGFE.")

    reader = PdfReader(pdf_path)
    registros = []
    prova_num = None
    prova_nome = ""
    serie = None

    for pagina_num, pagina in enumerate(reader.pages, 1):
        texto = pagina.extract_text() or ""
        for bruto in texto.splitlines():
            linha = " ".join(str(bruto).split()).strip()
            if not linha:
                continue

            # Ex.: "200M MEDLEY FEM.ª PROVA3 INFANTIL 21/08/2026 Final"
            mp = re.search(r"PROVA\s*(\d+)", linha, re.I)
            if not mp:
                mp = re.search(r"(\d+)\s*[ªº]?\s*PROVA\b", linha, re.I)
            if mp:
                prova_num = int(mp.group(1))
                nome = re.sub(r"\bPROVA\s*\d+\b", " ", linha, flags=re.I)
                nome = re.sub(r"\b\d{2}/\d{2}/\d{4}\b", " ", nome)
                nome = re.sub(r"\bFinal\b", " ", nome, flags=re.I)
                nome = re.sub(r"[ªº]", "", nome)
                nome = re.sub(r"\s+", " ", nome).strip(" .")
                prova_nome = nome
                serie = None
                continue

            ms = re.match(r"^(\d+)\s*[ªº]?\s*S[ÉE]RIE\b", linha, re.I)
            if ms:
                serie = int(ms.group(1))
                continue

            # Linha de atleta: RAIA + NOME + REGISTRO + ANO + ...
            ma = re.match(r"^(\d+)\s+(.+?)\s+(V?\d{5,7})\s+\d{4}\s+", linha)
            if ma and prova_num and serie is not None:
                raia = int(ma.group(1))
                nome_atleta = ma.group(2).strip()
                registro = ma.group(3).strip()
                registros.append({
                    "prova": prova_num,
                    "nome_prova": prova_nome,
                    "serie": serie,
                    "raia": raia,
                    "nome_atleta": nome_atleta,
                    "registro": registro,
                    "pagina": pagina_num,
                    "nome_normalizado": _normalizar_nome(nome_atleta),
                })

    # Remove duplicidades de extração sem perder ocorrências reais.
    unicos = {}
    for r in registros:
        chave = (r["prova"], r["serie"], r["raia"], r["nome_normalizado"])
        unicos[chave] = r
    return list(unicos.values())


def _evento_tem_balizamento(cur, evento_id):
    try:
        cur.execute("SELECT BalizamentoArquivo FROM tblEvento WHERE IDEvento=?", [evento_id])
        r = cur.fetchone()
        return bool(r and r[0])
    except Exception:
        return False


def _ensure_status_pagamento(conn):
    """Garante o status financeiro da OS, incluindo CORTESIA."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT TOP 1 StatusPagamento FROM tblVendaPacotes")
    except Exception:
        cur.execute("ALTER TABLE tblVendaPacotes ADD COLUMN StatusPagamento TEXT(20)")
        conn.commit()

    # Migra registros antigos: quem já tem pagamento vira PAGO; os demais ABERTO.
    cur.execute("""
        UPDATE tblVendaPacotes
        SET StatusPagamento='pago'
        WHERE (StatusPagamento Is Null OR StatusPagamento='')
          AND EXISTS (SELECT 1 FROM tblPagamento AS P WHERE P.IDVenda=tblVendaPacotes.IDVenda)
    """)
    cur.execute("""
        UPDATE tblVendaPacotes
        SET StatusPagamento='aberto'
        WHERE StatusPagamento Is Null OR StatusPagamento=''
    """)
    conn.commit()


def _ensure_status_venda_cancelado(conn):
    """Garante o status operacional CANCELADO em tblStatusVenda."""
    cur = conn.cursor()
    cur.execute("""
        SELECT TOP 1 IDStatusVenda
        FROM tblStatusVenda
        WHERE UCASE(StatusVenda)='CANCELADO'
    """)
    if cur.fetchone():
        return
    cur.execute("""
        INSERT INTO tblStatusVenda (StatusVenda)
        VALUES ('CANCELADO')
    """)
    conn.commit()


def _ensure_status_agendamento_cancelado(conn):
    """Garante o status CANCELADO em tblStatusAgendamento."""
    cur = conn.cursor()
    cur.execute("""
        SELECT TOP 1 IDStatusAgendamento
        FROM tblStatusAgendamento
        WHERE UCASE(StatusAgendamento)='CANCELADO'
    """)
    if cur.fetchone():
        return
    cur.execute("""
        INSERT INTO tblStatusAgendamento (StatusAgendamento)
        VALUES ('CANCELADO')
    """)
    conn.commit()


def _cancelar_venda_operacional(cur, id_venda):
    """
    Cancela uma OS sem apagar histórico e sem lançar pagamento.
    Também cancela o agendamento e desmarca todas as provas do balizamento
    vinculadas ao agendamento.
    """
    id_venda = access_int(id_venda)

    cur.execute("""
        SELECT IDVenda, IDAgendamento, IDEvento, IDCliente
        FROM tblVendaPacotes
        WHERE IDVenda=?
    """, [id_venda])
    venda = cur.fetchone()
    if not venda:
        raise ValueError("Venda/OS não encontrada.")

    id_agendamento = access_int(venda[1]) if venda[1] is not None else 0
    id_evento = access_int(venda[2]) if venda[2] is not None else 0

    cur.execute("""
        SELECT TOP 1 IDPagamento
        FROM tblPagamento
        WHERE IDVenda=?
    """, [id_venda])
    if cur.fetchone():
        raise ValueError("Esta OS já possui pagamento registrado e não pode ser cancelada.")

    cur.execute("""
        SELECT IDStatusVenda
        FROM tblStatusVenda
        WHERE UCASE(StatusVenda)='CANCELADO'
    """)
    status_venda = cur.fetchone()
    if not status_venda:
        raise ValueError("O status CANCELADO não foi encontrado em tblStatusVenda.")
    id_status_venda_cancelado = access_int(status_venda[0])

    cur.execute("""
        SELECT IDStatusAgendamento
        FROM tblStatusAgendamento
        WHERE UCASE(StatusAgendamento)='CANCELADO'
    """)
    status_ag = cur.fetchone()
    if not status_ag:
        raise ValueError("O status CANCELADO não foi encontrado em tblStatusAgendamento.")
    id_status_ag_cancelado = access_int(status_ag[0])

    cur.execute("""
        UPDATE tblVendaPacotes
        SET IDStatusVenda=?,
            StatusPagamento='cancelado',
            Finalizado=True,
            DataFinalizacao=Now()
        WHERE IDVenda=?
    """, [id_status_venda_cancelado, id_venda])

    if id_agendamento:
        cur.execute("""
            UPDATE tblAgendamentos
            SET IDStatusAgendamento=?
            WHERE IDAgendamento=?
        """, [id_status_ag_cancelado, id_agendamento])

        try:
            cur.execute("""
                UPDATE tblMarcacaoBalizamento
                SET Ativa=False
                WHERE IDAgendamento=?
                  AND Ativa=True
            """, [id_agendamento])
        except Exception:
            pass

    return id_evento


def get_connection():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"Banco não encontrado: {DB_PATH}")

    drivers = [d for d in pyodbc.drivers() if "Access Driver" in d]
    if not drivers:
        raise RuntimeError("Driver Microsoft Access não encontrado.")

    driver = drivers[-1]
    conn_str = f"DRIVER={{{driver}}};DBQ={DB_PATH};"
    conn = pyodbc.connect(conn_str)
    _ensure_status_pagamento(conn)
    _ensure_status_venda_cancelado(conn)
    _ensure_status_agendamento_cancelado(conn)
    try:
        _ensure_event_balizamento_fields(conn)
        _ensure_balizamento_provas(conn)
    except Exception:
        pass
    try:
        _ensure_balizamento(conn)
        _ensure_balizamento_manual(conn)
    except NameError:
        # A definição da tabela fica disponível antes do primeiro uso efetivo.
        pass
    return conn


def scalar(cursor, sql, default=0, params=None):
    """Executa uma consulta escalar, aceitando parâmetros opcionais."""
    try:
        if params is None:
            cursor.execute(sql)
        else:
            cursor.execute(sql, params)
        row = cursor.fetchone()
        if not row or row[0] is None:
            return default
        return row[0]
    except Exception:
        return default


def money(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


# =========================================================
# BALIZAMENTO - MARCAÇÃO FOTOGRÁFICA
# =========================================================
#
# Regra operacional:
#   • 1º atleta seu na mesma PROVA + SÉRIE = AMARELO
#   • 2º atleta seu (ou seguintes) na mesma PROVA + SÉRIE = AZUL-CLARO
#   • O segundo atleta NÃO é bloqueado.
#   • A API devolve imediatamente quem já está marcado na série.
#
# A estrutura é criada automaticamente no Access para não exigir
# alteração manual no banco nesta atualização.
# =========================================================

def _ensure_balizamento(conn):
    """Garante a tabela de marcações do balizamento no Access."""
    cur = conn.cursor()

    try:
        cur.execute("SELECT TOP 1 IDMarcacao FROM tblMarcacaoBalizamento")
        return
    except Exception:
        pass

    cur.execute("""
        CREATE TABLE tblMarcacaoBalizamento (
            IDMarcacao AUTOINCREMENT PRIMARY KEY,
            IDEvento LONG,
            IDCliente LONG,
            IDAgendamento LONG,
            NumeroProva LONG,
            NomeProva TEXT(255),
            NumeroSerie LONG,
            Raia LONG,
            Cor TEXT(20),
            DataMarcacao DATETIME,
            Ativa YESNO
        )
    """)
    conn.commit()


def _ensure_balizamento_manual(conn):
    """Garante a tabela de provas definidas manualmente antes da marcação."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT TOP 1 IDManual FROM tblBalizamentoManualProvas")
        return
    except Exception:
        pass

    cur.execute("""
        CREATE TABLE tblBalizamentoManualProvas (
            IDManual AUTOINCREMENT PRIMARY KEY,
            IDEvento LONG,
            IDCliente LONG,
            IDAgendamento LONG,
            NumeroProva LONG,
            NomeProva TEXT(255),
            NumeroSerie LONG,
            Raia LONG,
            DataCriacao DATETIME,
            Ativa YESNO
        )
    """)
    conn.commit()


def _recalcular_cores_serie(cur, id_evento, numero_prova, numero_serie):
    """
    Recalcula a cor dos atletas marcados naquela PROVA + SÉRIE.
    A ordem da marcação é preservada pelo IDMarcacao.
    """
    cur.execute("""
        SELECT IDMarcacao
        FROM tblMarcacaoBalizamento
        WHERE IDEvento=?
          AND NumeroProva=?
          AND NumeroSerie=?
          AND Ativa=True
        ORDER BY IDMarcacao ASC
    """, [id_evento, numero_prova, numero_serie])

    ids = [access_int(r[0]) for r in cur.fetchall()]

    for pos, id_marcacao in enumerate(ids):
        cor = "amarelo" if pos == 0 else "azul-claro"
        cur.execute("""
            UPDATE tblMarcacaoBalizamento
            SET Cor=?
            WHERE IDMarcacao=?
        """, [cor, id_marcacao])


def _balizamento_marcacoes_serie(cur, id_evento, numero_prova, numero_serie):
    """Retorna os atletas já marcados na mesma prova e série."""
    cur.execute("""
        SELECT
            M.IDMarcacao,
            M.IDCliente,
            C.Nome AS Atleta,
            M.IDAgendamento,
            M.Raia,
            M.Cor,
            M.DataMarcacao
        FROM
            (tblMarcacaoBalizamento AS M
             LEFT JOIN tblClientes AS C
               ON M.IDCliente=C.IDCliente)
        WHERE M.IDEvento=?
          AND M.NumeroProva=?
          AND M.NumeroSerie=?
          AND M.Ativa=True
        ORDER BY M.IDMarcacao ASC
    """, [id_evento, numero_prova, numero_serie])

    result = []
    for r in cur.fetchall():
        result.append({
            "id_marcacao": access_int(r[0]),
            "id_cliente": access_int(r[1]),
            "atleta": str(r[2] or ""),
            "id_agendamento": access_int(r[3]) if r[3] is not None else 0,
            "raia": access_int(r[4]) if r[4] is not None else 0,
            "cor": str(r[5] or "amarelo"),
            "data_marcacao": (
                r[6].strftime("%d/%m/%Y %H:%M")
                if hasattr(r[6], "strftime")
                else str(r[6] or "")
            ),
        })
    return result


@app.route("/api/balizamento/verificar")
def api_balizamento_verificar():
    """
    Consulta a série antes da marcação.

    Parâmetros:
      evento, prova, serie
    Opcional:
      cliente
    """
    conn = None
    try:
        id_evento = access_int(request.args.get("evento") or 0)
        numero_prova = access_int(request.args.get("prova") or 0)
        numero_serie = access_int(request.args.get("serie") or 0)
        id_cliente = access_int(request.args.get("cliente") or 0)

        if not id_evento or not numero_prova or not numero_serie:
            return jsonify({
                "ok": False,
                "erro": "Informe evento, prova e série."
            }), 400

        conn = get_connection()
        _ensure_balizamento(conn)
        cur = conn.cursor()

        marcados = _balizamento_marcacoes_serie(
            cur, id_evento, numero_prova, numero_serie
        )

        outros = [
            m for m in marcados
            if not id_cliente or m["id_cliente"] != id_cliente
        ]

        return jsonify({
            "ok": True,
            "conflito": bool(outros),
            "quantidade": len(marcados),
            "marcados": marcados,
            "outros_atletas": outros,
            "mensagem": (
                "Esta série já possui atleta marcado."
                if outros
                else "Série disponível para marcação."
            ),
        })
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/balizamento/marcacoes", methods=["GET"])
def api_balizamento_marcacoes():
    """Lista as marcações do balizamento de um evento."""
    conn = None
    try:
        id_evento = access_int(request.args.get("evento") or 0)
        if not id_evento:
            return jsonify({
                "ok": False,
                "erro": "Informe o evento."
            }), 400

        conn = get_connection()
        _ensure_balizamento(conn)
        cur = conn.cursor()

        cur.execute("""
            SELECT
                M.IDMarcacao,
                M.IDEvento,
                M.IDCliente,
                C.Nome AS Atleta,
                M.IDAgendamento,
                M.NumeroProva,
                M.NomeProva,
                M.NumeroSerie,
                M.Raia,
                M.Cor,
                M.DataMarcacao
            FROM
                (tblMarcacaoBalizamento AS M
                 LEFT JOIN tblClientes AS C
                   ON M.IDCliente=C.IDCliente)
            WHERE M.IDEvento=?
              AND M.Ativa=True
            ORDER BY M.NumeroProva, M.NumeroSerie, M.IDMarcacao
        """, [id_evento])

        data = []
        for r in cur.fetchall():
            data.append({
                "id_marcacao": access_int(r[0]),
                "evento": access_int(r[1]),
                "cliente": access_int(r[2]),
                "atleta": str(r[3] or ""),
                "agendamento": access_int(r[4]) if r[4] is not None else 0,
                "prova": access_int(r[5]) if r[5] is not None else 0,
                "nome_prova": str(r[6] or ""),
                "serie": access_int(r[7]) if r[7] is not None else 0,
                "raia": access_int(r[8]) if r[8] is not None else 0,
                "cor": str(r[9] or "amarelo"),
                "data_marcacao": (
                    r[10].strftime("%d/%m/%Y %H:%M")
                    if hasattr(r[10], "strftime")
                    else str(r[10] or "")
                ),
            })

        return jsonify({
            "ok": True,
            "evento": id_evento,
            "marcacoes": data
        })
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/balizamento/manual-lote", methods=["POST"])
def api_balizamento_manual_lote():
    """Salva em lote as provas definidas manualmente antes da marcação."""
    conn = None
    try:
        dados = request.get_json(silent=True) or {}
        id_evento = access_int(dados.get("evento") or 0)
        id_cliente = access_int(dados.get("cliente") or 0)
        id_agendamento = access_int(dados.get("agendamento") or 0)
        provas = dados.get("provas") or []

        if not id_evento or not id_cliente or not id_agendamento:
            return jsonify({"ok": False, "erro": "Informe evento, atleta e agendamento."}), 400
        if not isinstance(provas, list) or not provas:
            return jsonify({"ok": False, "erro": "Adicione pelo menos uma prova."}), 400

        conn = get_connection()
        _ensure_balizamento_manual(conn)
        cur = conn.cursor()

        # Regrava a definição manual ativa desse agendamento. As marcações de
        # foto já feitas não são apagadas, apenas a lista de definição é refeita.
        cur.execute("""
            UPDATE tblBalizamentoManualProvas
            SET Ativa=False
            WHERE IDEvento=? AND IDCliente=? AND IDAgendamento=? AND Ativa=True
        """, [id_evento, id_cliente, id_agendamento])

        vistos = set()
        quantidade = 0
        for item in provas:
            prova = access_int(item.get("prova") or 0)
            serie = access_int(item.get("serie") or 0)
            raia = access_int(item.get("raia") or 0)
            nome = str(item.get("nome") or "PROVA MANUAL").strip() or "PROVA MANUAL"
            chave = (prova, serie)
            if not prova or not serie:
                conn.rollback()
                return jsonify({"ok": False, "erro": "Todas as provas precisam ter número e série."}), 400
            if chave in vistos:
                continue
            vistos.add(chave)
            cur.execute("""
                INSERT INTO tblBalizamentoManualProvas
                (IDEvento, IDCliente, IDAgendamento, NumeroProva, NomeProva,
                 NumeroSerie, Raia, DataCriacao, Ativa)
                VALUES (?, ?, ?, ?, ?, ?, ?, Now(), True)
            """, [id_evento, id_cliente, id_agendamento, prova, nome, serie, raia or None])
            quantidade += 1

        conn.commit()
        return jsonify({"ok": True, "quantidade": quantidade})
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"ok": False, "erro": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/balizamento/marcar", methods=["POST"])
def api_balizamento_marcar():
    """
    Marca um atleta no balizamento.

    A marcação é sempre permitida.
    Se já houver outro atleta seu na mesma PROVA + SÉRIE,
    a nova marcação recebe AZUL-CLARO e a resposta traz
    quem já estava marcado.
    """
    conn = None
    try:
        dados = request.get_json(silent=True) or request.form

        id_evento = access_int(dados.get("evento") or dados.get("id_evento") or 0)
        id_cliente = access_int(dados.get("cliente") or dados.get("id_cliente") or 0)
        id_agendamento = access_int(
            dados.get("agendamento") or dados.get("id_agendamento") or 0
        )
        numero_prova = access_int(dados.get("prova") or dados.get("numero_prova") or 0)
        numero_serie = access_int(dados.get("serie") or dados.get("numero_serie") or 0)
        raia = access_int(dados.get("raia") or 0)
        nome_prova = str(
            dados.get("nome_prova") or dados.get("prova_nome") or ""
        ).strip()

        if not id_evento or not id_cliente or not numero_prova or not numero_serie:
            return jsonify({
                "ok": False,
                "erro": "Informe evento, atleta, prova e série."
            }), 400

        conn = get_connection()
        _ensure_balizamento(conn)
        cur = conn.cursor()

        # Se o atleta já estiver marcado nessa prova, atualizamos a posição
        # em vez de criar uma duplicata.
        cur.execute("""
            SELECT TOP 1 IDMarcacao
            FROM tblMarcacaoBalizamento
            WHERE IDEvento=?
              AND IDCliente=?
              AND NumeroProva=?
              AND Ativa=True
            ORDER BY IDMarcacao DESC
        """, [id_evento, id_cliente, numero_prova])

        existente = cur.fetchone()

        serie_anterior = None

        if existente:
            id_marcacao = access_int(existente[0])

            cur.execute("""
                SELECT NumeroSerie
                FROM tblMarcacaoBalizamento
                WHERE IDMarcacao=?
            """, [id_marcacao])
            anterior = cur.fetchone()
            if anterior:
                serie_anterior = access_int(anterior[0])

            cur.execute("""
                UPDATE tblMarcacaoBalizamento
                SET
                    IDAgendamento=?,
                    NomeProva=?,
                    NumeroSerie=?,
                    Raia=?,
                    DataMarcacao=Now(),
                    Ativa=True
                WHERE IDMarcacao=?
            """, [
                id_agendamento or None,
                nome_prova,
                numero_serie,
                raia or None,
                id_marcacao
            ])
        else:
            cur.execute("""
                INSERT INTO tblMarcacaoBalizamento
                (
                    IDEvento,
                    IDCliente,
                    IDAgendamento,
                    NumeroProva,
                    NomeProva,
                    NumeroSerie,
                    Raia,
                    Cor,
                    DataMarcacao,
                    Ativa
                )
                VALUES (?,?,?,?,?,?,?, ?,Now(),True)
            """, [
                id_evento,
                id_cliente,
                id_agendamento or None,
                numero_prova,
                nome_prova,
                numero_serie,
                raia or None,
                "amarelo"
            ])

            cur.execute("SELECT @@IDENTITY")
            id_marcacao = access_int(cur.fetchone()[0])

        # A cor é recalculada depois da gravação.
        _recalcular_cores_serie(
            cur, id_evento, numero_prova, numero_serie
        )

        # Se a marcação foi movida de uma série para outra, a série
        # anterior também precisa ter as cores reorganizadas.
        if (
            serie_anterior
            and serie_anterior != numero_serie
        ):
            _recalcular_cores_serie(
                cur, id_evento, numero_prova, serie_anterior
            )

        conn.commit()

        # Busca novamente a série já com as cores finais.
        marcados = _balizamento_marcacoes_serie(
            cur, id_evento, numero_prova, numero_serie
        )

        atual = next(
            (m for m in marcados if m["id_marcacao"] == id_marcacao),
            None
        )

        outros = [
            m for m in marcados
            if m["id_cliente"] != id_cliente
        ]

        return jsonify({
            "ok": True,
            "marcacao": atual,
            "cor": atual["cor"] if atual else "amarelo",
            "conflito": bool(outros),
            "mesma_serie": bool(outros),
            "quantidade": len(marcados),
            "marcados": marcados,
            "outros_atletas": outros,
            "mensagem": (
                f"ATENÇÃO: esta série já possui "
                f"{len(outros)} atleta(s) marcado(s)."
                if outros
                else "Atleta marcado com sucesso."
            ),
        })
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"ok": False, "erro": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/balizamento/desmarcar/<int:id_marcacao>", methods=["POST", "DELETE"])
def api_balizamento_desmarcar(id_marcacao):
    """Desmarca uma marcação e reorganiza as cores da série."""
    conn = None
    try:
        conn = get_connection()
        _ensure_balizamento(conn)
        cur = conn.cursor()

        cur.execute("""
            SELECT IDEvento, NumeroProva, NumeroSerie
            FROM tblMarcacaoBalizamento
            WHERE IDMarcacao=?
        """, [id_marcacao])

        row = cur.fetchone()
        if not row:
            return jsonify({
                "ok": False,
                "erro": "Marcação não encontrada."
            }), 404

        id_evento = access_int(row[0])
        numero_prova = access_int(row[1])
        numero_serie = access_int(row[2])

        cur.execute("""
            UPDATE tblMarcacaoBalizamento
            SET Ativa=False
            WHERE IDMarcacao=?
        """, [id_marcacao])

        _recalcular_cores_serie(
            cur, id_evento, numero_prova, numero_serie
        )

        conn.commit()

        marcados = _balizamento_marcacoes_serie(
            cur, id_evento, numero_prova, numero_serie
        )

        return jsonify({
            "ok": True,
            "evento": id_evento,
            "prova": numero_prova,
            "serie": numero_serie,
            "marcados": marcados
        })
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"ok": False, "erro": str(e)}), 500
    finally:
        if conn:
            conn.close()



@app.post("/agendamentos/<int:id_agendamento>/excluir")
def excluir_agendamento(id_agendamento):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        # Segurança: não permitir apagar um agendamento que já virou venda.
        cur.execute(
            "SELECT IDVendas FROM tblAgendamentos WHERE IDAgendamento=?",
            (id_agendamento,)
        )
        row = cur.fetchone()
        if not row:
            return redirect(url_for("web_agendamentos", erro="Agendamento não encontrado."))

        id_venda = row[0]
        if id_venda not in (None, 0, "", b"", bytearray()):
            try:
                venda_num = access_int(id_venda)
            except Exception:
                venda_num = 1
            if venda_num:
                return redirect(url_for(
                    "web_agendamentos",
                    erro="Este agendamento já está vinculado a uma venda e não pode ser excluído."
                ))

        cur.execute(
            "DELETE FROM tblAgendamentos WHERE IDAgendamento=?",
            (id_agendamento,)
        )
        conn.commit()
        return redirect(url_for("web_agendamentos", ok="Agendamento excluído com sucesso."))
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: pass
        return redirect(url_for("web_agendamentos", erro=str(e)))
    finally:
        if conn:
            conn.close()

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/evento-geral")
def api_evento_geral():
    """Retorna os indicadores do evento para o painel inicial."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        eventos = []
        cur.execute("""
            SELECT IDEvento, NomeEvento, DataEvento, Cidade, Ativo
            FROM tblEvento
            ORDER BY DataEvento DESC, NomeEvento
        """)
        for r in cur.fetchall():
            eventos.append({
                "id": access_int(r[0]),
                "nome": str(r[1] or ""),
                "data": r[2].strftime("%d/%m/%Y") if hasattr(r[2], "strftime") else str(r[2] or ""),
                "cidade": str(r[3] or ""),
                "ativo": bool(r[4]) if r[4] is not None else False,
            })

        evento_id_raw = request.args.get("evento", "").strip()
        evento_id = access_int(evento_id_raw) if evento_id_raw else (eventos[0]["id"] if eventos else 0)
        resumo = {
            "agendamentos": 0, "vendas": 0, "entregas_pendentes": 0,
            "pagamentos_pendentes": 0, "total_vendido": 0.0,
            "total_recebido": 0.0, "despesas": 0.0, "lucro": 0.0
        }
        evento_info = None

        if evento_id:
            cur.execute("""
                SELECT IDEvento, NomeEvento, DataEvento, Cidade, Ativo
                FROM tblEvento WHERE IDEvento=?
            """, [evento_id])
            r = cur.fetchone()
            if r:
                evento_info = {
                    "id": access_int(r[0]),
                    "nome": str(r[1] or ""),
                    "data": r[2].strftime("%d/%m/%Y") if hasattr(r[2], "strftime") else str(r[2] or ""),
                    "cidade": str(r[3] or ""),
                    "ativo": bool(r[4]) if r[4] is not None else False,
                }

                resumo["agendamentos"] = int(scalar(cur,
                    "SELECT Count(*) FROM tblAgendamentos WHERE IDEvento=?", 0, [evento_id]) or 0)

                cur.execute("""
                    SELECT V.IDVenda, V.ValorFinal, V.Finalizado, V.StatusPagamento
                    FROM tblVendaPacotes AS V WHERE V.IDEvento=?
                """, [evento_id])
                vendas_evento = cur.fetchall()
                resumo["vendas"] = len(vendas_evento)
                resumo["total_vendido"] = sum(float(r[1] or 0) for r in vendas_evento)
                resumo["entregas_pendentes"] = sum(1 for r in vendas_evento if r[2] is None or not bool(r[2]))

                pagamentos = {}
                cur.execute("""
                    SELECT P.IDVenda, Sum(P.ValorPago)
                    FROM tblPagamento AS P
                    INNER JOIN tblVendaPacotes AS V ON P.IDVenda=V.IDVenda
                    WHERE V.IDEvento=? GROUP BY P.IDVenda
                """, [evento_id])
                for r in cur.fetchall():
                    pagamentos[access_int(r[0])] = float(r[1] or 0)

                recebido = 0.0
                pendentes = 0
                for r in vendas_evento:
                    venda_id = access_int(r[0])
                    valor = float(r[1] or 0)
                    status_pg = str(r[3] or "aberto").strip().lower()
                    pago = pagamentos.get(venda_id, 0.0)
                    if status_pg == "cortesia":
                        continue
                    recebido += min(pago, valor) if valor > 0 else 0.0
                    if pago < valor - 0.009:
                        pendentes += 1
                resumo["total_recebido"] = recebido
                resumo["pagamentos_pendentes"] = pendentes

                try:
                    resumo["despesas"] = money(scalar(cur,
                        "SELECT Sum(ValorDespesa) FROM tblDespesasEvento WHERE IDEvento=?",
                        0, [evento_id]))
                except Exception:
                    resumo["despesas"] = 0.0
                resumo["lucro"] = resumo["total_recebido"] - resumo["despesas"]

        return jsonify({"ok": True, "eventos": eventos, "evento": evento_info, "resumo": resumo})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/dashboard")
def dashboard():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        # Indicadores reais do banco.
        atletas = scalar(cur, "SELECT Count(*) FROM tblClientes")
        agendamentos = scalar(cur, "SELECT Count(*) FROM tblAgendamentos")
        vendas = scalar(cur, "SELECT Count(*) FROM tblVendaPacotes")
        entregas_pendentes = scalar(
            cur,
            "SELECT Count(*) FROM tblVendaPacotes AS V "
            "LEFT JOIN tblStatusVenda AS S ON V.IDStatusVenda=S.IDStatusVenda "
            "WHERE (V.Finalizado = False OR V.Finalizado Is Null) "
            "AND (S.StatusVenda Is Null OR UCASE(S.StatusVenda) NOT LIKE '%CANCEL%')"
        )
        pagamentos_pendentes = scalar(
            cur,
            "SELECT Count(*) FROM tblVendaPacotes AS V "
            "LEFT JOIN tblPagamento AS P ON V.IDVenda = P.IDVenda "
            "LEFT JOIN tblStatusVenda AS S ON V.IDStatusVenda=S.IDStatusVenda "
            "WHERE P.IDPagamento Is Null "
            "AND (V.StatusPagamento Is Null OR V.StatusPagamento='aberto') "
            "AND (S.StatusVenda Is Null OR UCASE(S.StatusVenda) NOT LIKE '%CANCEL%')"
        )
        total_vendido = money(scalar(
            cur,
            "SELECT Sum(V.ValorFinal) FROM tblVendaPacotes AS V "
            "LEFT JOIN tblStatusVenda AS S ON V.IDStatusVenda=S.IDStatusVenda "
            "WHERE S.StatusVenda Is Null OR UCASE(S.StatusVenda) NOT LIKE '%CANCEL%'",
            0
        ))
        total_recebido = money(scalar(
            cur, "SELECT Sum(ValorPago) FROM tblPagamento", 0
        ))
        total_despesas = money(scalar(
            cur, "SELECT Sum(ValorDespesa) FROM tblDespesasEvento", 0
        ))

        # Últimas vendas.
        cur.execute(
            "SELECT TOP 8 V.IDVenda, V.DataVenda, C.Nome, E.NomeEvento, "
            "V.QtdProvas, V.ValorFinal, S.StatusVenda, V.Finalizado "
            "FROM ((tblVendaPacotes AS V "
            "INNER JOIN tblClientes AS C ON V.IDCliente=C.IDCliente) "
            "INNER JOIN tblEvento AS E ON V.IDEvento=E.IDEvento) "
            "LEFT JOIN tblStatusVenda AS S ON V.IDStatusVenda=S.IDStatusVenda "
            "ORDER BY V.IDVenda DESC"
        )
        vendas_rows = []
        for r in cur.fetchall():
            vendas_rows.append({
                "id": r[0],
                "data": r[1].strftime("%d/%m/%Y") if hasattr(r[1], "strftime") else str(r[1] or ""),
                "nome": str(r[2] or ""),
                "evento": str(r[3] or ""),
                "provas": int(r[4] or 0),
                "valor": money(r[5]),
                "status": str(r[6] or ""),
                "finalizado": bool(r[7]) if r[7] is not None else False
            })

        # Entregas pendentes.
        cur.execute(
            "SELECT TOP 8 V.IDVenda, C.Nome, E.NomeEvento, "
            "V.QtdProvas, V.ValorFinal "
            "FROM (tblVendaPacotes AS V "
            "INNER JOIN tblClientes AS C ON V.IDCliente=C.IDCliente) "
            "INNER JOIN tblEvento AS E ON V.IDEvento=E.IDEvento "
            "WHERE V.Finalizado=False OR V.Finalizado Is Null "
            "ORDER BY C.Nome"
        )
        entregas = []
        for r in cur.fetchall():
            entregas.append({
                "id": r[0], "nome": str(r[1] or ""), "evento": str(r[2] or ""),
                "provas": int(r[3] or 0), "valor": money(r[4])
            })

        # Pagamentos pendentes.
        cur.execute(
            "SELECT TOP 8 V.IDVenda, C.Nome, E.NomeEvento, V.ValorFinal "
            "FROM ((tblVendaPacotes AS V "
            "INNER JOIN tblClientes AS C ON V.IDCliente=C.IDCliente) "
            "INNER JOIN tblEvento AS E ON V.IDEvento=E.IDEvento) "
            "LEFT JOIN tblPagamento AS P ON V.IDVenda=P.IDVenda "
            "WHERE V.IDStatusVenda=1 AND P.IDPagamento Is Null "
            "ORDER BY C.Nome"
        )
        pagamentos = []
        for r in cur.fetchall():
            pagamentos.append({
                "id": r[0], "nome": str(r[1] or ""), "evento": str(r[2] or ""),
                "valor": money(r[3])
            })

        # Eventos.
        cur.execute(
            "SELECT TOP 8 IDEvento, NomeEvento, DataEvento, Cidade, Ativo "
            "FROM tblEvento ORDER BY DataEvento DESC"
        )
        eventos = []
        for r in cur.fetchall():
            eventos.append({
                "id": r[0], "nome": str(r[1] or ""),
                "data": r[2].strftime("%d/%m/%Y") if hasattr(r[2], "strftime") else str(r[2] or ""),
                "cidade": str(r[3] or ""),
                "ativo": bool(r[4]) if r[4] is not None else False
            })

        return jsonify({
            "ok": True,
            "indicadores": {
                "atletas": int(atletas or 0),
                "agendamentos": int(agendamentos or 0),
                "vendas": int(vendas or 0),
                "entregas_pendentes": int(entregas_pendentes or 0),
                "pagamentos_pendentes": int(pagamentos_pendentes or 0),
                "total_vendido": total_vendido,
                "total_recebido": total_recebido,
                "total_despesas": total_despesas,
                "saldo": total_recebido - total_despesas
            },
            "vendas": vendas_rows,
            "entregas": entregas,
            "pagamentos": pagamentos,
            "eventos": eventos
        })

    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/status")
def status():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        tabelas = []
        for row in cur.tables(tableType="TABLE"):
            if row.table_name and not row.table_name.startswith("MSys"):
                tabelas.append(row.table_name)
        return jsonify({"ok": True, "banco": DB_PATH, "tabelas": tabelas})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500
    finally:
        if conn:
            conn.close()


# =========================
# MODULOS WEB DO SGFE
# =========================

def fetch_table(table_name, search=""):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM [{table_name}]")
        cols = [d[0] for d in cur.description]
        result = []
        search_lower = search.lower().strip()

        for row in cur.fetchall():
            item = {}
            searchable = []
            for col, value in zip(cols, row):
                if hasattr(value, "strftime"):
                    value = value.strftime("%d/%m/%Y")
                elif value is not None:
                    value = str(value)
                else:
                    value = ""
                item[col] = value
                searchable.append(value.lower())

            if not search_lower or search_lower in " ".join(searchable):
                result.append(item)

        return cols, result
    finally:
        conn.close()


def module_page(page, title, subtitle, table_name, search_label):
    search = request.args.get("q", "").strip()
    columns, data = fetch_table(table_name, search)
    return render_template(
        "module.html",
        page=page,
        title=title,
        subtitle=subtitle,
        columns=columns,
        data=data,
        search=search,
        search_label=search_label,
        currency_columns=[],
        mensagem=request.args.get("ok", ""),
        erro=request.args.get("erro", "")
    )


@app.route("/atletas")
def web_atletas():
    search = request.args.get("q", "").strip()
    conn = get_connection()
    try:
        cur = conn.cursor()
        sql = """
        SELECT
            C.IDCliente,
            C.Nome AS Atleta,
            X.Sexo,
            C.AnoNascimento,
            E.NomeEquipe AS Equipe,
            M.Modalidade,
            C.Telefone,
            C.Contato,
            C.Cidade,
            C.Estado
        FROM ((tblClientes AS C
        LEFT JOIN tblEquipes AS E ON C.IDEquipe=E.IDEquipe)
        LEFT JOIN tblModalidades AS M ON C.IDModalidade=M.IDModalidade)
        LEFT JOIN tblSexos AS X ON C.IDSexo=X.IDSexo
        """
        params=[]
        if search:
            sql += """
            WHERE C.Nome LIKE ?
               OR E.NomeEquipe LIKE ?
               OR C.Telefone LIKE ?
               OR C.Contato LIKE ?
            """
            like = "%" + search + "%"
            params = [like, like, like, like]
        sql += " ORDER BY C.Nome"

        cur.execute(sql, params)
        columns=[d[0] for d in cur.description]
        data=[]
        for row in cur.fetchall():
            item={}
            for col,value in zip(columns,row):
                if value is None:
                    value=""
                else:
                    value=str(value)
                item[col]=value
            data.append(item)
    finally:
        conn.close()

    return render_template(
        "module.html", page="atletas",
        title="Atletas",
        subtitle="Cadastro e histórico dos atletas do SGFE",
        columns=["IDCliente","Atleta","Sexo","AnoNascimento","Equipe",
                 "Modalidade","Telefone","Contato","Cidade","Estado"],
        data=data, search=search,
        search_label="Pesquisar atleta, equipe ou telefone...",
        currency_columns=[],
        mensagem=request.args.get("ok", ""),
        erro=request.args.get("erro", "")
    )



@app.route("/atletas/<int:cliente_id>/editar", methods=["GET", "POST"])
def editar_atleta(cliente_id):
    conn = get_connection()
    erro = ""
    try:
        cur = conn.cursor()

        cur.execute("SELECT IDEquipe, NomeEquipe, Cidade, Estado FROM tblEquipes ORDER BY NomeEquipe")
        equipes = [{"id": access_int(r[0]), "nome": r[1] or "",
                    "cidade": r[2] or "", "estado": r[3] or ""}
                   for r in cur.fetchall()]

        cur.execute("SELECT IDSexo, Sexo FROM tblSexos ORDER BY Sexo")
        sexos = [{"id": access_int(r[0]), "nome": r[1] or ""} for r in cur.fetchall()]

        cur.execute("SELECT IDModalidade, Modalidade FROM tblModalidades ORDER BY Modalidade")
        modalidades = [{"id": access_int(r[0]), "nome": r[1] or ""} for r in cur.fetchall()]

        if request.method == "POST":
            nome = request.form.get("nome", "").strip()
            sexo_id = request.form.get("sexo_id", "").strip()
            ano = request.form.get("ano_nascimento", "").strip()
            equipe_id = request.form.get("equipe_id", "").strip()
            modalidade_id = request.form.get("modalidade_id", "").strip()
            telefone = request.form.get("telefone", "").strip()
            contato = request.form.get("contato", "").strip()
            cidade = request.form.get("cidade", "").strip()
            estado = request.form.get("estado", "").strip()
            email = request.form.get("email", "").strip()
            instagram = request.form.get("instagram", "").strip()
            observacoes = request.form.get("observacoes", "").strip()

            if not nome:
                raise ValueError("Informe o nome do atleta.")

            if telefone:
                cur.execute("""SELECT TOP 1 IDCliente FROM tblClientes
                               WHERE Nome=? AND Telefone=? AND IDCliente<>?""",
                            [nome, telefone, cliente_id])
            else:
                cur.execute("""SELECT TOP 1 IDCliente FROM tblClientes
                               WHERE Nome=? AND IDCliente<>?""",
                            [nome, cliente_id])
            if cur.fetchone():
                raise ValueError("Já existe outro atleta cadastrado com esses dados.")

            sets = ["Nome=?"]
            values = [nome]

            def num(campo, valor):
                if valor:
                    sets.append(f"{campo}=?")
                    values.append(access_int(valor))
                else:
                    sets.append(f"{campo}=Null")

            def text(campo, valor):
                if valor:
                    sets.append(f"{campo}=?")
                    values.append(valor)
                else:
                    sets.append(f"{campo}=Null")

            num("IDSexo", sexo_id)
            if ano:
                sets.append("AnoNascimento=?")
                values.append(int(ano))
            else:
                sets.append("AnoNascimento=Null")
            num("IDEquipe", equipe_id)
            num("IDModalidade", modalidade_id)
            text("Telefone", telefone)
            text("Contato", contato)
            text("Cidade", cidade)
            text("Estado", estado)
            text("Email", email)
            text("Instagram", instagram)
            text("Observacoes", observacoes)

            values.append(cliente_id)
            cur.execute(f"UPDATE tblClientes SET {','.join(sets)} WHERE IDCliente=?", values)
            conn.commit()
            return redirect(url_for("web_atletas", ok="Atleta atualizado com sucesso."))

        cur.execute("""
            SELECT IDCliente,Nome,IDSexo,AnoNascimento,IDEquipe,IDModalidade,
                   Telefone,Contato,Cidade,Estado,Email,Instagram,Observacoes
            FROM tblClientes WHERE IDCliente=?
        """, [cliente_id])
        r = cur.fetchone()
        if not r:
            return "Atleta não encontrado", 404

        form = {
            "nome": r[1] or "",
            "sexo_id": access_int(r[2]) if r[2] is not None else "",
            "ano_nascimento": r[3] if r[3] is not None else "",
            "equipe_id": access_int(r[4]) if r[4] is not None else "",
            "modalidade_id": access_int(r[5]) if r[5] is not None else "",
            "telefone": r[6] or "", "contato": r[7] or "",
            "cidade": r[8] or "", "estado": r[9] or "",
            "email": r[10] or "", "instagram": r[11] or "",
            "observacoes": r[12] or ""
        }
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        erro = str(e)
        form = dict(request.form) if request.method == "POST" else {}
    finally:
        conn.close()

    return render_template("editar_atleta.html",
        cliente_id=cliente_id, form=form, equipes=equipes,
        sexos=sexos, modalidades=modalidades, erro=erro, mensagem="")


@app.route("/atletas/<int:cliente_id>/excluir", methods=["POST"])
def excluir_atleta(cliente_id):
    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute("SELECT TOP 1 IDVenda FROM tblVendaPacotes WHERE IDCliente=?", [cliente_id])
        if cur.fetchone():
            return redirect(url_for("web_atletas",
                erro="Este atleta possui vendas/OS e não pode ser excluído. O histórico foi preservado."))

        cur.execute("SELECT TOP 1 IDAgendamento FROM tblAgendamentos WHERE IDCliente=?", [cliente_id])
        if cur.fetchone():
            return redirect(url_for("web_atletas",
                erro="Este atleta possui agendamentos e não pode ser excluído. O histórico foi preservado."))

        cur.execute("DELETE FROM tblClientes WHERE IDCliente=?", [cliente_id])
        conn.commit()
        return redirect(url_for("web_atletas", ok="Atleta excluído com sucesso."))
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return redirect(url_for("web_atletas", erro=str(e)))
    finally:
        conn.close()


@app.route("/atletas/<int:cliente_id>")
def atleta_detalhe(cliente_id):
    conn=get_connection()
    try:
        cur=conn.cursor()
        cur.execute("""
        SELECT C.IDCliente,C.Nome AS Atleta,X.Sexo,C.AnoNascimento,
               E.NomeEquipe AS Equipe,M.Modalidade,C.Telefone,C.Contato,
               C.Cidade,C.Estado,C.Email,C.Instagram,C.Observacoes
        FROM ((tblClientes AS C
        LEFT JOIN tblEquipes AS E ON C.IDEquipe=E.IDEquipe)
        LEFT JOIN tblModalidades AS M ON C.IDModalidade=M.IDModalidade)
        LEFT JOIN tblSexos AS X ON C.IDSexo=X.IDSexo
        WHERE C.IDCliente=?
        """,[cliente_id])
        r=cur.fetchone()
        if not r:
            return "Atleta não encontrado",404
        cols=[d[0] for d in cur.description]
        atleta=dict(zip(cols,[("" if v is None else str(v)) for v in r]))

        cur.execute("""
        SELECT V.IDVenda,V.DataVenda,C.Nome AS Atleta,E.NomeEvento AS Evento,
               E.DataEvento,V.QtdProvas,V.ValorFinal,
               S.StatusVenda AS Status,V.Finalizado,V.DataFinalizacao,
               IIf(IsNull(P.IDPagamento),'PENDENTE','PAGO') AS SituacaoPagamento
        FROM (((tblVendaPacotes AS V
        INNER JOIN tblClientes AS C ON V.IDCliente=C.IDCliente)
        INNER JOIN tblEvento AS E ON V.IDEvento=E.IDEvento)
        LEFT JOIN tblStatusVenda AS S ON V.IDStatusVenda=S.IDStatusVenda)
        LEFT JOIN tblPagamento AS P ON V.IDVenda=P.IDVenda
        WHERE V.IDCliente=?
        ORDER BY V.DataVenda DESC,V.IDVenda DESC
        """,[cliente_id])
        hcols=[d[0] for d in cur.description]
        historico=[]
        for row in cur.fetchall():
            item={}
            for col,value in zip(hcols,row):
                if hasattr(value,"strftime"):
                    value=value.strftime("%d/%m/%Y")
                elif value is None:
                    value=""
                else:
                    value=str(value)
                item[col]=value
            historico.append(item)
    finally:
        conn.close()

    return render_template("atleta.html", atleta=atleta, historico=historico)


@app.route("/eventos/<int:evento_id>/balizamento", methods=["GET", "POST"])
def gerenciar_balizamento_evento(evento_id):
    """Anexa/substitui o PDF do balizamento e importa suas provas automaticamente."""
    conn = get_connection()
    erro = ""
    try:
        cur = conn.cursor()
        cur.execute("SELECT IDEvento, NomeEvento, DataEvento, Cidade, BalizamentoArquivo, BalizamentoData FROM tblEvento WHERE IDEvento=?", [evento_id])
        evento = cur.fetchone()
        if not evento:
            return "Evento não encontrado", 404

        if request.method == "POST":
            arquivo = request.files.get("balizamento")
            if not arquivo or not arquivo.filename:
                raise ValueError("Selecione o PDF do balizamento.")

            nome_original = secure_filename(arquivo.filename)
            if not nome_original or not nome_original.lower().endswith(".pdf"):
                raise ValueError("O balizamento deve ser um arquivo PDF.")

            nome_final = f"evento_{evento_id}_{uuid.uuid4().hex[:10]}_{nome_original}"
            caminho = os.path.join(BALIZAMENTO_FOLDER, nome_final)
            arquivo.save(caminho)

            registros = _extrair_provas_balizamento(caminho)
            if not registros:
                try: os.remove(caminho)
                except Exception: pass
                raise ValueError("O PDF foi lido, mas nenhuma prova/série/atleta foi reconhecida.")

            # IMPORTANTE: a reimportação substitui SOMENTE o balizamento oficial.
            # As marcações fotográficas ficam em tblMarcacaoBalizamento e NÃO são apagadas.
            cur.execute("SELECT Count(*) FROM tblMarcacaoBalizamento WHERE IDEvento=? AND Ativa=True", [evento_id])
            marcacoes_preservadas = access_int(cur.fetchone()[0] or 0)

            # Substituição segura: apaga apenas o balizamento estruturado anterior do evento.
            cur.execute("DELETE FROM tblBalizamentoProvas WHERE IDEvento=?", [evento_id])
            for item in registros:
                cur.execute("""
                    INSERT INTO tblBalizamentoProvas
                    (IDEvento, NumeroProva, NomeProva, NumeroSerie, Raia, NomeAtleta, Registro, Pagina)
                    VALUES (?,?,?,?,?,?,?,?)
                """, [
                    evento_id, item["prova"], item["nome_prova"], item["serie"],
                    item["raia"], item["nome_atleta"], item["registro"], item["pagina"]
                ])

            antigo = evento[4] or ""
            antigo_caminho = ""
            try:
                cur.execute("SELECT BalizamentoCaminho FROM tblEvento WHERE IDEvento=?", [evento_id])
                rr = cur.fetchone()
                antigo_caminho = str(rr[0] or "") if rr else ""
            except Exception:
                pass

            cur.execute("""
                UPDATE tblEvento
                SET BalizamentoArquivo=?, BalizamentoCaminho=?, BalizamentoData=Now()
                WHERE IDEvento=?
            """, [nome_original, caminho, evento_id])
            conn.commit()

            if antigo_caminho and antigo_caminho != caminho and os.path.exists(antigo_caminho):
                try: os.remove(antigo_caminho)
                except Exception: pass

            mensagem_importacao = (
                f"Balizamento importado com sucesso: {len(registros)} atletas/provas reconhecidos."
            )
            if marcacoes_preservadas:
                mensagem_importacao += f" {marcacoes_preservadas} marcação(ões) fotográfica(s) preservada(s)."
            return redirect(url_for("gerenciar_balizamento_evento", evento_id=evento_id, ok=mensagem_importacao))

        cur.execute("SELECT Count(*) FROM tblBalizamentoProvas WHERE IDEvento=?", [evento_id])
        quantidade = access_int(cur.fetchone()[0] or 0)
        arquivo_atual = evento[4] or ""
        data_arquivo = evento[5].strftime("%d/%m/%Y %H:%M") if hasattr(evento[5], "strftime") else str(evento[5] or "")
        return render_template(
            "balizamento_evento.html",
            evento={
                "id": access_int(evento[0]),
                "nome": str(evento[1] or ""),
                "data": evento[2].strftime("%d/%m/%Y") if hasattr(evento[2], "strftime") else str(evento[2] or ""),
                "cidade": str(evento[3] or ""),
                "arquivo": str(arquivo_atual),
                "data_arquivo": data_arquivo,
                "quantidade": quantidade,
            },
            mensagem=request.args.get("ok", ""),
            erro=erro
        )
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        erro = str(e)
        cur = conn.cursor()
        try:
            cur.execute("SELECT IDEvento, NomeEvento, DataEvento, Cidade, BalizamentoArquivo, BalizamentoData FROM tblEvento WHERE IDEvento=?", [evento_id])
            evento = cur.fetchone()
        except Exception:
            evento = None
        if not evento:
            return "Evento não encontrado", 404
        return render_template("balizamento_evento.html", evento={
            "id": access_int(evento[0]), "nome": str(evento[1] or ""),
            "data": evento[2].strftime("%d/%m/%Y") if hasattr(evento[2], "strftime") else str(evento[2] or ""),
            "cidade": str(evento[3] or ""), "arquivo": str(evento[4] or ""),
            "data_arquivo": evento[5].strftime("%d/%m/%Y %H:%M") if hasattr(evento[5], "strftime") else str(evento[5] or ""),
            "quantidade": access_int(scalar(cur, "SELECT Count(*) FROM tblBalizamentoProvas WHERE IDEvento=?", 0, [evento_id]))
        }, mensagem="", erro=erro), 400
    finally:
        conn.close()


@app.route("/agendamentos/<int:agendamento_id>/balizamento")
def balizamento_agendamento(agendamento_id):
    """Mostra somente as provas do atleta encontradas no balizamento do evento."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT A.IDAgendamento, A.IDCliente, C.Nome AS Atleta, A.IDEvento,
                   E.NomeEvento, E.DataEvento, E.BalizamentoArquivo
            FROM ((tblAgendamentos AS A
            LEFT JOIN tblClientes AS C ON A.IDCliente=C.IDCliente)
            LEFT JOIN tblEvento AS E ON A.IDEvento=E.IDEvento)
            WHERE A.IDAgendamento=?
        """, [agendamento_id])
        r = cur.fetchone()
        if not r:
            return "Agendamento não encontrado", 404

        info = {
            "id": access_int(r[0]), "cliente_id": access_int(r[1]), "atleta": str(r[2] or ""),
            "evento_id": access_int(r[3]), "evento": str(r[4] or ""),
            "data_evento": r[5].strftime("%d/%m/%Y") if hasattr(r[5], "strftime") else str(r[5] or ""),
            "arquivo": str(r[6] or "")
        }
        if not info["arquivo"]:
            return redirect(url_for("web_agendamentos", erro="Este evento ainda não possui balizamento anexado."))

        cur.execute("""
            SELECT IDBalizamento, NumeroProva, NomeProva, NumeroSerie, Raia, NomeAtleta, Registro, Pagina
            FROM tblBalizamentoProvas
            WHERE IDEvento=? AND NomeAtleta LIKE ?
            ORDER BY NumeroProva, NumeroSerie, Raia
        """, [info["evento_id"], "%" + info["atleta"] + "%"])
        ocorrencias = []
        nome_norm = _normalizar_nome(info["atleta"])
        for row in cur.fetchall():
            if _normalizar_nome(row[5]) != nome_norm:
                continue
            ocorrencias.append({
                "id": access_int(row[0]), "prova": access_int(row[1]), "nome_prova": str(row[2] or ""),
                "serie": access_int(row[3]), "raia": access_int(row[4]), "nome_atleta": str(row[5] or ""),
                "registro": str(row[6] or ""), "pagina": access_int(row[7])
            })

        cur.execute("""
            SELECT IDMarcacao, NumeroProva, NumeroSerie, Raia, Cor, NomeProva
            FROM tblMarcacaoBalizamento
            WHERE IDEvento=? AND IDCliente=? AND Ativa=True
        """, [info["evento_id"], info["cliente_id"]])
        marcadas = {}
        for m in cur.fetchall():
            marcadas[access_int(m[1])] = {
                "id_marcacao": access_int(m[0]), "serie": access_int(m[2]), "raia": access_int(m[3]),
                "cor": str(m[4] or "amarelo"), "nome_prova": str(m[5] or "")
            }

        _ensure_balizamento_manual(conn)

        # Se o atleta não foi encontrado no balizamento oficial, primeiro abre
        # a tela separada para o operador DEFINIR as provas. Só depois da
        # confirmação essas provas aparecem na tela de marcação.
        cur.execute("""
            SELECT IDManual, NumeroProva, NomeProva, NumeroSerie, Raia
            FROM tblBalizamentoManualProvas
            WHERE IDEvento=? AND IDCliente=? AND IDAgendamento=? AND Ativa=True
            ORDER BY NumeroProva, NumeroSerie, Raia
        """, [info["evento_id"], info["cliente_id"], info["id"]])
        manuais = cur.fetchall()

        if not ocorrencias and not manuais:
            return render_template("manual_balizamento.html", info=info)

        provas_oficiais = {item["prova"] for item in ocorrencias}

        for item in ocorrencias:
            item["marcada"] = item["prova"] in marcadas and marcadas[item["prova"]]["serie"] == item["serie"]
            item["marcacao"] = marcadas.get(item["prova"], {})
            item["manual"] = False

        # Provas definidas manualmente entram na tela de marcação como
        # ocorrências normais. Elas só viram uma marcação quando o operador
        # clicar em MARCAR.
        for m in manuais:
            prova = access_int(m[1])
            if prova in provas_oficiais:
                continue
            serie = access_int(m[3])
            marcacao = marcadas.get(prova, {})
            marcada = prova in marcadas and access_int(marcadas[prova].get("serie") or 0) == serie
            ocorrencias.append({
                "id": 0,
                "prova": prova,
                "nome_prova": str(m[2] or "PROVA MANUAL"),
                "serie": serie,
                "raia": access_int(m[4] or 0),
                "nome_atleta": info["atleta"],
                "registro": "",
                "pagina": 0,
                "marcada": marcada,
                "marcacao": marcacao,
                "manual": True
            })

        ocorrencias.sort(key=lambda x: (access_int(x.get("prova")), access_int(x.get("serie")), access_int(x.get("raia"))))

        return render_template("balizamento.html", info=info, ocorrencias=ocorrencias, marcadas=marcadas)
    finally:
        conn.close()


@app.route("/eventos/<int:evento_id>/balizamento/imprimir")
def imprimir_balizamento_evento(evento_id):
    """Exibe o balizamento completo do evento com as marcações fotográficas destacadas."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT IDEvento, NomeEvento, DataEvento, Cidade, BalizamentoArquivo
            FROM tblEvento
            WHERE IDEvento=?
        """, [evento_id])
        ev = cur.fetchone()
        if not ev:
            return "Evento não encontrado", 404
        if not ev[4]:
            return redirect(url_for("web_agendamentos", erro="Este evento ainda não possui balizamento anexado."))

        cur.execute("""
            SELECT IDBalizamento, NumeroProva, NomeProva, NumeroSerie, Raia,
                   NomeAtleta, Registro, Pagina
            FROM tblBalizamentoProvas
            WHERE IDEvento=?
            ORDER BY NumeroProva, NumeroSerie, Raia, NomeAtleta
        """, [evento_id])
        rows = cur.fetchall()

        cur.execute("""
            SELECT M.IDMarcacao, M.IDCliente, C.Nome AS Atleta,
                   M.NumeroProva, M.NumeroSerie, M.Raia, M.Cor, M.NomeProva
            FROM tblMarcacaoBalizamento AS M
            LEFT JOIN tblClientes AS C ON M.IDCliente=C.IDCliente
            WHERE M.IDEvento=? AND M.Ativa=True
            ORDER BY M.NumeroProva, M.NumeroSerie, M.IDMarcacao
        """, [evento_id])
        marcacoes = cur.fetchall()

        # Chave por prova+série+raia+atleta, para destacar exatamente a linha marcada.
        marcadas = {}
        for m in marcacoes:
            nome = _normalizar_nome(m[2])
            chave = (access_int(m[3]), access_int(m[4]), access_int(m[5]), nome)
            marcadas[chave] = {
                "cor": str(m[6] or "amarelo"),
                "id": access_int(m[0]),
                "atleta": str(m[2] or ""),
            }

        balizamento = []
        for r in rows:
            prova = access_int(r[1])
            serie = access_int(r[3])
            raia = access_int(r[4])
            atleta = str(r[5] or "")
            marca = marcadas.get((prova, serie, raia, _normalizar_nome(atleta)))
            balizamento.append({
                "prova": prova,
                "nome_prova": str(r[2] or ""),
                "serie": serie,
                "raia": raia,
                "atleta": atleta,
                "registro": str(r[6] or ""),
                "pagina": access_int(r[7]),
                "marcada": bool(marca),
                "cor": marca["cor"] if marca else "",
            })

        # Inclui também marcações manuais que não existem no PDF oficial.
        try:
            _ensure_balizamento_manual(conn)
            cur.execute("""
                SELECT M.NumeroProva, M.NomeProva, M.NumeroSerie, M.Raia,
                       C.Nome AS Atleta
                FROM tblBalizamentoManualProvas AS M
                LEFT JOIN tblClientes AS C ON M.IDCliente=C.IDCliente
                WHERE M.IDEvento=? AND M.Ativa=True
                ORDER BY M.NumeroProva, M.NumeroSerie, M.Raia
            """, [evento_id])
            oficiais = {(x["prova"], x["serie"], x["raia"], _normalizar_nome(x["atleta"])) for x in balizamento}
            for m in cur.fetchall():
                chave = (access_int(m[0]), access_int(m[2]), access_int(m[3]), _normalizar_nome(m[4]))
                if chave not in oficiais:
                    marca = marcadas.get(chave, {})
                    balizamento.append({
                        "prova": access_int(m[0]),
                        "nome_prova": str(m[1] or ""),
                        "serie": access_int(m[2]),
                        "raia": access_int(m[3]),
                        "atleta": str(m[4] or ""),
                        "registro": "MANUAL",
                        "pagina": 0,
                        "marcada": bool(marca),
                        "cor": marca.get("cor", "amarelo") if marca else "amarelo",
                        "manual": True,
                    })
        except Exception:
            pass

        return render_template("balizamento_impressao.html", evento={
            "id": access_int(ev[0]),
            "nome": str(ev[1] or ""),
            "data": ev[2].strftime("%d/%m/%Y") if hasattr(ev[2], "strftime") else str(ev[2] or ""),
            "cidade": str(ev[3] or ""),
        }, balizamento=balizamento)
    finally:
        conn.close()


@app.route("/agendamentos", methods=["GET", "POST"])
def web_agendamentos():
    mensagem = ""
    erro = ""

    conn = get_connection()
    try:
        cur = conn.cursor()

        # ==========================
        # NOVO AGENDAMENTO
        # ==========================
        if request.method == "POST":
            id_cliente = access_int(request.form.get("id_cliente") or 0)
            id_evento = access_int(request.form.get("id_evento") or 0)
            # O status de um novo agendamento é sempre AGENDADO.
            cur.execute("SELECT TOP 1 IDStatusAgendamento FROM tblStatusAgendamento WHERE UCASE(StatusAgendamento)='AGENDADO'")
            status_row = cur.fetchone()
            if not status_row:
                raise ValueError("O status AGENDADO não foi encontrado no SGFE.")
            id_status = access_int(status_row[0])
            observacoes = request.form.get("observacoes") or ""

            if not id_cliente:
                raise ValueError("Selecione um atleta.")
            if not id_evento:
                raise ValueError("Selecione um evento.")
            if not id_status:
                raise ValueError("Selecione o status do agendamento.")

            # Regra fundamental: o mesmo atleta não pode ser agendado
            # duas vezes para o mesmo evento enquanto o agendamento anterior
            # não estiver cancelado.
            cur.execute("""
                SELECT A.IDAgendamento, S.StatusAgendamento
                FROM tblAgendamentos AS A
                LEFT JOIN tblStatusAgendamento AS S
                  ON A.IDStatusAgendamento=S.IDStatusAgendamento
                WHERE A.IDCliente=? AND A.IDEvento=?
                ORDER BY A.IDAgendamento DESC
            """, [id_cliente, id_evento])

            existentes = cur.fetchall()
            for ex in existentes:
                status_antigo = str(ex[1] or "").upper()
                if "CANCEL" not in status_antigo:
                    raise ValueError(
                        f"Este atleta já possui o agendamento #{ex[0]} "
                        "para este evento."
                    )

            cur.execute("""
                INSERT INTO tblAgendamentos
                (DataAgendamento, IDCliente, IDEvento,
                 IDStatusAgendamento, Observacoes, UltimaAtualizacao)
                VALUES (Now(),?,?,?,?,Now())
            """, [
                id_cliente, id_evento, id_status, observacoes
            ])

            conn.commit()
            mensagem = "Agendamento realizado com sucesso."

        # ==========================
        # FILTROS
        # ==========================
        busca = request.args.get("q", "").strip()
        filtro_evento = request.args.get("evento", "").strip()
        filtro_status = request.args.get("status", "").strip()

        sql = """
        SELECT
            A.IDAgendamento,
            A.DataAgendamento,
            C.IDCliente,
            C.Nome AS Atleta,
            C.Telefone AS Telefone,
            C.Contato AS Contato,
            X.Sexo AS Sexo,
            E.IDEvento,
            E.NomeEvento AS Evento,
            E.DataEvento,
            E.Cidade,
            E.BalizamentoArquivo,
            S.IDStatusAgendamento,
            S.StatusAgendamento AS Status,
            A.Observacoes,
            A.IDVendas
        FROM ((((tblAgendamentos AS A
        LEFT JOIN tblClientes AS C ON A.IDCliente=C.IDCliente)
        LEFT JOIN tblEvento AS E ON A.IDEvento=E.IDEvento)
        LEFT JOIN tblStatusAgendamento AS S
          ON A.IDStatusAgendamento=S.IDStatusAgendamento)
        LEFT JOIN tblSexos AS X ON C.IDSexo=X.IDSexo)
        WHERE (A.IDVendas Is Null OR A.IDVendas=0)
          AND NOT EXISTS (
              SELECT V.IDVenda
              FROM tblVendaPacotes AS V
              WHERE V.IDAgendamento=A.IDAgendamento
          )
          AND (S.StatusAgendamento Is Null OR UCase(S.StatusAgendamento) NOT LIKE '%CANCEL%')
        """
        params = []

        if busca:
            sql += """
            AND (
                C.Nome LIKE ?
                OR C.Telefone LIKE ?
                OR C.Contato LIKE ?
                OR E.NomeEvento LIKE ?
            )
            """
            like = "%" + busca + "%"
            params.extend([like, like, like, like])

        if filtro_evento:
            sql += " AND A.IDEvento=?"
            params.append(access_int(filtro_evento))

        if filtro_status:
            sql += " AND A.IDStatusAgendamento=?"
            params.append(access_int(filtro_status))

        # REGRA FUNDAMENTAL DO FLUXO:
        # se o agendamento já gerou uma venda/OS, ele NÃO aparece mais
        # nesta lista. O histórico continua preservado na venda e no atleta.
        #
        # Preferência: quem agendou primeiro aparece primeiro.
        sql += " ORDER BY A.DataAgendamento ASC, A.IDAgendamento ASC"

        cur.execute(sql, params)
        columns = [d[0] for d in cur.description]
        data = []

        for row in cur.fetchall():
            item = {}
            for col, value in zip(columns, row):
                if hasattr(value, "strftime"):
                    value = value.strftime("%d/%m/%Y")
                elif value is None:
                    value = ""
                else:
                    value = str(value)
                item[col] = value
            data.append(item)

        # ==========================
        # COMBO ATLETAS
        # ==========================
        cur.execute("""
            SELECT C.IDCliente, C.Nome, E.NomeEquipe, X.Sexo
            FROM ((tblClientes AS C
            LEFT JOIN tblEquipes AS E ON C.IDEquipe=E.IDEquipe)
            LEFT JOIN tblSexos AS X ON C.IDSexo=X.IDSexo)
            ORDER BY C.Nome
        """)
        atletas = [
            {
                "id": access_int(r[0]),
                "nome": r[1] or "",
                "equipe": r[2] or "", "sexo": r[3] or ""
            }
            for r in cur.fetchall()
        ]

        # ==========================
        # COMBO EVENTOS
        # qryComboEventos original
        # ==========================
        cur.execute("""
            SELECT IDEvento, NomeEvento, DataEvento, Cidade
            FROM tblEvento
            ORDER BY DataEvento ASC, NomeEvento
        """)
        eventos = []
        for r in cur.fetchall():
            eventos.append({
                "id": access_int(r[0]),
                "nome": r[1] or "",
                "data": r[2].strftime("%d/%m/%Y")
                    if hasattr(r[2], "strftime") else str(r[2] or ""),
                "cidade": r[3] or ""
            })

        # ==========================
        # COMBO STATUS
        # qryComboStatusAgendamento original
        # ==========================
        cur.execute("""
            SELECT IDStatusAgendamento, StatusAgendamento
            FROM tblStatusAgendamento
            ORDER BY IDStatusAgendamento
        """)
        status = [
            {"id": access_int(r[0]), "nome": r[1] or ""}
            for r in cur.fetchall()
        ]

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        erro = str(e)

        busca = request.args.get("q", "").strip()
        filtro_evento = request.args.get("evento", "").strip()
        filtro_status = request.args.get("status", "").strip()
        data = locals().get("data", [])
        atletas = locals().get("atletas", [])
        eventos = locals().get("eventos", [])
        status = locals().get("status", [])
        columns = locals().get("columns", [])

    finally:
        conn.close()

    # ==========================
    # DADOS DO CADASTRO COMPLETO DO ATLETA
    # ==========================
    conn_cad = get_connection()
    try:
        cur_cad = conn_cad.cursor()

        cur_cad.execute("SELECT IDEquipe, NomeEquipe, Cidade, Estado FROM tblEquipes ORDER BY NomeEquipe")
        equipes_cadastro = [
            {
                "id": access_int(r[0]),
                "nome": r[1] or "",
                "cidade": r[2] or "",
                "estado": r[3] or ""
            }
            for r in cur_cad.fetchall()
        ]

        cur_cad.execute("SELECT IDSexo, Sexo FROM tblSexos ORDER BY Sexo")
        sexos_cadastro = [{"id": access_int(r[0]), "nome": r[1] or ""} for r in cur_cad.fetchall()]

        cur_cad.execute("SELECT IDModalidade, Modalidade FROM tblModalidades ORDER BY Modalidade")
        modalidades_cadastro = [{"id": access_int(r[0]), "nome": r[1] or ""} for r in cur_cad.fetchall()]
    finally:
        conn_cad.close()

    return render_template(
        "agendamentos.html",
        page="agendamentos",
        title="Agendamentos",
        subtitle="Agenda completa do SGFE",
        columns=columns,
        data=data,
        atletas=atletas,
        eventos=eventos,
        status=status,
        busca=busca,
        filtro_evento=filtro_evento,
        filtro_status=filtro_status,
        equipes_cadastro=equipes_cadastro,
        sexos_cadastro=sexos_cadastro,
        modalidades_cadastro=modalidades_cadastro,
        mensagem=mensagem,
        erro=erro
    )


@app.route("/agendamentos/nova-equipe", methods=["POST"])
def agendamento_nova_equipe():
    """Cadastro rápido de equipe durante o cadastro do atleta."""
    nome = request.form.get("nome", "").strip()
    if not nome:
        return jsonify({"ok": False, "erro": "Informe o nome da equipe."}), 400

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT TOP 1 IDEquipe, NomeEquipe, Cidade, Estado FROM tblEquipes WHERE NomeEquipe=?", [nome])
        existente = cur.fetchone()
        if existente:
            return jsonify({
                "ok": True,
                "id": access_int(existente[0]),
                "nome": existente[1] or nome,
                "cidade": existente[2] or "",
                "estado": existente[3] or "",
                "existente": True
            })

        cur.execute("INSERT INTO tblEquipes (NomeEquipe) VALUES (?)", [nome])
        cur.execute("SELECT @@IDENTITY")
        equipe_id = access_int(cur.fetchone()[0])
        conn.commit()
        return jsonify({
            "ok": True,
            "id": equipe_id,
            "nome": nome,
            "cidade": "",
            "estado": "",
            "existente": False
        })
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "erro": str(e)}), 500
    finally:
        conn.close()


@app.route("/agendamentos/novo-atleta", methods=["POST"])
def agendamento_novo_atleta():
    nome = request.form.get("nome", "").strip()
    telefone = request.form.get("telefone", "").strip()
    contato = request.form.get("contato", "").strip()
    equipe_id = request.form.get("equipe_id", "").strip()
    sexo_id = request.form.get("sexo_id", "").strip()
    nascimento = request.form.get("ano_nascimento", "").strip()
    modalidade_id = request.form.get("modalidade_id", "").strip()
    cidade = request.form.get("cidade", "").strip()
    estado = request.form.get("estado", "").strip()
    email = request.form.get("email", "").strip()
    instagram = request.form.get("instagram", "").strip()
    observacoes = request.form.get("observacoes", "").strip()

    if not nome:
        return jsonify({"ok": False, "erro": "Informe o nome do atleta."}), 400

    conn = get_connection()
    try:
        cur = conn.cursor()

        # Mesmo critério do cadastro do SGFE: evita duplicidade.
        if telefone:
            cur.execute("""
                SELECT TOP 1 IDCliente, Nome
                FROM tblClientes
                WHERE Nome=? AND Telefone=?
            """, [nome, telefone])
        else:
            cur.execute("""
                SELECT TOP 1 IDCliente, Nome
                FROM tblClientes
                WHERE Nome=?
            """, [nome])

        existente = cur.fetchone()
        if existente:
            return jsonify({
                "ok": False,
                "erro": "Este atleta já está cadastrado. Consulte e selecione o cadastro existente.",
                "id_cliente": access_int(existente[0]),
                "nome": existente[1] or nome
            }), 409

        # Cadastro completo do atleta/cliente, usando os mesmos campos
        # disponíveis no cadastro do SGFE.
        campos = ["Nome"]
        valores = [nome]
        placeholders = ["?"]

        def add_num(campo, valor):
            if valor:
                campos.append(campo)
                valores.append(access_int(valor))
                placeholders.append("?")

        def add_txt(campo, valor):
            if valor:
                campos.append(campo)
                valores.append(valor)
                placeholders.append("?")

        add_num("IDSexo", sexo_id)
        if nascimento:
            campos.append("AnoNascimento")
            valores.append(int(nascimento))
            placeholders.append("?")
        add_num("IDEquipe", equipe_id)
        add_num("IDModalidade", modalidade_id)
        add_txt("Telefone", telefone)
        add_txt("Contato", contato)
        add_txt("Cidade", cidade)
        add_txt("Estado", estado)
        add_txt("Email", email)
        add_txt("Instagram", instagram)
        add_txt("Observacoes", observacoes)

        cur.execute(
            f"INSERT INTO tblClientes ({','.join(campos)}) "
            f"VALUES ({','.join(placeholders)})",
            valores
        )

        cur.execute("SELECT @@IDENTITY")
        id_cliente = access_int(cur.fetchone()[0])
        conn.commit()

        return jsonify({
            "ok": True,
            "id_cliente": id_cliente,
            "nome": nome,
            "telefone": telefone,
            "contato": contato
        })

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "erro": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/balizamento/atleta/<int:agendamento_id>")
def api_balizamento_atleta(agendamento_id):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT A.IDCliente, A.IDEvento, C.Nome, E.NomeEvento, E.BalizamentoArquivo
            FROM ((tblAgendamentos AS A
            LEFT JOIN tblClientes AS C ON A.IDCliente=C.IDCliente)
            LEFT JOIN tblEvento AS E ON A.IDEvento=E.IDEvento)
            WHERE A.IDAgendamento=?
        """, [agendamento_id])
        ag = cur.fetchone()
        if not ag:
            return jsonify({"ok": False, "erro": "Agendamento não encontrado."}), 404
        cliente_id, evento_id, nome, evento, arquivo = access_int(ag[0]), access_int(ag[1]), str(ag[2] or ""), str(ag[3] or ""), str(ag[4] or "")
        cur.execute("""
            SELECT NumeroProva, NomeProva, NumeroSerie, Raia, NomeAtleta, Registro, Pagina
            FROM tblBalizamentoProvas
            WHERE IDEvento=? AND NomeAtleta LIKE ?
            ORDER BY NumeroProva, NumeroSerie, Raia
        """, [evento_id, "%" + nome + "%"])
        result=[]
        alvo=_normalizar_nome(nome)
        for r in cur.fetchall():
            if _normalizar_nome(r[4]) != alvo: continue
            result.append({"prova":access_int(r[0]),"nome_prova":str(r[1] or ""),"serie":access_int(r[2]),"raia":access_int(r[3]),"registro":str(r[5] or ""),"pagina":access_int(r[6])})
        return jsonify({"ok": True, "arquivo": arquivo, "atleta": nome, "evento": evento, "ocorrencias": result})
    finally:
        conn.close()


@app.route("/api/agendamentos/<int:cliente_id>/<int:evento_id>")
def api_agendamentos_cliente_evento(cliente_id, evento_id):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
        SELECT A.IDAgendamento, A.DataAgendamento,
               S.StatusAgendamento AS Status
        FROM tblAgendamentos AS A
        LEFT JOIN tblStatusAgendamento AS S
          ON A.IDStatusAgendamento=S.IDStatusAgendamento
        WHERE A.IDCliente=? AND A.IDEvento=?
        ORDER BY A.IDAgendamento DESC
        """, [cliente_id, evento_id])
        result = []
        for r in cur.fetchall():
            result.append({
                "id": r[0],
                "data": r[1].strftime("%d/%m/%Y") if hasattr(r[1], "strftime") else str(r[1] or ""),
                "status": "" if r[2] is None else str(r[2])
            })
        return jsonify(result)
    finally:
        conn.close()


@app.route("/agendamentos/<int:agendamento_id>/venda")
def venda_a_partir_do_agendamento(agendamento_id):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
        SELECT A.IDAgendamento, A.IDCliente, C.Nome AS Cliente,
               A.IDEvento, E.NomeEvento AS Evento,
               E.DataEvento, A.DataAgendamento,
               S.StatusAgendamento AS Status
        FROM (((tblAgendamentos AS A
        LEFT JOIN tblClientes AS C ON A.IDCliente=C.IDCliente)
        LEFT JOIN tblEvento AS E ON A.IDEvento=E.IDEvento)
        LEFT JOIN tblStatusAgendamento AS S
          ON A.IDStatusAgendamento=S.IDStatusAgendamento)
        WHERE A.IDAgendamento=?
        """, [agendamento_id])
        r = cur.fetchone()
        if not r:
            return "Agendamento não encontrado", 404

        info = {
            "id": r[0], "cliente_id": r[1], "cliente": r[2] or "",
            "evento_id": r[3], "evento": r[4] or "",
            "data_evento": r[5].strftime("%d/%m/%Y") if hasattr(r[5], "strftime") else str(r[5] or ""),
            "data_agendamento": r[6].strftime("%d/%m/%Y") if hasattr(r[6], "strftime") else str(r[6] or ""),
            "status": r[7] or "",
            "qtd_preselecionada": access_int(request.args.get("qtd_provas") or 0)
        }

        # Busca os preços do evento sem depender do tipo que o ODBC Access
        # atribui ao parâmetro IDEvento. A comparação do ID é normalizada
        # em Python para evitar o combo vazio.
        cur.execute("""
            SELECT IdEvento, QtdProvas, Valor
            FROM tblPrecoEvento
            ORDER BY IdEvento, QtdProvas
        """)
        precos = []
        evento_id = access_int(info["evento_id"])

        for pr in cur.fetchall():
            try:
                preco_evento_id = access_int(pr[0])
                qtd_provas = access_int(pr[1])
            except Exception:
                continue

            if preco_evento_id == evento_id:
                precos.append({
                    "qtd": qtd_provas,
                    "valor": float(pr[2] or 0)
                })

        precos = sorted(
            {p["qtd"]: p for p in precos}.values(),
            key=lambda x: x["qtd"]
        )

        cur.execute("SELECT IDStatusVenda,StatusVenda FROM tblStatusVenda ORDER BY IDStatusVenda")
        status = [{"id": access_int(r[0]), "nome": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()

    erro_precos = ""
    if not precos:
        erro_precos = (
            "Nenhum pacote de provas foi encontrado para este evento. "
            f"ID do evento: {info['evento_id']}. "
            "Verifique os preços cadastrados em tblPrecoEvento."
        )

    return render_template("nova_venda_agendamento.html",
        info=info, precos=precos, status=status,
        erro_precos=erro_precos)


@app.route("/vendas/nova", methods=["GET","POST"])
def nova_venda():
    conn = get_connection()
    mensagem = ""
    erro = ""
    form = {}
    if request.method == "GET":
        if request.args.get("id_agendamento"):
            form["id_agendamento"] = request.args.get("id_agendamento")
        if request.args.get("qtd_provas"):
            form["qtd_provas"] = request.args.get("qtd_provas")
    try:
        cur = conn.cursor()

        if request.method == "POST":
            form = dict(request.form)
            id_agendamento = access_int(request.form.get("id_agendamento") or 0)
            qtd = access_int(request.form.get("qtd_provas") or 0)
            desconto = float((request.form.get("desconto") or "0").replace(",", "."))
            id_status = access_int(request.form.get("id_status") or 0)
            obs = request.form.get("observacoes") or ""

            if not id_agendamento or not qtd or not id_status:
                raise ValueError("Selecione o agendamento, a quantidade de provas e o status da venda.")

            # O status desta tela é STATUS DA VENDA (tblStatusVenda).
            cur.execute("""
                SELECT IDStatusVenda
                FROM tblStatusVenda
            """)
            status_ids = {access_int(r[0]) for r in cur.fetchall()}
            if id_status not in status_ids:
                raise ValueError("O status selecionado não existe em tblStatusVenda.")

            cur.execute("""
                SELECT StatusVenda
                FROM tblStatusVenda
                WHERE IDStatusVenda=?
            """, [id_status])
            status_nome = str((cur.fetchone() or [""])[0] or "").strip().upper()

            # The appointment is the source of truth for the client and event.
            # O agendamento é a fonte oficial do cliente e do evento.
            # Lemos a tabela e normalizamos os IDs para evitar problemas
            # de parâmetro/tipo do ODBC Microsoft Access.
            cur.execute("""
                SELECT IDAgendamento, IDCliente, IDEvento
                FROM tblAgendamentos
            """)
            ag = None
            for row in cur.fetchall():
                try:
                    if access_int(row[0]) == id_agendamento:
                        ag = row
                        break
                except Exception:
                    continue

            if not ag:
                raise ValueError("Agendamento não encontrado.")

            id_cliente = access_int(ag[1])
            id_evento = access_int(ag[2])

            cur.execute("""
                SELECT IDEvento, QtdProvas, Valor
                FROM tblPrecoEvento
                ORDER BY IDEvento, QtdProvas
            """)
            preco = None
            for row in cur.fetchall():
                try:
                    if access_int(row[0]) == id_evento and access_int(row[1]) == qtd:
                        preco = row
                        break
                except Exception:
                    continue

            if not preco:
                raise ValueError(
                    "Não existe preço cadastrado para essa quantidade de provas neste evento."
                )

            valor_pacote = float(preco[2] or 0)
            if desconto < 0:
                raise ValueError("O desconto não pode ser negativo.")
            if desconto > valor_pacote:
                raise ValueError("O desconto não pode ser maior que o valor do pacote.")

            valor_final = valor_pacote - desconto

            # Prevent an accidental second sale for the same appointment.
            cur.execute("""
                SELECT IDAgendamento
                FROM tblVendaPacotes
            """)
            for row in cur.fetchall():
                try:
                    if access_int(row[0]) == id_agendamento:
                        raise ValueError("Este agendamento já possui uma venda vinculada.")
                except (TypeError, ValueError):
                    if isinstance(row[0], (int, float)) and int(row[0]) == id_agendamento:
                        raise ValueError("Este agendamento já possui uma venda vinculada.")

            cur.execute("""
            INSERT INTO tblVendaPacotes
            (DataVenda,IDCliente,IDEvento,QtdProvas,ValorPacote,
             ValorDesconto,ValorFinal,IDStatusVenda,Observacoes,
             IDAgendamento,Finalizado,DataFinalizacao)
            VALUES (Now(),?,?,?,?,?,?,?,?,?,False,Null)
            """, [id_cliente, id_evento, qtd, valor_pacote, desconto,
                  valor_final, id_status, obs, id_agendamento])

            # O vínculo é fundamental para o fluxo:
            # AGENDAMENTO -> VENDA/OS.
            # Depois que a venda nasce, o agendamento deixa de aparecer
            # na lista de agendamentos ativos.
            id_venda = access_int(cur.execute(
                "SELECT @@IDENTITY"
            ).fetchone()[0])

            cur.execute("""
                UPDATE tblAgendamentos
                SET IDVendas=?
                WHERE IDAgendamento=?
            """, [id_venda, id_agendamento])

            if status_nome == "CANCELADO":
                _cancelar_venda_operacional(cur, id_venda)

            conn.commit()

            mensagem = (
                f"Venda criada com sucesso. OS: {id_venda} "
                f"• Valor final: R$ {valor_final:,.2f}"
            ).replace(",", "X").replace(".", ",").replace("X", ".")
            if status_nome == "CANCELADO":
                mensagem = (
                    f"OS {id_venda} criada e cancelada. "
                    "Pagamento não registrado e balizamento desmarcado."
                )

            # Fluxo operacional: depois de salvar a venda originada do
            # balizamento/agendamento, volta diretamente para Agendamentos.
            return redirect(url_for("web_agendamentos", ok=mensagem))

        cur.execute("SELECT IDCliente,Nome FROM tblClientes ORDER BY Nome")
        clientes = [{"id": access_int(r[0]), "nome": r[1]} for r in cur.fetchall()]

        cur.execute("""
        SELECT IDEvento,NomeEvento,DataEvento,Cidade
        FROM tblEvento ORDER BY DataEvento DESC
        """)
        eventos = []
        for r in cur.fetchall():
            eventos.append({
                "id": access_int(r[0]), "nome": r[1],
                "data": r[2].strftime("%d/%m/%Y") if hasattr(r[2], "strftime") else str(r[2] or ""),
                "cidade": r[3] or ""
            })

        cur.execute("SELECT IDStatusVenda,StatusVenda FROM tblStatusVenda ORDER BY IDStatusVenda")
        status = [{"id": access_int(r[0]), "nome": r[1]} for r in cur.fetchall()]

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        erro = str(e)
        clientes = locals().get("clientes", [])
        eventos = locals().get("eventos", [])
        status = locals().get("status", [])
    finally:
        conn.close()

    return render_template("nova_venda.html",
        clientes=clientes, eventos=eventos, status=status,
        mensagem=mensagem, erro=erro, form=form)


@app.route("/api/precos/<int:evento_id>")
def api_precos(evento_id):
    conn=get_connection()
    try:
        cur=conn.cursor()
        cur.execute("""
        SELECT QtdProvas,Valor FROM tblPrecoEvento
        WHERE IdEvento=? ORDER BY QtdProvas
        """,[evento_id])
        return jsonify([{"qtd":r[0],"valor":float(r[1] or 0)} for r in cur.fetchall()])
    finally:
        conn.close()


@app.route("/vendas")
def web_vendas():
    """Lista de vendas/pacotes com filtro por evento.

    Ao abrir a tela sem filtro, o evento mais recente por DataEvento é
    selecionado automaticamente. O usuário pode trocar o evento e a
    escolha permanece no filtro através do parâmetro ?evento=.
    """
    search = request.args.get("q", "").strip()
    evento_param = request.args.get("evento", "").strip()
    mensagem = request.args.get("ok", "")
    erro = request.args.get("erro", "")

    conn = get_connection()
    eventos = []
    evento_selecionado = 0

    try:
        cur = conn.cursor()

        # Eventos em ordem do mais recente para o mais antigo.
        cur.execute("""
            SELECT IDEvento, NomeEvento, DataEvento, Cidade, Ativo
            FROM tblEvento
            ORDER BY DataEvento DESC, IDEvento DESC
        """)
        for r in cur.fetchall():
            eventos.append({
                "id": access_int(r[0]),
                "nome": str(r[1] or ""),
                "data": r[2].strftime("%d/%m/%Y") if hasattr(r[2], "strftime") else str(r[2] or ""),
                "cidade": str(r[3] or ""),
                "ativo": bool(r[4]) if r[4] is not None else False,
            })

        # Sem escolha do usuário, sempre começa no último evento.
        if evento_param:
            try:
                candidato = access_int(evento_param)
            except Exception:
                candidato = 0
            if any(e["id"] == candidato for e in eventos):
                evento_selecionado = candidato
        elif eventos:
            evento_selecionado = eventos[0]["id"]

        sql = """
        SELECT
            V.IDVenda,
            V.DataVenda,
            C.Nome AS Atleta,
            E.NomeEvento AS Evento,
            V.QtdProvas,
            V.ValorPacote,
            V.ValorDesconto,
            V.ValorFinal,
            S.StatusVenda AS Status,
            V.IDAgendamento,
            V.Finalizado,
            V.DataFinalizacao
        FROM (((tblVendaPacotes AS V
        LEFT JOIN tblClientes AS C ON V.IDCliente=C.IDCliente)
        LEFT JOIN tblEvento AS E ON V.IDEvento=E.IDEvento)
        LEFT JOIN tblStatusVenda AS S ON V.IDStatusVenda=S.IDStatusVenda)
        """

        where = []
        params = []

        if evento_selecionado:
            where.append("V.IDEvento=?")
            params.append(evento_selecionado)

        if search:
            where.append("""
                (C.Nome LIKE ?
                 OR E.NomeEvento LIKE ?
                 OR S.StatusVenda LIKE ?)
            """)
            like = "%" + search + "%"
            params.extend([like, like, like])

        if where:
            sql += " WHERE " + " AND ".join(where)

        sql += " ORDER BY V.IDVenda DESC"

        cur.execute(sql, params)
        columns = [d[0] for d in cur.description]
        data = []
        for row in cur.fetchall():
            item = {}
            for col, value in zip(columns, row):
                if hasattr(value, "strftime"):
                    value = value.strftime("%d/%m/%Y")
                elif value is None:
                    value = ""
                else:
                    value = str(value)
                item[col] = value
            data.append(item)

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        erro = str(e)
        columns = locals().get("columns", [])
        data = locals().get("data", [])
    finally:
        conn.close()

    return render_template(
        "vendas.html",
        page="vendas",
        title="Vendas / Pacotes",
        subtitle="Vendas e pacotes registrados no SGFE",
        columns=columns,
        data=data,
        search=search,
        search_label="Pesquisar venda...",
        mensagem=mensagem,
        erro=erro,
        eventos=eventos,
        evento_selecionado=evento_selecionado
    )


@app.route("/vendas/<int:id_venda>/editar", methods=["GET", "POST"])
def editar_venda(id_venda):
    conn = get_connection()
    erro = ""
    mensagem = ""
    try:
        cur = conn.cursor()

        # Carrega a venda sem depender de JOINs para localizar o registro.
        cur.execute("""
            SELECT IDVenda, IDCliente, IDEvento, QtdProvas, ValorPacote,
                   ValorDesconto, ValorFinal, IDStatusVenda,
                   Observacoes, IDAgendamento, Finalizado
            FROM tblVendaPacotes
            ORDER BY IDVenda
        """)
        venda = None
        for row in cur.fetchall():
            try:
                if access_int(row[0]) == id_venda:
                    venda = row
                    break
            except Exception:
                continue

        if not venda:
            return redirect(url_for("web_vendas", erro="Venda não encontrada."))

        # Se já existe pagamento, não alteramos o valor da venda por baixo
        # do registro financeiro.
        cur.execute("SELECT IDVenda FROM tblPagamento")
        possui_pagamento = False
        for row in cur.fetchall():
            try:
                if access_int(row[0]) == id_venda:
                    possui_pagamento = True
                    break
            except Exception:
                continue

        if request.method == "POST":
            if possui_pagamento:
                raise ValueError(
                    "Esta venda já possui pagamento registrado. "
                    "Não é seguro alterar o pacote ou o valor."
                )

            qtd = access_int(request.form.get("qtd_provas") or 0)
            desconto = float(
                (request.form.get("desconto") or "0").replace(",", ".")
            )
            id_status = access_int(request.form.get("id_status") or 0)
            obs = request.form.get("observacoes") or ""

            if not qtd:
                raise ValueError("Selecione a quantidade de provas.")
            if not id_status:
                raise ValueError("Selecione o status da venda.")
            if desconto < 0:
                raise ValueError("O desconto não pode ser negativo.")

            id_evento = access_int(venda[2])

            cur.execute("""
                SELECT IDEvento, QtdProvas, Valor
                FROM tblPrecoEvento
                ORDER BY IDEvento, QtdProvas
            """)
            preco = None
            for row in cur.fetchall():
                try:
                    if access_int(row[0]) == id_evento and access_int(row[1]) == qtd:
                        preco = row
                        break
                except Exception:
                    continue

            if not preco:
                raise ValueError(
                    "Não existe preço cadastrado para essa quantidade "
                    "de provas neste evento."
                )

            valor_pacote = float(preco[2] or 0)
            if desconto > valor_pacote:
                raise ValueError(
                    "O desconto não pode ser maior que o valor do pacote."
                )

            valor_final = valor_pacote - desconto

            cur.execute("""
                UPDATE tblVendaPacotes
                SET QtdProvas=?,
                    ValorPacote=?,
                    ValorDesconto=?,
                    ValorFinal=?,
                    IDStatusVenda=?,
                    Observacoes=?
                WHERE IDVenda=?
            """, [
                qtd, valor_pacote, desconto, valor_final,
                id_status, obs, id_venda
            ])
            conn.commit()

            mensagem = "Venda/OS atualizada com sucesso."

            # Recarrega a venda para mostrar os valores atualizados.
            cur.execute("""
                SELECT IDVenda, IDCliente, IDEvento, QtdProvas, ValorPacote,
                       ValorDesconto, ValorFinal, IDStatusVenda,
                       Observacoes, IDAgendamento, Finalizado
                FROM tblVendaPacotes
                ORDER BY IDVenda
            """)
            for row in cur.fetchall():
                try:
                    if access_int(row[0]) == id_venda:
                        venda = row
                        break
                except Exception:
                    continue

        # Preços do evento.
        id_evento = access_int(venda[2])
        cur.execute("""
            SELECT IDEvento, QtdProvas, Valor
            FROM tblPrecoEvento
            ORDER BY IDEvento, QtdProvas
        """)
        precos = []
        for row in cur.fetchall():
            try:
                if access_int(row[0]) == id_evento:
                    precos.append({
                        "qtd": access_int(row[1]),
                        "valor": float(row[2] or 0)
                    })
            except Exception:
                continue

        precos = sorted(
            {p["qtd"]: p for p in precos}.values(),
            key=lambda x: x["qtd"]
        )

        cur.execute(
            "SELECT IDStatusVenda, StatusVenda "
            "FROM tblStatusVenda ORDER BY IDStatusVenda"
        )
        status = [
            {"id": access_int(r[0]), "nome": str(r[1] or "")}
            for r in cur.fetchall()
        ]

        cur.execute(
            "SELECT C.Nome, E.NomeEvento "
            "FROM tblClientes AS C, tblEvento AS E "
            "WHERE C.IDCliente=? AND E.IDEvento=?",
            [access_int(venda[1]), id_evento]
        )
        info = cur.fetchone()
        nome_atleta = str(info[0] if info else "")
        nome_evento = str(info[1] if info else "")

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        erro = str(e)
        # Preserve the form with the current values if an update fails.
        precos = locals().get("precos", [])
        status = locals().get("status", [])
        venda = locals().get("venda", None)
        nome_atleta = locals().get("nome_atleta", "")
        nome_evento = locals().get("nome_evento", "")
    finally:
        conn.close()

    if venda is None:
        return redirect(url_for("web_vendas", erro="Venda não encontrada."))

    return render_template(
        "editar_venda.html",
        venda=venda,
        precos=precos,
        status=status,
        mensagem=mensagem,
        erro=erro,
        nome_atleta=nome_atleta,
        nome_evento=nome_evento,
        possui_pagamento=possui_pagamento
    )


@app.post("/vendas/<int:id_venda>/excluir")
def excluir_venda(id_venda):
    conn = get_connection()
    try:
        cur = conn.cursor()

        # Localiza a venda e o agendamento vinculado.
        cur.execute("""
            SELECT IDVenda, IDAgendamento
            FROM tblVendaPacotes
            ORDER BY IDVenda
        """)
        venda = None
        for row in cur.fetchall():
            try:
                if access_int(row[0]) == id_venda:
                    venda = row
                    break
            except Exception:
                continue

        if not venda:
            return redirect(url_for("web_vendas", erro="Venda não encontrada."))

        # Nunca apagamos uma venda que já tenha pagamento.
        cur.execute("SELECT IDVenda FROM tblPagamento")
        for row in cur.fetchall():
            try:
                if access_int(row[0]) == id_venda:
                    return redirect(url_for(
                        "web_vendas",
                        erro="Esta venda possui pagamento registrado e não pode ser excluída."
                    ))
            except Exception:
                continue

        id_agendamento = venda[1]

        # Remove o vínculo da venda do agendamento para que ele volte
        # à lista de agendamentos ativos e possa ser vendido novamente.
        if id_agendamento not in (None, "", b"", bytearray()):
            try:
                ag_num = access_int(id_agendamento)
            except Exception:
                ag_num = 0
            if ag_num:
                cur.execute("""
                    UPDATE tblAgendamentos
                    SET IDVendas=Null
                    WHERE IDAgendamento=?
                """, [ag_num])

        cur.execute(
            "DELETE FROM tblVendaPacotes WHERE IDVenda=?",
            [id_venda]
        )
        conn.commit()

        return redirect(url_for(
            "web_vendas",
            ok="Venda/OS excluída com sucesso. O agendamento voltou para a lista."
        ))
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return redirect(url_for("web_vendas", erro=str(e)))
    finally:
        conn.close()


# ==========================
# CONFIGURAÇÕES
# ==========================
@app.route("/configuracoes")
def configuracoes():
    conn = get_connection()
    dados = {"eventos": 0, "modalidades": 0, "equipes": 0, "despesas": 0, "total_despesas": 0.0}
    erro = ""
    try:
        cur = conn.cursor()
        dados["eventos"] = access_int(scalar(cur, "SELECT Count(*) FROM tblEvento", 0))
        dados["modalidades"] = access_int(scalar(cur, "SELECT Count(*) FROM tblModalidades", 0))
        dados["equipes"] = access_int(scalar(cur, "SELECT Count(*) FROM tblEquipes", 0))
        dados["despesas"] = access_int(scalar(cur, "SELECT Count(*) FROM tblDespesasEvento", 0))
        dados["total_despesas"] = money(scalar(cur, "SELECT Sum(ValorDespesa) FROM tblDespesasEvento", 0))
    except Exception as e:
        erro = str(e)
    finally:
        conn.close()
    return render_template("configuracoes.html", dados=dados, erro=erro)


@app.route("/equipes", methods=["GET", "POST"])
def web_equipes():
    conn = get_connection()
    erro = ""
    mensagem = request.args.get("ok", "")
    try:
        cur = conn.cursor()
        if request.method == "POST":
            nome = request.form.get("nome_equipe", "").strip()
            cidade = request.form.get("cidade", "").strip()
            estado = request.form.get("estado", "").strip()
            if not nome:
                raise ValueError("Informe o nome da equipe.")
            cur.execute("SELECT TOP 1 IDEquipe FROM tblEquipes WHERE NomeEquipe=?", [nome])
            if cur.fetchone():
                raise ValueError("Essa equipe já está cadastrada.")
            cur.execute("INSERT INTO tblEquipes (NomeEquipe, Cidade, Estado) VALUES (?, ?, ?)", [nome, cidade or None, estado or None])
            conn.commit()
            return redirect(url_for("web_equipes", ok="Equipe cadastrada com sucesso."))
        cur.execute("SELECT IDEquipe, NomeEquipe, Cidade, Estado FROM tblEquipes ORDER BY NomeEquipe")
        equipes = [{"id": access_int(r[0]), "nome": r[1] or "", "cidade": r[2] or "", "estado": r[3] or ""} for r in cur.fetchall()]
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        erro = str(e)
        equipes = locals().get("equipes", [])
    finally:
        conn.close()
    return render_template("equipes.html", equipes=equipes, mensagem=mensagem, erro=erro, novo=(request.args.get("novo") == "1"))

@app.post("/equipes/<int:equipe_id>/editar")
def editar_equipe(equipe_id):
    conn = get_connection()
    try:
        nome = request.form.get("nome_equipe", "").strip()
        cidade = request.form.get("cidade", "").strip()
        estado = request.form.get("estado", "").strip()
        if not nome:
            raise ValueError("Informe o nome da equipe.")
        cur = conn.cursor()
        cur.execute("SELECT TOP 1 IDEquipe FROM tblEquipes WHERE NomeEquipe=? AND IDEquipe<>?", [nome, equipe_id])
        if cur.fetchone():
            raise ValueError("Já existe outra equipe com esse nome.")
        cur.execute("UPDATE tblEquipes SET NomeEquipe=?, Cidade=?, Estado=? WHERE IDEquipe=?", [nome, cidade or None, estado or None, equipe_id])
        conn.commit()
        return redirect(url_for("web_equipes", ok="Equipe atualizada com sucesso."))
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return redirect(url_for("web_equipes", erro=str(e)))
    finally:
        conn.close()

@app.post("/equipes/<int:equipe_id>/excluir")
def excluir_equipe(equipe_id):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT TOP 1 IDCliente FROM tblClientes WHERE IDEquipe=?", [equipe_id])
        if cur.fetchone():
            return redirect(url_for("web_equipes", erro="Esta equipe já está vinculada a atleta(s) e não pode ser excluída."))
        cur.execute("DELETE FROM tblEquipes WHERE IDEquipe=?", [equipe_id])
        conn.commit()
        return redirect(url_for("web_equipes", ok="Equipe excluída com sucesso."))
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return redirect(url_for("web_equipes", erro=str(e)))
    finally:
        conn.close()

@app.route("/modalidades", methods=["GET", "POST"])
def web_modalidades():
    conn = get_connection()
    erro = ""
    mensagem = request.args.get("ok", "")
    try:
        cur = conn.cursor()
        if request.method == "POST":
            nome = request.form.get("modalidade", "").strip()
            if not nome:
                raise ValueError("Informe o nome da modalidade.")
            cur.execute("SELECT TOP 1 IDModalidade FROM tblModalidades WHERE Modalidade=?", [nome])
            if cur.fetchone():
                raise ValueError("Essa modalidade já está cadastrada.")
            cur.execute("INSERT INTO tblModalidades (Modalidade) VALUES (?)", [nome])
            conn.commit()
            return redirect(url_for("web_modalidades", ok="Modalidade cadastrada com sucesso."))

        cur.execute("SELECT IDModalidade, Modalidade FROM tblModalidades ORDER BY Modalidade")
        modalidades = [{"id": access_int(r[0]), "nome": r[1] or ""} for r in cur.fetchall()]
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        erro = str(e)
        modalidades = locals().get("modalidades", [])
    finally:
        conn.close()
    return render_template("modalidades.html", modalidades=modalidades, mensagem=mensagem, erro=erro)


@app.post("/modalidades/<int:modalidade_id>/editar")
def editar_modalidade(modalidade_id):
    conn = get_connection()
    try:
        nome = request.form.get("modalidade", "").strip()
        if not nome:
            raise ValueError("Informe o nome da modalidade.")
        cur = conn.cursor()
        cur.execute("SELECT TOP 1 IDModalidade FROM tblModalidades WHERE Modalidade=? AND IDModalidade<>?", [nome, modalidade_id])
        if cur.fetchone():
            raise ValueError("Essa modalidade já está cadastrada.")
        cur.execute("UPDATE tblModalidades SET Modalidade=? WHERE IDModalidade=?", [nome, modalidade_id])
        conn.commit()
        return redirect(url_for("web_modalidades", ok="Modalidade atualizada com sucesso."))
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return redirect(url_for("web_modalidades", erro=str(e)))
    finally:
        conn.close()


@app.post("/modalidades/<int:modalidade_id>/excluir")
def excluir_modalidade(modalidade_id):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT TOP 1 IDEvento FROM tblEvento WHERE IDModalidade=?", [modalidade_id])
        if cur.fetchone():
            return redirect(url_for("web_modalidades", erro="Esta modalidade já está vinculada a evento(s) e não pode ser excluída."))
        cur.execute("DELETE FROM tblModalidades WHERE IDModalidade=?", [modalidade_id])
        conn.commit()
        return redirect(url_for("web_modalidades", ok="Modalidade excluída com sucesso."))
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return redirect(url_for("web_modalidades", erro=str(e)))
    finally:
        conn.close()


def _despesas_colunas(cur):
    cur.execute("SELECT * FROM tblDespesasEvento WHERE 1=0")
    return [d[0] for d in cur.description]


@app.route("/despesas", methods=["GET", "POST"])
def web_despesas():
    conn = get_connection()
    erro = ""
    mensagem = request.args.get("ok", "")
    evento_filtro = request.args.get("evento", "").strip()
    try:
        cur = conn.cursor()
        colunas_db = _despesas_colunas(cur)

        # Eventos disponíveis para cadastrar e filtrar despesas.
        cur.execute("""
            SELECT IDEvento, NomeEvento, DataEvento, Cidade
            FROM tblEvento
            ORDER BY DataEvento DESC, NomeEvento
        """)
        eventos = []
        for r in cur.fetchall():
            eventos.append({
                "id": access_int(r[0]),
                "nome": r[1] or "",
                "data": r[2].strftime("%d/%m/%Y") if hasattr(r[2], "strftime") else str(r[2] or ""),
                "cidade": r[3] or ""
            })

        if request.method == "POST":
            descricao = request.form.get("descricao", "").strip()
            valor_raw = request.form.get("valor", "").strip().replace("R$", "").replace(" ", "").replace(".", "").replace(",", ".")
            data = request.form.get("data", "").strip()
            evento_id = request.form.get("evento_id", "").strip()
            observacoes = request.form.get("observacoes", "").strip()

            if not descricao:
                raise ValueError("Informe a descrição da despesa.")
            if not valor_raw:
                raise ValueError("Informe o valor da despesa.")
            if not evento_id:
                raise ValueError("Selecione o evento da despesa.")

            valor = float(valor_raw)
            if valor < 0:
                raise ValueError("O valor da despesa não pode ser negativo.")

            # Usa somente os campos que realmente existem no Access.
            mapa = {
                "Descricao": descricao, "Descrição": descricao, "DescricaoDespesa": descricao,
                "ValorDespesa": valor, "Valor": valor,
                "DataDespesa": data, "Data": data,
                "IDEvento": access_int(evento_id),
                "Observacoes": observacoes, "Observações": observacoes
            }
            campos, valores = [], []
            for c in colunas_db:
                if c in mapa and mapa[c] not in (None, ""):
                    campos.append(c)
                    valores.append(mapa[c])

            if "IDEvento" not in campos:
                raise ValueError("A tabela de despesas não possui o campo IDEvento necessário para vincular a despesa ao evento.")
            if not campos:
                raise ValueError("Não foi possível identificar os campos da tabela de despesas.")

            cur.execute(
                f"INSERT INTO tblDespesasEvento ({','.join(campos)}) VALUES ({','.join(['?']*len(campos))})",
                valores
            )
            conn.commit()
            return redirect(url_for("web_despesas", evento=access_int(evento_id), ok="Despesa registrada no evento com sucesso."))

        # Lista somente o evento selecionado. Sem seleção, mostra todas as despesas.
        where = ""
        params = []
        if evento_filtro:
            where = " WHERE D.IDEvento=? "
            params.append(access_int(evento_filtro))

        # Consulta amigável, sem expor IDs internos como a tela antiga fazia.
        descricao_col = next((c for c in ["Descricao", "Descrição", "DescricaoDespesa"] if c in colunas_db), None)
        valor_col = next((c for c in ["ValorDespesa", "Valor"] if c in colunas_db), None)
        data_col = next((c for c in ["DataDespesa", "Data"] if c in colunas_db), None)
        obs_col = next((c for c in ["Observacoes", "Observações"] if c in colunas_db), None)

        if not descricao_col or not valor_col or not data_col or "IDEvento" not in colunas_db:
            raise ValueError("A tabela de despesas precisa ter Descricao, ValorDespesa, DataDespesa e IDEvento.")

        obs_sql = f", D.[{obs_col}]" if obs_col else ", Null"
        sql = f"""
            SELECT D.[{descricao_col}], D.[{data_col}], E.NomeEvento, E.Cidade,
                   D.[{valor_col}], D.IDEvento{obs_sql}
            FROM tblDespesasEvento AS D
            LEFT JOIN tblEvento AS E ON D.IDEvento=E.IDEvento
            {where}
            ORDER BY D.[{data_col}] DESC
        """
        cur.execute(sql, params)
        rows = cur.fetchall()

        data = []
        for r in rows:
            data.append({
                "descricao": str(r[0] or ""),
                "data": r[1].strftime("%d/%m/%Y") if hasattr(r[1], "strftime") else str(r[1] or ""),
                "evento": str(r[2] or "Sem evento"),
                "cidade": str(r[3] or ""),
                "valor": money(r[4]),
                "observacoes": str(r[6] or "")
            })

        total_sql = f"SELECT Sum(D.[{valor_col}]) FROM tblDespesasEvento AS D"
        if where:
            total_sql += " WHERE D.IDEvento=?"
        total = money(scalar(cur, total_sql, 0, params))

        colunas = ["Descrição", "Data", "Evento", "Cidade", "Valor", "Observações"]
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        erro = str(e)
        colunas = locals().get("colunas", ["Descrição", "Data", "Evento", "Cidade", "Valor", "Observações"])
        data = locals().get("data", [])
        eventos = locals().get("eventos", [])
        total = locals().get("total", 0.0)
    finally:
        conn.close()

    return render_template(
        "despesas.html",
        colunas=colunas,
        data=data,
        eventos=eventos,
        evento_selecionado=access_int(evento_filtro) if evento_filtro else 0,
        total=total,
        mensagem=mensagem,
        erro=erro
    )


@app.route("/eventos")
def web_eventos():
    search = request.args.get("q", "").strip()
    conn = get_connection()
    try:
        cur = conn.cursor()
        sql = """
            SELECT
                E.IDEvento,
                E.NomeEvento,
                E.DataEvento,
                E.Cidade,
                M.Modalidade,
                E.Ativo
            FROM tblEvento AS E
            LEFT JOIN tblModalidades AS M
              ON E.IDModalidade=M.IDModalidade
        """
        params=[]
        if search:
            sql += """
                WHERE E.NomeEvento LIKE ?
                   OR E.Cidade LIKE ?
                   OR M.Modalidade LIKE ?
            """
            like="%"+search+"%"
            params=[like,like,like]
        sql += " ORDER BY E.DataEvento DESC, E.IDEvento DESC"

        cur.execute(sql,params)
        columns=[d[0] for d in cur.description]
        data=[]
        for row in cur.fetchall():
            item={}
            for c,v in zip(columns,row):
                if hasattr(v,"strftime"):
                    v=v.strftime("%d/%m/%Y")
                elif v is None:
                    v=""
                else:
                    v=str(v)
                item[c]=v
            data.append(item)
    finally:
        conn.close()

    return render_template(
        "module.html", page="eventos",
        title="Eventos / Provas",
        subtitle="Eventos cadastrados no SGFE",
        columns=["IDEvento","NomeEvento","DataEvento","Cidade","Modalidade","Ativo"],
        data=data, search=search,
        search_label="Pesquisar evento, cidade ou modalidade...",
        currency_columns=[],
        mensagem=request.args.get("ok",""),
        erro=request.args.get("erro","")
    )



@app.route("/eventos/geral")
def geral_eventos():
    """Painel geral de um evento: operação + financeiro."""
    evento_id_raw = request.args.get("evento", "").strip()
    conn = get_connection()
    erro = ""
    eventos = []
    resumo = {
        "agendamentos": 0,
        "vendas": 0,
        "entregas_pendentes": 0,
        "pagamentos_pendentes": 0,
        "total_vendido": 0.0,
        "total_recebido": 0.0,
        "despesas": 0.0,
        "lucro": 0.0,
    }
    evento_selecionado = 0
    evento_info = None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT IDEvento, NomeEvento, DataEvento, Cidade, Ativo
            FROM tblEvento
            ORDER BY DataEvento DESC, NomeEvento
        """)
        for r in cur.fetchall():
            eventos.append({
                "id": access_int(r[0]),
                "nome": str(r[1] or ""),
                "data": r[2].strftime("%d/%m/%Y") if hasattr(r[2], "strftime") else str(r[2] or ""),
                "cidade": str(r[3] or ""),
                "ativo": bool(r[4]) if r[4] is not None else False,
            })

        if evento_id_raw:
            evento_selecionado = access_int(evento_id_raw)
        elif eventos:
            evento_selecionado = eventos[0]["id"]

        if evento_selecionado:
            cur.execute("""
                SELECT IDEvento, NomeEvento, DataEvento, Cidade, Ativo
                FROM tblEvento
                WHERE IDEvento=?
            """, [evento_selecionado])
            r = cur.fetchone()
            if r:
                evento_info = {
                    "id": access_int(r[0]),
                    "nome": str(r[1] or ""),
                    "data": r[2].strftime("%d/%m/%Y") if hasattr(r[2], "strftime") else str(r[2] or ""),
                    "cidade": str(r[3] or ""),
                    "ativo": bool(r[4]) if r[4] is not None else False,
                }

                resumo["agendamentos"] = int(scalar(cur,
                    "SELECT Count(*) FROM tblAgendamentos WHERE IDEvento=?", 0, [evento_selecionado]) or 0)

                # Vendas do evento e situação de entrega.
                cur.execute("""
                    SELECT V.IDVenda, V.ValorFinal, V.Finalizado, V.StatusPagamento
                    FROM tblVendaPacotes AS V
                    LEFT JOIN tblStatusVenda AS S ON V.IDStatusVenda=S.IDStatusVenda
                    WHERE V.IDEvento=?
                      AND (S.StatusVenda Is Null OR UCASE(S.StatusVenda) NOT LIKE '%CANCEL%')
                """, [evento_selecionado])
                vendas_evento = cur.fetchall()
                resumo["vendas"] = len(vendas_evento)
                resumo["total_vendido"] = sum(float(r[1] or 0) for r in vendas_evento)
                resumo["entregas_pendentes"] = sum(1 for r in vendas_evento if r[2] is None or not bool(r[2]))

                pagamentos = {}
                cur.execute("""
                    SELECT P.IDVenda, Sum(P.ValorPago)
                    FROM tblPagamento AS P
                    INNER JOIN tblVendaPacotes AS V ON P.IDVenda=V.IDVenda
                    WHERE V.IDEvento=?
                    GROUP BY P.IDVenda
                """, [evento_selecionado])
                for r in cur.fetchall():
                    pagamentos[access_int(r[0])] = float(r[1] or 0)

                recebido = 0.0
                pendentes = 0
                for r in vendas_evento:
                    venda_id = access_int(r[0])
                    valor = float(r[1] or 0)
                    status_pg = str(r[3] or "aberto").strip().lower()
                    pago = pagamentos.get(venda_id, 0.0)
                    if status_pg == "cortesia":
                        continue
                    recebido += min(pago, valor) if valor > 0 else 0.0
                    if pago < valor - 0.009:
                        pendentes += 1
                resumo["total_recebido"] = recebido
                resumo["pagamentos_pendentes"] = pendentes

                # Despesas vinculadas ao evento.
                try:
                    resumo["despesas"] = money(scalar(
                        cur,
                        "SELECT Sum(ValorDespesa) FROM tblDespesasEvento WHERE IDEvento=?",
                        0,
                        [evento_selecionado]
                    ))
                except Exception:
                    resumo["despesas"] = 0.0

                resumo["lucro"] = resumo["total_recebido"] - resumo["despesas"]
    except Exception as e:
        erro = str(e)
    finally:
        conn.close()

    return render_template(
        "geral_eventos.html",
        page="eventos",
        eventos=eventos,
        evento_selecionado=evento_selecionado,
        evento_info=evento_info,
        resumo=resumo,
        erro=erro,
    )


@app.route("/eventos/novo", methods=["GET","POST"])
def novo_evento():
    conn=get_connection()
    erro=""
    form=dict(request.form) if request.method=="POST" else {}
    try:
        cur=conn.cursor()
        cur.execute("SELECT IDModalidade, Modalidade FROM tblModalidades ORDER BY Modalidade")
        modalidades=[{"id":access_int(r[0]),"nome":r[1] or ""} for r in cur.fetchall()]

        if request.method=="POST":
            nome=request.form.get("nome_evento","").strip()
            data=request.form.get("data_evento","").strip()
            cidade=request.form.get("cidade","").strip()
            modalidade=request.form.get("id_modalidade","").strip()
            ativo=bool(request.form.get("ativo"))

            if not nome: raise ValueError("Informe o nome do evento.")
            if not data: raise ValueError("Informe a data do evento.")
            if not cidade: raise ValueError("Informe a cidade.")
            if not modalidade: raise ValueError("Selecione a modalidade.")

            # HTML date -> Access date literal via parameter.
            cur.execute("""
                INSERT INTO tblEvento (NomeEvento, DataEvento, Cidade, IDModalidade, Ativo)
                VALUES (?, ?, ?, ?, ?)
            """,[nome,data,cidade,access_int(modalidade),ativo])
            conn.commit()
            # Depois de criar o evento, abre diretamente a configuração dos preços.
            novo_id = cur.execute("SELECT @@IDENTITY").fetchone()[0]
            return redirect(url_for("precos_evento", evento_id=access_int(novo_id)))

    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        erro=str(e)
    finally:
        conn.close()

    return render_template("evento_form.html",
        titulo="Novo Evento", form=form, modalidades=modalidades,
        erro=erro)


@app.route("/eventos/<int:evento_id>/precos", methods=["GET","POST"])
def precos_evento(evento_id):
    conn = get_connection()
    erro = ""
    mensagem = ""
    valores = {}
    nome_evento = ""
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT IDEvento, NomeEvento, DataEvento, Cidade
            FROM tblEvento
            WHERE IDEvento=?
        """, [evento_id])
        evento = cur.fetchone()
        if not evento:
            return "Evento não encontrado", 404

        nome_evento = evento[1] or ""

        if request.method == "POST":
            # Salva apenas linhas preenchidas. Se uma quantidade já existe,
            # atualiza; se não existe, cria.
            for qtd in range(1, 11):
                raw = request.form.get(f"valor_{qtd}", "").strip()
                if raw == "":
                    continue

                raw = raw.replace("R$", "").replace(" ", "").replace(".", "").replace(",", ".")
                valor = float(raw)
                if valor < 0:
                    raise ValueError(f"O valor da {qtd}ª prova não pode ser negativo.")

                cur.execute("""
                    SELECT COUNT(*)
                    FROM tblPrecoEvento
                    WHERE IDEvento=? AND QtdProvas=?
                """, [evento_id, qtd])
                existe = access_int(cur.fetchone()[0])

                if existe:
                    cur.execute("""
                        UPDATE tblPrecoEvento
                        SET Valor=?
                        WHERE IDEvento=? AND QtdProvas=?
                    """, [valor, evento_id, qtd])
                else:
                    cur.execute("""
                        INSERT INTO tblPrecoEvento (IDEvento, QtdProvas, Valor)
                        VALUES (?, ?, ?)
                    """, [evento_id, qtd, valor])

            conn.commit()
            mensagem = "Preços do evento salvos com sucesso."

        cur.execute("""
            SELECT QtdProvas, Valor
            FROM tblPrecoEvento
            WHERE IDEvento=?
            ORDER BY QtdProvas
        """, [evento_id])

        for r in cur.fetchall():
            try:
                qtd = access_int(r[0])
                valores[qtd] = float(r[1] or 0)
            except Exception:
                continue

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        erro = str(e)
    finally:
        conn.close()

    return render_template(
        "precos_evento.html",
        evento_id=evento_id,
        nome_evento=nome_evento,
        valores=valores,
        mensagem=mensagem,
        erro=erro
    )


@app.route("/eventos/<int:evento_id>/editar", methods=["GET","POST"])
def editar_evento(evento_id):
    conn=get_connection()
    erro=""
    try:
        cur=conn.cursor()
        cur.execute("SELECT IDModalidade, Modalidade FROM tblModalidades ORDER BY Modalidade")
        modalidades=[{"id":access_int(r[0]),"nome":r[1] or ""} for r in cur.fetchall()]

        if request.method=="POST":
            nome=request.form.get("nome_evento","").strip()
            data=request.form.get("data_evento","").strip()
            cidade=request.form.get("cidade","").strip()
            modalidade=request.form.get("id_modalidade","").strip()
            ativo=bool(request.form.get("ativo"))

            if not nome or not data or not cidade or not modalidade:
                raise ValueError("Preencha nome, data, cidade e modalidade.")

            cur.execute("""
                UPDATE tblEvento
                SET NomeEvento=?, DataEvento=?, Cidade=?, IDModalidade=?, Ativo=?
                WHERE IDEvento=?
            """,[nome,data,cidade,access_int(modalidade),ativo,evento_id])
            conn.commit()
            return redirect(url_for("web_eventos",ok="Evento atualizado com sucesso."))

        cur.execute("""
            SELECT IDEvento,NomeEvento,DataEvento,Cidade,IDModalidade,Ativo
            FROM tblEvento WHERE IDEvento=?
        """,[evento_id])
        r=cur.fetchone()
        if not r: return "Evento não encontrado",404

        form={
            "nome_evento":r[1] or "",
            "data_evento":r[2].strftime("%Y-%m-%d") if hasattr(r[2],"strftime") else "",
            "cidade":r[3] or "",
            "id_modalidade":access_int(r[4]) if r[4] is not None else "",
            "ativo":bool(r[5]) if r[5] is not None else True
        }
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        erro=str(e)
        form=dict(request.form) if request.method=="POST" else {}
    finally:
        conn.close()

    return render_template("evento_form.html",
        titulo="Editar Evento", form=form, modalidades=modalidades, erro=erro)


@app.route("/eventos/<int:evento_id>/excluir", methods=["POST"])
def excluir_evento(evento_id):
    conn=get_connection()
    try:
        cur=conn.cursor()

        # Preserva o histórico: evento usado em qualquer operação não é apagado.
        checks=[
            ("tblAgendamentos","IDEvento","agendamentos"),
            ("tblVendaPacotes","IDEvento","vendas/OS"),
            ("tblPrecoEvento","IDEvento","preços cadastrados")
        ]
        for tabela,campo,nome in checks:
            cur.execute(f"SELECT TOP 1 * FROM {tabela} WHERE {campo}=?",[evento_id])
            if cur.fetchone():
                return redirect(url_for(
                    "web_eventos",
                    erro=f"Este evento possui {nome} e não pode ser excluído. O histórico foi preservado."
                ))

        cur.execute("DELETE FROM tblEvento WHERE IDEvento=?",[evento_id])
        conn.commit()
        return redirect(url_for("web_eventos",ok="Evento excluído com sucesso."))
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return redirect(url_for("web_eventos",erro=str(e)))
    finally:
        conn.close()


@app.route("/entregas")
def web_entregas():
    evento_id = request.args.get("evento", "").strip()
    search = request.args.get("q", "").strip()
    mensagem = request.args.get("ok", "")
    erro = request.args.get("erro", "")
    conn = get_connection()
    try:
        cur = conn.cursor()

        # Eventos para o seletor.
        cur.execute("""
            SELECT IDEvento, NomeEvento, DataEvento, Cidade
            FROM tblEvento
            ORDER BY DataEvento DESC, NomeEvento
        """)
        eventos = []
        for r in cur.fetchall():
            eventos.append({
                "id": access_int(r[0]),
                "nome": str(r[1] or ""),
                "data": r[2].strftime("%d/%m/%Y") if hasattr(r[2], "strftime") else str(r[2] or ""),
                "cidade": str(r[3] or "")
            })

        # Formas de pagamento para o formulário flutuante de pagamento.
        cur.execute("SELECT IDFormaPagamento, FormaPagamento FROM tblFormaPagamento ORDER BY IDFormaPagamento")
        formas_pagamento = []
        for r in cur.fetchall():
            formas_pagamento.append({
                "id": access_int(r[0]),
                "nome": str(r[1] or "")
            })

        data = []
        if evento_id:
            eid = access_int(evento_id)
            cur.execute("""
                SELECT V.IDVenda, V.IDCliente, C.Nome AS Atleta,
                       C.Telefone, C.Contato, V.IDEvento, E.NomeEvento,
                       E.DataEvento, V.QtdProvas, V.ValorFinal,
                       V.Finalizado, V.DataFinalizacao, V.StatusPagamento
                FROM (((tblVendaPacotes AS V
                INNER JOIN tblClientes AS C ON V.IDCliente=C.IDCliente)
                INNER JOIN tblEvento AS E ON V.IDEvento=E.IDEvento)
                LEFT JOIN tblStatusVenda AS S ON V.IDStatusVenda=S.IDStatusVenda)
                WHERE V.IDEvento=?
                  AND (S.StatusVenda Is Null OR UCASE(S.StatusVenda) NOT LIKE '%CANCEL%')
                ORDER BY C.Nome, V.IDVenda
            """, [eid])
            vendas = cur.fetchall()

            # Pagamentos registrados. Uma venda pode ter mais de um pagamento.
            cur.execute("SELECT IDVenda FROM tblPagamento")
            pagos = set()
            for r in cur.fetchall():
                try:
                    pagos.add(access_int(r[0]))
                except Exception:
                    pass

            termo = search.lower()
            for r in vendas:
                id_venda = access_int(r[0])
                item = {
                    "id": id_venda,
                    "cliente_id": access_int(r[1]),
                    "atleta": str(r[2] or ""),
                    "telefone": str(r[3] or ""),
                    "contato": str(r[4] or ""),
                    "evento_id": access_int(r[5]),
                    "evento": str(r[6] or ""),
                    "data_evento": r[7].strftime("%d/%m/%Y") if hasattr(r[7], "strftime") else str(r[7] or ""),
                    "provas": access_int(r[8]),
                    "valor": money(r[9]),
                    "entregue": bool(r[10]) if r[10] is not None else False,
                    "data_finalizacao": r[11].strftime("%d/%m/%Y") if hasattr(r[11], "strftime") else str(r[11] or ""),
                    "status_pagamento": str(r[12] or "aberto").strip().lower(),
                    "pago": str(r[12] or "aberto").strip().lower() == "pago",
                    "cortesia": str(r[12] or "aberto").strip().lower() == "cortesia"
                }
                if not termo or termo in (item["atleta"] + " " + item["evento"]).lower():
                    data.append(item)

    finally:
        conn.close()

    return render_template(
        "entregas.html", page="entregas", title="Entregas",
        subtitle="Controle de entrega das fotos e situação do pagamento",
        eventos=eventos, evento_selecionado=access_int(evento_id) if evento_id else 0,
        data=data, search=search, mensagem=mensagem, erro=erro,
        formas_pagamento=formas_pagamento
    )


@app.post("/entregas/<int:id_venda>/finalizar")
def finalizar_entrega(id_venda):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT IDVenda FROM tblVendaPacotes WHERE IDVenda=?", [id_venda])
        if not cur.fetchone():
            return redirect(url_for("web_entregas", erro="Venda/OS não encontrada."))
        cur.execute("""
            UPDATE tblVendaPacotes
            SET Finalizado=True, DataFinalizacao=Now()
            WHERE IDVenda=?
        """, [id_venda])
        conn.commit()
        cur.execute("SELECT IDEvento FROM tblVendaPacotes WHERE IDVenda=?", [id_venda])
        r = cur.fetchone()
        eid = access_int(r[0]) if r else 0
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": True, "evento": eid, "mensagem": "Entrega finalizada com sucesso."})
        return redirect(url_for("web_entregas", evento=eid, ok="Entrega finalizada com sucesso."))
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "erro": str(e)}), 500
        return redirect(url_for("web_entregas", erro=str(e)))
    finally:
        conn.close()


@app.route("/pagamentos/pendentes")
def web_pagamentos_pendentes():
    venda_id = request.args.get("venda", "").strip()
    search = request.args.get("q", "").strip()
    conn = get_connection()
    try:
        cur = conn.cursor()
        sql = """
            SELECT V.IDVenda, C.Nome AS Atleta, E.NomeEvento AS Evento,
                   V.QtdProvas, V.ValorFinal, V.DataVenda, C.Telefone, C.Contato
            FROM ((tblVendaPacotes AS V
            INNER JOIN tblClientes AS C ON V.IDCliente=C.IDCliente)
            INNER JOIN tblEvento AS E ON V.IDEvento=E.IDEvento)
            LEFT JOIN tblPagamento AS P ON V.IDVenda=P.IDVenda
            WHERE P.IDPagamento Is Null
              AND (V.StatusPagamento Is Null OR V.StatusPagamento='aberto')
        """
        params=[]
        if venda_id:
            sql += " AND V.IDVenda=?"
            params.append(access_int(venda_id))
        elif search:
            sql += " AND (C.Nome LIKE ? OR E.NomeEvento LIKE ?)"
            like="%"+search+"%"
            params.extend([like,like])
        sql += " ORDER BY V.IDVenda DESC"
        cur.execute(sql, params)
        pendentes=[]
        for r in cur.fetchall():
            pendentes.append({
                "id":access_int(r[0]), "atleta":str(r[1] or ""),
                "evento":str(r[2] or ""), "provas":access_int(r[3]),
                "valor":money(r[4]),
                "data":r[5].strftime("%d/%m/%Y") if hasattr(r[5],"strftime") else str(r[5] or ""),
                "telefone":str(r[6] or ""), "contato":str(r[7] or "")
            })

        cur.execute("SELECT IDFormaPagamento, FormaPagamento FROM tblFormaPagamento ORDER BY IDFormaPagamento")
        formas=[{"id":access_int(r[0]),"nome":str(r[1] or "")} for r in cur.fetchall()]
        formas.append({"id": -1, "nome": "CANCELADO"})
    finally:
        conn.close()
    html = render_template("pagamentos_pendentes.html", page="pagamentos", title="Pagamentos Pendentes",
        subtitle="Vendas ainda sem pagamento registrado", pendentes=pendentes, formas=formas,
        venda_selecionada=access_int(venda_id) if venda_id else 0, search=search)

    # A tela atual usa o seletor de forma de pagamento também para a ação
    # especial CANCELADO. Sem exigir alteração do template existente, mudamos
    # o texto do botão quando essa opção é escolhida.
    script = """
<script>
(function(){
  function ajustarBotaoCancelamento(){
    var s=document.querySelector('[name="id_forma"]');
    if(!s) return;
    var cancelar=(s.value==='-1');
    var botoes=document.querySelectorAll('button,input[type="submit"]');
    botoes.forEach(function(b){
      var t=(b.innerText||b.value||'').trim().toUpperCase();
      if(t.indexOf('CONFIRMAR PAGAMENTO')>=0 || t==='CONFIRMAR'){
        b.innerText=cancelar?'FINALIZAR COMO CANCELADO':'✓ CONFIRMAR PAGAMENTO';
        if(b.tagName==='INPUT') b.value=cancelar?'FINALIZAR COMO CANCELADO':'CONFIRMAR PAGAMENTO';
      }
    });
  }
  document.addEventListener('DOMContentLoaded',function(){
    var s=document.querySelector('[name="id_forma"]');
    if(s){s.addEventListener('change',ajustarBotaoCancelamento);ajustarBotaoCancelamento();}
  });
})();
</script>
"""
    html = html.replace("FORMA DE PAGAMENTO", "FORMA DE PAGAMENTO / SITUAÇÃO", 1)

    if "</body>" in html:
        html = html.replace("</body>", script + "</body>", 1)
    else:
        html += script

    return html


@app.post("/pagamentos/registrar")
def registrar_pagamento():
    venda_id=access_int(request.form.get("id_venda") or 0)
    forma_id=access_int(request.form.get("id_forma") or 0)
    parcelas=access_int(request.form.get("parcelas") or 1)
    valor_text=(request.form.get("valor_pago") or "0").replace(".","").replace(",",".")
    observacoes=request.form.get("observacoes") or ""
    conn=get_connection()
    try:
        cur=conn.cursor()
        if not venda_id:
            raise ValueError("Venda/OS inválida.")

        # CANCELADO é uma ação operacional, não um pagamento.
        if forma_id == -1:
            _cancelar_venda_operacional(cur, venda_id)
            conn.commit()
            return redirect(url_for(
                "web_historico",
                ok="OS cancelada com sucesso. Pagamento não registrado e balizamento desmarcado."
            ))

        if not forma_id:
            raise ValueError("Selecione a forma de pagamento.")

        valor=float(valor_text or 0)
        if valor<=0:
            raise ValueError("Informe o valor pago.")

        cur.execute("SELECT IDPagamento FROM tblPagamento WHERE IDVenda=?",[venda_id])
        if cur.fetchone():
            raise ValueError("Esta venda já possui pagamento registrado.")

        cur.execute("""
            INSERT INTO tblPagamento (IDVenda, DataPagamento, IDFormaPagamento, Parcelas, ValorPago, Observacoes)
            VALUES (?, Now(), ?, ?, ?, ?)
        """,[venda_id,forma_id,parcelas,valor,observacoes])
        cur.execute("UPDATE tblVendaPacotes SET StatusPagamento='pago' WHERE IDVenda=?", [venda_id])
        conn.commit()
        cur.execute("SELECT IDEvento FROM tblVendaPacotes WHERE IDVenda=?",[venda_id])
        r=cur.fetchone(); eid=access_int(r[0]) if r else 0
        return redirect(url_for("web_entregas",evento=eid,ok="Pagamento registrado com sucesso."))
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return redirect(url_for("web_pagamentos_pendentes",venda=venda_id,erro=str(e)))
    finally:
        conn.close()


@app.post("/vendas/<int:id_venda>/cancelar")
def cancelar_venda(id_venda):
    """Cancela a OS mantendo histórico, sem pagamento, e limpa o balizamento."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        eid = _cancelar_venda_operacional(cur, id_venda)
        conn.commit()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({
                "ok": True,
                "evento": eid,
                "mensagem": "OS cancelada com sucesso. Pagamento não registrado e balizamento desmarcado."
            })

        return redirect(url_for(
            "web_historico",
            ok="OS cancelada com sucesso. Pagamento não registrado e balizamento desmarcado."
        ))
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "erro": str(e)}), 500
        return redirect(url_for("web_pagamentos_pendentes", venda=id_venda,erro=str(e)))
    finally:
        conn.close()


@app.post("/pagamentos/status")
def alterar_status_pagamento():
    venda_id = access_int(request.form.get("id_venda") or 0)
    status = (request.form.get("status") or "").strip().lower()
    conn = get_connection()
    try:
        cur = conn.cursor()
        if status == "cancelado":
            _cancelar_venda_operacional(cur, venda_id)
            conn.commit()
            return redirect(url_for(
                "web_historico",
                ok="OS cancelada com sucesso. Pagamento não registrado e balizamento desmarcado."
            ))
        if status not in ("aberto", "pago", "cortesia"):
            raise ValueError("Status de pagamento inválido.")
        cur.execute("SELECT IDEvento FROM tblVendaPacotes WHERE IDVenda=?", [venda_id])
        row = cur.fetchone()
        if not row:
            raise ValueError("Venda/OS não encontrada.")
        if status == "cortesia":
            cur.execute("SELECT IDPagamento FROM tblPagamento WHERE IDVenda=?", [venda_id])
            if cur.fetchone():
                raise ValueError("Esta OS já possui pagamento registrado e não pode ser marcada como cortesia.")
        if status == "pago":
            cur.execute("SELECT IDPagamento FROM tblPagamento WHERE IDVenda=?", [venda_id])
            if not cur.fetchone():
                raise ValueError("Para marcar como PAGO, registre primeiro o pagamento.")
        cur.execute("UPDATE tblVendaPacotes SET StatusPagamento=? WHERE IDVenda=?", [status, venda_id])
        conn.commit()
        eid = access_int(row[0])
        return redirect(url_for("web_pagamentos", evento=eid, ok="Status de pagamento atualizado."))
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return redirect(url_for("web_pagamentos", erro=str(e)))
    finally:
        conn.close()


@app.route("/pagamentos")
def web_pagamentos():
    evento_id = request.args.get("evento", "").strip()
    search = request.args.get("q", "").strip()
    status_filtro = request.args.get("status", "todos").strip().lower() or "todos"
    conn = get_connection()
    try:
        cur = conn.cursor()

        # Eventos disponíveis para o controle financeiro.
        cur.execute("""
            SELECT IDEvento, NomeEvento, DataEvento, Cidade
            FROM tblEvento
            ORDER BY DataEvento DESC, NomeEvento
        """)
        eventos = []
        for r in cur.fetchall():
            eventos.append({
                "id": access_int(r[0]),
                "nome": str(r[1] or ""),
                "data": r[2].strftime("%d/%m/%Y") if hasattr(r[2], "strftime") else str(r[2] or ""),
                "cidade": str(r[3] or "")
            })

        data = []
        resumo = {"vendido": 0.0, "recebido": 0.0, "aberto": 0.0, "total": 0,
                  "pagos": 0, "abertos": 0, "parciais": 0, "cortesias": 0}

        if evento_id:
            eid = access_int(evento_id)
            cur.execute("""
                SELECT V.IDVenda, V.DataVenda, C.Nome AS Atleta,
                       C.Telefone, C.Contato, E.NomeEvento, E.DataEvento,
                       V.QtdProvas, V.ValorFinal, V.Finalizado, V.StatusPagamento
                FROM (((tblVendaPacotes AS V
                INNER JOIN tblClientes AS C ON V.IDCliente=C.IDCliente)
                INNER JOIN tblEvento AS E ON V.IDEvento=E.IDEvento)
                LEFT JOIN tblStatusVenda AS S ON V.IDStatusVenda=S.IDStatusVenda)
                WHERE V.IDEvento=?
                  AND (S.StatusVenda Is Null OR UCASE(S.StatusVenda) NOT LIKE '%CANCEL%')
                ORDER BY C.Nome, V.IDVenda
            """, [eid])
            vendas = cur.fetchall()

            # Soma o que já foi recebido por venda. A estrutura aceita mais de
            # um registro histórico de pagamento, mesmo que o fluxo atual
            # normalmente registre um pagamento por venda.
            cur.execute("""
                SELECT IDVenda, Sum(ValorPago)
                FROM tblPagamento
                GROUP BY IDVenda
            """)
            pagamentos = {}
            for r in cur.fetchall():
                try:
                    pagamentos[access_int(r[0])] = float(r[1] or 0)
                except Exception:
                    pagamentos[access_int(r[0])] = 0.0

            termo = search.lower()
            for r in vendas:
                id_venda = access_int(r[0])
                valor = float(r[8] or 0)
                status_registrado = str(r[9] or "aberto").strip().lower()
                recebido = float(pagamentos.get(id_venda, 0) or 0)
                if status_registrado == "cortesia":
                    recebido = 0.0
                    aberto = 0.0
                    situacao = "cortesia"
                    situacao_label = "CORTESIA"
                elif recebido >= valor and valor > 0:
                    aberto = 0.0
                    situacao = "pago"
                    situacao_label = "PAGO"
                elif recebido > 0:
                    aberto = max(valor - recebido, 0.0)
                    situacao = "parcial"
                    situacao_label = "PARCIAL"
                else:
                    aberto = valor
                    situacao = "aberto"
                    situacao_label = "EM ABERTO"

                item = {
                    "id": id_venda,
                    "atleta": str(r[2] or ""),
                    "telefone": str(r[3] or ""),
                    "contato": str(r[4] or ""),
                    "evento": str(r[5] or ""),
                    "data_evento": r[6].strftime("%d/%m/%Y") if hasattr(r[6], "strftime") else str(r[6] or ""),
                    "provas": access_int(r[7]),
                    "valor": valor,
                    "recebido": recebido,
                    "aberto": aberto,
                    "situacao": situacao,
                    "situacao_label": situacao_label,
                    "status_pagamento": status_registrado,
                }

                resumo["vendido"] += valor
                resumo["recebido"] += min(recebido, valor)
                resumo["aberto"] += aberto
                resumo["total"] += 1
                if situacao == "pago": resumo["pagos"] += 1
                elif situacao == "parcial": resumo["parciais"] += 1
                elif situacao == "cortesia": resumo["cortesias"] += 1
                else: resumo["abertos"] += 1

                if termo and termo not in (item["atleta"] + " " + item["evento"]).lower():
                    continue
                if status_filtro in ("pago", "parcial", "aberto", "cortesia") and situacao != status_filtro:
                    continue
                data.append(item)
    finally:
        conn.close()

    return render_template(
        "pagamentos.html", page="pagamentos", title="Pagamentos",
        subtitle="Controle financeiro por evento",
        eventos=eventos,
        evento_selecionado=access_int(evento_id) if evento_id else 0,
        data=data, search=search, status_filtro=status_filtro, resumo=resumo
    )


@app.route("/historico")
def web_historico():
    """Histórico completo das OS, com visão de entrega e pagamento."""
    evento_id = request.args.get("evento", "").strip()
    search = request.args.get("q", "").strip()
    status_filtro = request.args.get("status", "todos").strip().lower() or "todos"
    mensagem = request.args.get("ok", "")
    erro = request.args.get("erro", "")

    conn = get_connection()
    try:
        cur = conn.cursor()

        # Eventos para o filtro.
        cur.execute("""
            SELECT IDEvento, NomeEvento, DataEvento, Cidade
            FROM tblEvento
            ORDER BY DataEvento DESC, NomeEvento
        """)
        eventos = []
        for r in cur.fetchall():
            eventos.append({
                "id": access_int(r[0]),
                "nome": str(r[1] or ""),
                "data": r[2].strftime("%d/%m/%Y") if hasattr(r[2], "strftime") else str(r[2] or ""),
                "cidade": str(r[3] or "")
            })

        # Todas as vendas/OS. O histórico não deve esconder uma OS por estar
        # finalizada ou paga: ele é justamente a memória do sistema.
        sql = """
            SELECT V.IDVenda, V.DataVenda, V.IDCliente, C.Nome AS Atleta,
                   C.Telefone, C.Contato, V.IDEvento, E.NomeEvento AS Evento,
                   E.DataEvento, E.Cidade, V.QtdProvas, V.ValorPacote,
                   V.ValorDesconto, V.ValorFinal, S.StatusVenda AS Status,
                   V.Finalizado, V.DataFinalizacao, V.StatusPagamento
            FROM ((tblVendaPacotes AS V
            LEFT JOIN tblClientes AS C ON V.IDCliente=C.IDCliente)
            LEFT JOIN tblEvento AS E ON V.IDEvento=E.IDEvento)
            LEFT JOIN tblStatusVenda AS S ON V.IDStatusVenda=S.IDStatusVenda
        """
        params = []
        where = []
        if evento_id:
            where.append("V.IDEvento=?")
            params.append(access_int(evento_id))
        if search:
            where.append("(C.Nome LIKE ? OR E.NomeEvento LIKE ? OR C.Telefone LIKE ? OR C.Contato LIKE ?)")
            like = "%" + search + "%"
            params.extend([like, like, like, like])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY V.DataVenda DESC, V.IDVenda DESC"
        cur.execute(sql, params)
        vendas = cur.fetchall()

        # Soma de pagamentos por OS. Isso deixa o histórico correto mesmo que
        # uma OS venha a ter mais de um lançamento de pagamento no futuro.
        cur.execute("""
            SELECT IDVenda, Sum(ValorPago) AS TotalPago
            FROM tblPagamento
            GROUP BY IDVenda
        """)
        pagos = {}
        for r in cur.fetchall():
            try:
                pagos[access_int(r[0])] = float(r[1] or 0)
            except Exception:
                pass

        data = []
        for r in vendas:
            id_venda = access_int(r[0])
            valor = float(r[13] or 0)
            status_registrado = str(r[17] or "aberto").strip().lower()
            status_venda_registrado = str(r[14] or "").strip().lower()
            recebido = pagos.get(id_venda, 0.0)
            if "cancel" in status_venda_registrado or status_registrado == "cancelado":
                recebido = 0.0
                aberto = 0.0
                situacao_pagamento = "cancelado"
            elif status_registrado == "cortesia":
                recebido = 0.0
                aberto = 0.0
                situacao_pagamento = "cortesia"
            elif recebido <= 0:
                aberto = valor
                situacao_pagamento = "aberto"
            elif recebido >= valor - 0.009:
                aberto = 0.0
                situacao_pagamento = "pago"
            else:
                aberto = max(valor - recebido, 0.0)
                situacao_pagamento = "parcial"

            if status_filtro != "todos" and situacao_pagamento != status_filtro:
                continue

            item = {
                "id": id_venda,
                "data_venda": r[1].strftime("%d/%m/%Y") if hasattr(r[1], "strftime") else str(r[1] or ""),
                "cliente_id": access_int(r[2]),
                "atleta": str(r[3] or ""),
                "telefone": str(r[4] or ""),
                "contato": str(r[5] or ""),
                "evento_id": access_int(r[6]),
                "evento": str(r[7] or ""),
                "data_evento": r[8].strftime("%d/%m/%Y") if hasattr(r[8], "strftime") else str(r[8] or ""),
                "cidade": str(r[9] or ""),
                "provas": access_int(r[10]),
                "pacote": float(r[11] or 0),
                "desconto": float(r[12] or 0),
                "valor": valor,
                "status": str(r[14] or ""),
                "entregue": bool(r[15]) if r[15] is not None else False,
                "data_entrega": r[16].strftime("%d/%m/%Y") if hasattr(r[16], "strftime") else str(r[16] or ""),
                "recebido": recebido,
                "aberto": aberto,
                "situacao_pagamento": situacao_pagamento,
                "status_pagamento": status_registrado,
            }
            data.append(item)

        # Resumo respeitando o evento selecionado, mas mostrando a realidade
        # completa das OS daquele filtro, independente do filtro de situação.
        vendas_validas = [
            r for r in vendas
            if "cancel" not in str(r[14] or "").strip().lower()
            and str(r[17] or "aberto").strip().lower() != "cancelado"
        ]
        resumo_vendido = sum(float((r[13] or 0)) for r in vendas_validas)
        resumo_recebido = sum(pagos.get(access_int(r[0]), 0.0) for r in vendas_validas)
        resumo_aberto = sum(
            (0.0 if str(r[17] or "aberto").strip().lower() == "cortesia"
             else max(float(r[13] or 0) - pagos.get(access_int(r[0]), 0.0), 0.0))
            for r in vendas_validas
        )
        resumo_entregues = sum(1 for r in vendas_validas if bool(r[15]))
        resumo_pagos = sum(
            1 for r in vendas_validas
            if pagos.get(access_int(r[0]), 0.0) >= float(r[13] or 0) - 0.009
        )
        resumo_abertos = sum(
            1 for r in vendas_validas
            if pagos.get(access_int(r[0]), 0.0) <= 0.009
        )
        resumo_cortesias = sum(
            1 for r in vendas_validas
            if str(r[17] or "aberto").strip().lower() == "cortesia"
        )
        resumo_parciais = max(
            len(vendas_validas) - resumo_pagos - resumo_abertos - resumo_cortesias, 0
        )

    finally:
        conn.close()

    return render_template(
        "historico.html", page="historico", title="Histórico",
        subtitle="A memória completa das suas OS, entregas e pagamentos.",
        eventos=eventos,
        evento_selecionado=access_int(evento_id) if evento_id else 0,
        data=data, search=search, status_filtro=status_filtro,
        mensagem=mensagem, erro=erro,
        resumo={
            "os": len(vendas),
            "vendido": resumo_vendido,
            "recebido": resumo_recebido,
            "aberto": resumo_aberto,
            "entregues": resumo_entregues,
            "pagos": resumo_pagos,
            "abertos": resumo_abertos,
            "parciais": resumo_parciais,
            "cortesias": resumo_cortesias,
        }
    )

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
