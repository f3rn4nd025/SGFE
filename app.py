from flask import Flask, render_template, jsonify, request, redirect, url_for, send_file, session
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import os
import re
import unicodedata
import uuid
import io
import shutil
import sqlite3

try:
    import psycopg2
except Exception:
    psycopg2 = None

from datetime import datetime

try:
    import pyodbc
except Exception:
    pyodbc = None

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

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

        # Alguns IDs do Access migrados para PostgreSQL chegam como um
        # único caractere Unicode cujo code point é o ID + 256.
        # Recupera o ID sem alterar nenhuma regra de negócio.
        if len(s) == 1 and ord(s) >= 256:
            return ord(s) - 256

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
app.secret_key = os.environ.get("SGFE_SECRET_KEY", "SGFE-FG-FOTOS-LOCAL-2026")
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30

# =========================================================
# ACESSO SGFE
# =========================================================
# Administrador inicial do sistema.
# Pode ser alterado por variáveis de ambiente quando publicado.
ADMIN_LOGIN = os.environ.get("SGFE_ADMIN_LOGIN", "admin")
ADMIN_PASSWORD = os.environ.get("SGFE_ADMIN_PASSWORD", "SGFE@2026")


def _senha_confere(senha, senha_hash_ou_senha):
    # Fotógrafos usam hash Werkzeug; o administrador inicial usa a
    # senha definida acima, sem depender de tabela adicional no banco.
    try:
        if check_password_hash(str(senha_hash_ou_senha or ""), senha):
            return True
    except Exception:
        pass
    return senha == senha_hash_ou_senha


def _next_seguro(destino):
    destino = (destino or "").strip()
    if destino.startswith("/") and not destino.startswith("//"):
        return destino
    return url_for("index")


@app.before_request
def controlar_acesso_sgfe():
    caminho = request.path or "/"

    if caminho.startswith("/static/") or caminho in (
        "/login", "/logout", "/fotografo/login", "/fotografo/logout"
    ):
        return None

    if session.get("admin_autenticado"):
        return None

    if session.get("fotografo_id"):
        # Fotógrafos têm acesso ao SGFE inteiro, inclusive Configurações,
        # mas não podem administrar os próprios fotógrafos/usuários.
        if caminho == "/fotografos" or caminho.startswith("/fotografos/"):
            return redirect(url_for("index"))
        return None

    return redirect(url_for("login", next=caminho))


# =========================================================
# MODAL SGFE PADRÃO - CONFIRMAÇÕES GLOBAIS
# =========================================================
# Mantém as regras das telas e do banco intactas. Apenas substitui
# o confirm() nativo do navegador pelos modais visuais do SGFE.
# Isso também vale para templates que usam confirm() diretamente.
# =========================================================

@app.after_request
def aplicar_modal_sgfe(response):
    try:
        if not response.content_type or not response.content_type.startswith("text/html"):
            return response

        html = response.get_data(as_text=True)
        if "</body>" not in html.lower():
            return response

        # PAINEL DO FOTÓGRAFO: substitui a identidade visual do administrador
        # somente para usuários com perfil FOTOGRAFO. O painel, menu e regras
        # operacionais continuam exatamente os mesmos.
        if session.get("perfil") == "FOTOGRAFO":
            import re
            if re.search(r'class=["\']brand-mark["\']', html, flags=re.IGNORECASE):
                marca_fotografo = '<div class="brand-mark fotografo-brand-mark" aria-label="SGFE - Gestão de Fotos Esportivas"><div class="fotografo-sgfe">SGFE</div><div class="fotografo-subtitulo">GESTÃO DE FOTOS ESPORTIVAS</div></div>'
                html = re.sub(r'<div\s+class=["\']brand-mark["\'][^>]*>.*?</div>', marca_fotografo, html, count=1, flags=re.IGNORECASE | re.DOTALL)
                estilo_marca = '<style id="sgfe-marca-fotografo"><style>.fotografo-brand-mark{display:flex!important;flex-direction:column!important;align-items:flex-start!important;justify-content:center!important;gap:3px!important;padding-left:20px!important;box-sizing:border-box!important;}.fotografo-sgfe{font-size:46px;font-weight:900;line-height:.95;letter-spacing:1px;color:#fff;}.fotografo-subtitulo{font-size:11px;font-weight:800;letter-spacing:1px;color:#18d8de;white-space:nowrap;}</style>'
                # Corrige a tag de estilo duplicada propositalmente evitada abaixo.
                estilo_marca = estilo_marca.replace('<style id="sgfe-marca-fotografo"><style>', '<style id="sgfe-marca-fotografo">')
                if 'id="sgfe-marca-fotografo"' not in html:
                    pos_marca = html.lower().find('</head>')
                    if pos_marca >= 0:
                        html = html[:pos_marca] + estilo_marca + html[pos_marca:]

        # SAIR fica no menu lateral, em todas as telas autenticadas.
        # Não altera o restante do menu nem cria outro botão no cabeçalho.
        if session.get("admin_autenticado") or session.get("fotografo_id"):
            destino_logout = "/logout"
            import re
            if re.search(r'class=["\']sidebar["\']', html, flags=re.IGNORECASE) and not re.search(r'href=["\'](?:/logout|/fotografo/logout)["\']', html, flags=re.IGNORECASE):
                logout_menu = '<a class="menu sgfe-menu-logout" href="%s" aria-label="Sair do sistema"><b>↪</b> SAIR</a>' % destino_logout
                html = re.sub(r'</aside>', logout_menu + '</aside>', html, count=1, flags=re.IGNORECASE)

        modal_script = r"""
<style id="fg-modal-sgfe-style">
  .fg-modal-sgfe-backdrop{
    position:fixed;
    inset:0;
    z-index:99999;
    display:none;
    align-items:center;
    justify-content:center;
    padding:20px;
    background:rgba(0,0,0,.76);
    backdrop-filter:blur(4px);
  }
  .fg-modal-sgfe-backdrop.show{display:flex}
  .fg-modal-sgfe{
    width:min(430px,calc(100vw - 40px));
    background:#111416;
    border:1px solid #18d6c5;
    border-radius:16px;
    box-shadow:0 0 0 1px rgba(24,214,197,.12),0 18px 55px rgba(0,0,0,.65),0 0 28px rgba(24,214,197,.12);
    color:#f4f7f8;
    overflow:hidden;
    animation:fgModalSgfeEntrada .16s ease-out;
  }
  @keyframes fgModalSgfeEntrada{
    from{opacity:0;transform:translateY(8px) scale(.98)}
    to{opacity:1;transform:translateY(0) scale(1)}
  }
  .fg-modal-sgfe-head{
    padding:24px 24px 12px;
    text-align:center;
  }
  .fg-modal-sgfe-icon{
    width:52px;
    height:52px;
    margin:0 auto 12px;
    border-radius:50%;
    display:flex;
    align-items:center;
    justify-content:center;
    border:1px solid #18d6c5;
    color:#18d6c5;
    background:#0b2222;
    font-size:25px;
    font-weight:800;
    box-shadow:0 0 18px rgba(24,214,197,.12);
  }
  .fg-modal-sgfe-title{
    margin:0;
    color:#ffffff;
    font-size:17px;
    line-height:1.25;
    font-weight:900;
    letter-spacing:.6px;
  }
  .fg-modal-sgfe-title span{color:#18d6c5}
  .fg-modal-sgfe-message{
    padding:0 26px 22px;
    text-align:center;
    color:#c9d1d3;
    font-size:13px;
    line-height:1.55;
    white-space:pre-line;
  }
  .fg-modal-sgfe-actions{
    display:flex;
    gap:10px;
    padding:15px 20px 20px;
    border-top:1px solid #252b2d;
  }
  .fg-modal-sgfe-btn{
    flex:1;
    min-height:42px;
    border-radius:9px;
    padding:0 15px;
    cursor:pointer;
    font-size:11px;
    font-weight:900;
    letter-spacing:.7px;
    transition:.15s ease;
  }
  .fg-modal-sgfe-cancel{
    color:#e8edef;
    background:#171b1d;
    border:1px solid #3b4548;
  }
  .fg-modal-sgfe-cancel:hover{
    border-color:#18d6c5;
    color:#18d6c5;
  }
  .fg-modal-sgfe-confirm{
    color:#071313;
    background:#18d6c5;
    border:1px solid #18d6c5;
  }
  .fg-modal-sgfe-confirm:hover{filter:brightness(1.08)}
  .fg-modal-sgfe-confirm:disabled{opacity:.6;cursor:default}
</style>

<div id="fgModalSgfe" class="fg-modal-sgfe-backdrop" aria-hidden="true">
  <div class="fg-modal-sgfe" role="dialog" aria-modal="true" aria-labelledby="fgModalSgfeTitle">
    <div class="fg-modal-sgfe-head">
      <div id="fgModalSgfeIcon" class="fg-modal-sgfe-icon">?</div>
      <h3 id="fgModalSgfeTitle" class="fg-modal-sgfe-title">CONFIRMAR <span>AÇÃO</span></h3>
    </div>
    <div id="fgModalSgfeMessage" class="fg-modal-sgfe-message"></div>
    <div class="fg-modal-sgfe-actions">
      <button type="button" id="fgModalSgfeCancel" class="fg-modal-sgfe-btn fg-modal-sgfe-cancel">CANCELAR</button>
      <button type="button" id="fgModalSgfeConfirm" class="fg-modal-sgfe-btn fg-modal-sgfe-confirm">✓ CONFIRMAR</button>
    </div>
  </div>
</div>

<script id="fg-modal-sgfe-script">
(function(){
  'use strict';

  var modal, titulo, mensagem, confirmar, cancelar, icone;
  var pendente=null;
  var liberados=[];

  function escaparTexto(v){
    return String(v||'')
      .replace(/\\'/g,"'")
      .replace(/\\"/g,'"')
      .replace(/\\n/g,'\n')
      .replace(/\\r/g,'\r')
      .replace(/\\\\/g,'\\');
  }

  function extrairMensagem(codigo){
    if(!codigo || codigo.indexOf('confirm')<0) return '';
    var m=codigo.match(/confirm\s*\(\s*(['"])([\s\S]*?)\1\s*\)/i);
    return m ? escaparTexto(m[2]) : '';
  }

  function textoElemento(el){
    if(!el) return '';
    return (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
  }

  function configurar(el, codigo){
    var texto=textoElemento(el).toUpperCase();
    var msg=extrairMensagem(codigo||'');
    var t='CONFIRMAR AÇÃO';
    var ic='?';

    if(texto.indexOf('ENTREGAR')>=0){
      t='CONFIRMAR ENTREGA';
      ic='?';
      msg='Confirmar que as fotos desta OS\nforam entregues?';
    }else if(texto.indexOf('EXCLUIR')>=0){
      t='CONFIRMAR EXCLUSÃO';
      ic='?';
      if(!msg) msg='Deseja realmente excluir este registro?';
    }else if(msg){
      t='CONFIRMAR AÇÃO';
      ic='?';
    }

    var partes=t.split(' '); titulo.innerHTML=partes[0]+' <span>'+partes.slice(1).join(' ')+'</span>';
    mensagem.textContent=msg || 'Deseja confirmar esta ação?';
    icone.textContent=ic;
  }

  function mostrar(el,codigo,continuar){
    if(!modal) return;
    pendente={el:el,codigo:codigo||'',continuar:continuar};
    configurar(el,codigo||'');
    modal.classList.add('show');
    modal.setAttribute('aria-hidden','false');
    setTimeout(function(){confirmar.focus();},20);
  }

  function fechar(){
    if(!modal) return;
    modal.classList.remove('show');
    modal.setAttribute('aria-hidden','true');
    pendente=null;
  }

  function executarPendente(){
    var p=pendente;
    fechar();
    if(!p) return;
    if(typeof p.continuar==='function') p.continuar();
  }

  function jaLiberado(form){
    var i=liberados.indexOf(form);
    if(i>=0){liberados.splice(i,1);return true;}
    return false;
  }

  function encontrarAlvoConfirmavel(el){
    while(el && el!==document.body){
      if(el.nodeType===1){
        var onclick=el.getAttribute('onclick')||'';
        var texto=(el.innerText || el.value || el.getAttribute('aria-label') || '').trim().toUpperCase();

        // ENTREGAR: intercepta pelo próprio botão, independentemente de
        // onde o confirm() esteja (onclick/onsubmit/código da tela).
        if(/^(✓\s*)?ENTREGAR$/.test(texto) || texto.indexOf('ENTREGAR')===0){
          return {el:el,codigo:onclick||'confirm(\"Confirmar que as fotos desta OS\\nforam entregues?\")',entrega:true};
        }

        // Demais confirmações continuam usando a lógica original.
        if(onclick && /confirm\s*\(/i.test(onclick)) return {el:el,codigo:onclick,entrega:false};
      }
      el=el.parentElement;
    }
    return null;
  }

  function iniciar(){
    modal=document.getElementById('fgModalSgfe');
    titulo=document.getElementById('fgModalSgfeTitle');
    mensagem=document.getElementById('fgModalSgfeMessage');
    confirmar=document.getElementById('fgModalSgfeConfirm');
    cancelar=document.getElementById('fgModalSgfeCancel');
    icone=document.getElementById('fgModalSgfeIcon');
    if(!modal) return;

    cancelar.addEventListener('click',fechar);
    confirmar.addEventListener('click',executarPendente);

    modal.addEventListener('click',function(e){
      if(e.target===modal) fechar();
    });

    document.addEventListener('keydown',function(e){
      if(e.key==='Escape' && modal.classList.contains('show')) fechar();
    });

    // Captura botões/links que tenham confirm() no onclick.
    document.addEventListener('click',function(e){
      if(modal.classList.contains('show')) return;
      var alvo=encontrarAlvoConfirmavel(e.target);
      if(!alvo) return;

      e.preventDefault();
      e.stopImmediatePropagation();

      var el=alvo.el;
      var codigo=alvo.codigo;
      mostrar(el,codigo,function(){
        // ENTREGAR: depois da confirmação nesta caixa (padrão SGFE), envia
        // exatamente o mesmo formulário/rota original.
        if(alvo.entrega && el.form){
          var form=el.form;
          var onsubmit=form.getAttribute('onsubmit');
          var onclick=el.getAttribute('onclick');
          var confirmOriginal=window.confirm;
          try{
            form.removeAttribute('onsubmit');
            el.removeAttribute('onclick');
            // A ação já foi confirmada nesta caixa. Se a própria tela
            // também confirma antes de enviar (como a tela ENTREGAS, que
            // confirma de novo e só então chama fetch() para
            // /entregas/<id>/finalizar), evitamos perguntar uma segunda
            // vez usando o confirm() nativo do navegador.
            window.confirm=function(){ return true; };
            // requestSubmit() é usado em vez de submit(): só ele dispara
            // o evento "submit" do JavaScript. submit() envia o
            // formulário "por baixo dos panos" sem disparar esse evento,
            // e por isso a tela nunca chegava a chamar o fetch() real.
            if(form.requestSubmit){ form.requestSubmit(el); }
            else{ HTMLFormElement.prototype.submit.call(form); }
          }finally{
            window.confirm=confirmOriginal;
            if(onsubmit!==null) form.setAttribute('onsubmit',onsubmit);
            if(onclick!==null) el.setAttribute('onclick',onclick);
          }
          return;
        }

        // ENTREGAR fora de formulário: remove apenas o onclick durante
        // a execução para preservar a ação original sem o confirm nativo.
        el.removeAttribute('onclick');
        try{ el.click(); }
        finally{ el.setAttribute('onclick',codigo); }
      });
    },true);

    // Captura formulários cujo onsubmit possui confirm().
    document.addEventListener('submit',function(e){
      var form=e.target;
      if(!form || form.nodeType!==1) return;
      if(jaLiberado(form)) return;

      var submitter=e.submitter || document.activeElement;
      var codigo=(form.getAttribute('onsubmit')||'');
      var alvo=submitter && submitter.getAttribute ? (submitter.getAttribute('onclick')||'') : '';
      var temConfirm=/confirm\s*\(/i.test(codigo) || /confirm\s*\(/i.test(alvo);

      if(!temConfirm) return;

      e.preventDefault();
      e.stopImmediatePropagation();

      var base=submitter && textoElemento(submitter) ? submitter : form;
      var codigoConfirm=/confirm\s*\(/i.test(alvo) ? alvo : codigo;

      mostrar(base,codigoConfirm,function(){
        liberados.push(form);
        if(form.requestSubmit){
          if(submitter && submitter.form===form) form.requestSubmit(submitter);
          else form.requestSubmit();
        }else{
          HTMLFormElement.prototype.submit.call(form);
        }
      });
    },true);
  }

  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',iniciar);
  else iniciar();
})();
</script>
"""

        # Evita duplicação caso uma resposta HTML já tenha recebido o modal.
        if 'id="fgModalSgfe"' in html:
            return response

        pos = html.lower().rfind('</body>')
        html = html[:pos] + modal_script + html[pos:]

        # ENTREGAS: apenas normaliza a fonte dos dados das linhas da tabela.
        # Não altera filtro, consultas, rotas, banco ou qualquer ação da tela.
        if request.path == '/entregas':
            css_entregas_fonte = "<style id='sgfe-entregas-fonte-normal'>table tbody td, table tbody td * { font-weight: 400 !important; }</style>"
            html = html[:pos] + css_entregas_fonte + html[pos:]

        # PAGAMENTOS: mesma normalização de fonte da tela ENTREGAS (só
        # visual). Não altera filtro, consultas, rotas, banco ou ação.
        if request.path == '/pagamentos':
            css_pagamentos_fonte = "<style id='sgfe-pagamentos-fonte-normal'>table tbody td, table tbody td * { font-weight: 400 !important; }</style>"
            html = html[:pos] + css_pagamentos_fonte + html[pos:]

        # DESPESAS: o botão FILTRAR é um <button>, enquanto MENU PRINCIPAL/
        # VOLTAR/LIMPAR são links <a class="nav-btn"> que já recebem o
        # estilo certo do style.css. Como esse CSS parece valer só para
        # <a>, o <button class="nav-btn"> ficava com a aparência padrão
        # (branca) do navegador. Isso só ajusta a aparência deste botão
        # específico, sem alterar filtro, consultas, rotas ou ação.
        if request.path == '/despesas':
            css_despesas_botao = (
                "<style id='sgfe-despesas-botao-filtrar'>"
                ".expense-filter button.nav-btn{"
                "background:#0b5b5e;color:#fff;border:1px solid #12d8de;"
                "border-radius:8px;padding:11px 16px;font-weight:900;"
                "cursor:pointer;}"
                ".expense-filter button.nav-btn:hover{filter:brightness(1.08);}"
                "</style>"
            )
            html = html[:pos] + css_despesas_botao + html[pos:]

        response.set_data(html)
        return response
    except Exception:
        # A camada visual nunca pode derrubar uma tela operacional.
        return response


# =========================================================
# BANCO DE DADOS
# =========================================================
# Local Windows: mantém o Access para não alterar o ambiente de testes.
# Render: usa o PostgreSQL definido em DATABASE_URL.
# O modo SQLite continua disponível para testes locais/backup.
#
# IMPORTANTE: a lógica de negócio e as telas permanecem intactas.
# Aqui somente trocamos a camada de conexão para que o SGFE consiga
# conversar com o PostgreSQL do Render sem reescrever as consultas existentes.
DB_PATH = os.environ.get("SGFE_ACCESS_DB", r"C:\FG FOTOS\SGFE.accdb")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_MODE = os.environ.get("SGFE_DB_MODE", "access").strip().lower()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATA_DIR = os.environ.get("SGFE_DATA_DIR", os.path.join(BASE_DIR, "dados"))
ADMIN_DB = os.path.join(DATA_DIR, "sgfe_admin.sqlite3")
TENANT_DB_DIR = os.path.join(DATA_DIR, "bancos_fotografos")
BALIZAMENTO_FOLDER = os.path.join(DATA_DIR, "balizamentos")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TENANT_DB_DIR, exist_ok=True)
os.makedirs(BALIZAMENTO_FOLDER, exist_ok=True)


def _is_postgres():
    """Render usa PostgreSQL quando DATABASE_URL estiver disponível."""
    return bool(DATABASE_URL) or DB_MODE == "postgres"


def _is_sqlite():
    return DB_MODE in {"sqlite", "render", "production"} and not _is_postgres()


def _tenant_db_path(id_fotografo):
    if _is_postgres():
        return f"fotografo_{access_int(id_fotografo)}"
    ext = ".sqlite3" if _is_sqlite() else ".accdb"
    return os.path.join(TENANT_DB_DIR, f"fotografo_{access_int(id_fotografo)}{ext}")


def _tenant_balizamento_folder():
    """Cada fotógrafo também possui sua própria área de balizamentos."""
    if session.get("perfil") == "FOTOGRAFO" and session.get("fotografo_id"):
        pasta = os.path.join(BALIZAMENTO_FOLDER, f"fotografo_{access_int(session['fotografo_id'])}")
        os.makedirs(pasta, exist_ok=True)
        return pasta
    return BALIZAMENTO_FOLDER


class SQLiteCompatCursor:
    """Cursor SQLite com pequenas compatibilidades para o SQL já usado pelo SGFE."""
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        return self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)

    def __iter__(self):
        return iter(self._cursor)

    def execute(self, sql, params=None):
        sql = _sqlite_sql(sql)
        if params is None:
            self._cursor.execute(sql)
        else:
            self._cursor.execute(sql, params)
        return self

    def executemany(self, sql, seq_of_params):
        self._cursor.executemany(_sqlite_sql(sql), seq_of_params)
        return self

    def close(self):
        return self._cursor.close()


class SQLiteCompatConnection:
    def __init__(self, path):
        self.path = path
        self._conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys=OFF")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.create_function("Now", 0, lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self._conn.create_function("UCASE", 1, lambda v: str(v or "").upper())
        self._conn.create_function("LCASE", 1, lambda v: str(v or "").lower())
        self._conn.create_function("IsNull", 1, lambda v: 1 if v is None else 0)
        self._conn.create_function("IIf", 3, lambda cond, a, b: a if cond else b)

    def cursor(self):
        return SQLiteCompatCursor(self._conn.cursor())

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def tables(self, tableType="TABLE"):
        rows = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return [_SQLiteTableRow(r[0]) for r in rows]


class _SQLiteTableRow:
    def __init__(self, name):
        self.table_name = name


class PostgreSQLCompatCursor:
    """Adapta as consultas Access/SQLite existentes para PostgreSQL."""
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        return self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)

    def __iter__(self):
        return iter(self._cursor)

    def execute(self, sql, params=None):
        original_sql = sql
        converted_sql = _postgres_sql(sql)
        try:
            if params is None:
                self._cursor.execute(converted_sql)
            else:
                self._cursor.execute(converted_sql, params)
            return self
        except Exception as exc:
            print("[SGFE-SQL-ERRO]", flush=True)
            print(f"[SGFE-SQL-ERRO] original={original_sql!r}", flush=True)
            print(f"[SGFE-SQL-ERRO] convertido={converted_sql!r}", flush=True)
            print(f"[SGFE-SQL-ERRO] parametros={params!r}", flush=True)
            print(f"[SGFE-SQL-ERRO] erro={exc}", flush=True)
            try:
                self._cursor.connection.rollback()
            except Exception:
                pass
            raise

    def executemany(self, sql, seq_of_params):
        self._cursor.executemany(_postgres_sql(sql), seq_of_params)
        return self

    def tables(self, tableType="TABLE"):
        self._cursor.execute("""
            SELECT tablename
            FROM pg_catalog.pg_tables
            WHERE schemaname = current_schema()
            ORDER BY tablename
        """)
        return [_PostgreSQLTableRow(r[0]) for r in self._cursor.fetchall()]

    def close(self):
        return self._cursor.close()


class _PostgreSQLTableRow:
    def __init__(self, name):
        self.table_name = name


class PostgreSQLCompatConnection:
    def __init__(self, url, schema="public"):
        if psycopg2 is None:
            raise RuntimeError("psycopg2-binary não está instalado. Adicione psycopg2-binary ao requirements.txt.")
        self.url = url
        self.schema = schema or "public"
        self._conn = psycopg2.connect(url, connect_timeout=15)
        self._conn.autocommit = False
        cur = self._conn.cursor()
        cur.execute("SET search_path TO \"public\"" if self.schema == "public" else f'SET search_path TO "{self.schema}", "public"')
        cur.close()

    def cursor(self):
        return PostgreSQLCompatCursor(self._conn.cursor())

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def tables(self, tableType="TABLE"):
        cur = self._conn.cursor()
        cur.execute("""
            SELECT tablename
            FROM pg_catalog.pg_tables
            WHERE schemaname = current_schema()
            ORDER BY tablename
        """)
        rows = cur.fetchall()
        cur.close()
        return [_PostgreSQLTableRow(r[0]) for r in rows]


def _sqlite_sql(sql):
    if not isinstance(sql, str):
        return sql
    s = sql
    s = re.sub(r"SELECT\s+@@IDENTITY", "SELECT last_insert_rowid()", s, flags=re.I)
    m = re.search(r"\bSELECT\s+TOP\s+(\d+)\s+", s, flags=re.I | re.S)
    if m:
        n = m.group(1)
        s = s[:m.start()] + re.sub(r"\bSELECT\s+TOP\s+\d+\s+", "SELECT ", s[m.start():], count=1, flags=re.I)
        semi = ";" if s.rstrip().endswith(";") else ""
        core = s.rstrip(";").rstrip()
        core += f" LIMIT {n}"
        s = core + semi
    s = re.sub(r"\bLONG\b", "INTEGER", s, flags=re.I)
    s = re.sub(r"\bYESNO\b", "INTEGER", s, flags=re.I)
    s = re.sub(r"\bAUTOINCREMENT\s+PRIMARY\s+KEY", "INTEGER PRIMARY KEY AUTOINCREMENT", s, flags=re.I)
    return s


def _postgres_sql(sql):
    """Converte somente as diferenças de dialeto necessárias pelo SGFE."""
    if not isinstance(sql, str):
        return sql
    s = sql

    # Access identity -> PostgreSQL sequence da última inserção.
    s = re.sub(r"SELECT\s+@@IDENTITY", "SELECT LASTVAL()", s, flags=re.I)

    # Access SELECT TOP n -> PostgreSQL LIMIT n.
    # Também cobre SELECT DISTINCT TOP n.
    m = re.search(
        r"\bSELECT\s+(DISTINCT\s+)?TOP\s+(\d+)\s+",
        s,
        flags=re.I | re.S
    )
    if m:
        distinct = m.group(1) or ""
        n = m.group(2)
        s = s[:m.start()] + "SELECT " + distinct + s[m.end():]
        semi = ";" if s.rstrip().endswith(";") else ""
        core = s.rstrip(";").rstrip()
        core += f" LIMIT {n}"
        s = core + semi

    # Colchetes do Access. PostgreSQL usa identificadores sem aspas em minúsculas.
    s = re.sub(r"\[([^\]]+)\]", r"\1", s)

    # Funções usadas pelo código legado.
    s = re.sub(r"\bUCASE\s*\(", "UPPER(", s, flags=re.I)
    s = re.sub(r"\bLCASE\s*\(", "LOWER(", s, flags=re.I)
    s = re.sub(
        r"\bIIf\s*\(\s*IsNull\s*\(\s*([^\)]+)\s*\)\s*,\s*([^,]+)\s*,\s*([^\)]+)\s*\)",
        r"CASE WHEN \1 IS NULL THEN \2 ELSE \3 END",
        s,
        flags=re.I,
    )
    s = re.sub(r"\bNow\s*\(\s*\)", "CURRENT_TIMESTAMP", s, flags=re.I)

    # Tipos Access usados pelas tabelas auxiliares criadas pelo próprio app.
    s = re.sub(r"\bLONG\b", "INTEGER", s, flags=re.I)
    s = re.sub(r"\bYESNO\b", "BOOLEAN", s, flags=re.I)
    s = re.sub(r"\bDATETIME\b", "TIMESTAMP", s, flags=re.I)
    s = re.sub(r"\bTEXT\s*\(\s*(\d+)\s*\)", r"VARCHAR(\1)", s, flags=re.I)
    s = re.sub(r"\bAUTOINCREMENT\s+PRIMARY\s+KEY", "BIGSERIAL PRIMARY KEY", s, flags=re.I)

    # As consultas atuais usam ? como placeholder. psycopg2 usa %s.
    s = s.replace("?", "%s")
    return s


def _abrir_conexao(db_path=None, schema="public"):
    if _is_postgres():
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL não foi configurada no Render.")
        return PostgreSQLCompatConnection(DATABASE_URL, schema=schema)
    if _is_sqlite():
        return SQLiteCompatConnection(db_path)
    if pyodbc is None:
        raise RuntimeError("pyodbc não está instalado para o modo Access.")
    drivers = [d for d in pyodbc.drivers() if "Access Driver" in d]
    if not drivers:
        raise RuntimeError("Driver Microsoft Access não encontrado.")
    driver = drivers[-1]
    return pyodbc.connect(f"DRIVER={{{driver}}};DBQ={db_path};")


def _limpar_dados_tenant(conn):
    """Apaga somente dados operacionais da cópia privada, preservando o esquema."""
    ordem = [
        "tblMarcacaoBalizamento", "tblBalizamentoManualProvas", "tblBalizamentoProvas",
        "tblPagamento", "tblVendaPacotes", "tblAgendamentos", "tblPrecoEvento",
        "tblDespesasEvento", "tblClientes", "tblEquipes", "tblEvento", "tblModalidades",
        "tblFotografos",
    ]
    restantes = list(ordem)
    for _ in range(len(ordem) + 2):
        progresso = False
        proximos = []
        for tabela in restantes:
            try:
                cur = conn.cursor()
                cur.execute(f"DELETE FROM [{tabela}]")
                progresso = True
                cur.close()
            except Exception:
                proximos.append(tabela)
        restantes = proximos
        if not restantes or not progresso:
            break
    if restantes:
        raise RuntimeError("Não foi possível preparar o banco privado do fotógrafo: " + ", ".join(restantes))
    conn.commit()


def _criar_banco_fotografo(id_fotografo):
    """Cria o banco/esquema privado do fotógrafo a partir do banco administrativo."""
    destino = _tenant_db_path(id_fotografo)

    if _is_postgres():
        schema = destino
        admin = PostgreSQLCompatConnection(DATABASE_URL, schema="public")
        try:
            cur = admin.cursor()

            # Só cria e copia os dados na PRIMEIRA vez. Sem esta checagem,
            # a função tentava copiar tudo de novo em TODO login do
            # fotógrafo — e como o schema já tinha os dados da vez
            # anterior, o INSERT batia em chave primária duplicada
            # (ex.: tblAgendamentos) e o login falhava.
            cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name=?", [schema])
            if cur.fetchone():
                return destino

            cur.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public' ORDER BY tablename")
            tabelas = [r[0] for r in cur.fetchall()]
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            for tabela in tabelas:
                cur.execute(f'CREATE TABLE IF NOT EXISTS "{schema}"."{tabela}" (LIKE public."{tabela}" INCLUDING ALL)')
                cur.execute(f'INSERT INTO "{schema}"."{tabela}" SELECT * FROM public."{tabela}"')
            admin.commit()
        finally:
            admin.close()

        conn = PostgreSQLCompatConnection(DATABASE_URL, schema=schema)
        try:
            _limpar_dados_tenant(conn)
        finally:
            conn.close()
        return destino

    if os.path.exists(destino):
        return destino
    if _is_sqlite():
        if not os.path.exists(ADMIN_DB):
            raise FileNotFoundError(f"Banco administrativo não encontrado: {ADMIN_DB}. Execute a migração do Access antes do primeiro acesso.")
        shutil.copy2(ADMIN_DB, destino)
    else:
        if not os.path.exists(DB_PATH):
            raise FileNotFoundError(f"Banco principal não encontrado: {DB_PATH}")
        tmp = destino + ".tmp"
        try:
            if os.path.exists(tmp): os.remove(tmp)
            shutil.copy2(DB_PATH, tmp)
            conn = _abrir_conexao(tmp)
            try: _limpar_dados_tenant(conn)
            finally: conn.close()
            os.replace(tmp, destino)
            return destino
        except Exception:
            try:
                if os.path.exists(tmp): os.remove(tmp)
            except Exception: pass
            raise
    conn = _abrir_conexao(destino)
    try:
        _limpar_dados_tenant(conn)
    finally:
        conn.close()
    return destino


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
            # PostgreSQL aborta a transação quando o SELECT falha.
            try:
                conn.rollback()
            except Exception:
                pass
            cur = conn.cursor()
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
    """Garante o campo StatusPagamento sem deixar transação PostgreSQL abortada."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT TOP 1 StatusPagamento FROM tblVendaPacotes")
        cur.fetchone()
    except Exception:
        # No PostgreSQL, uma consulta que falha deixa a transação em estado
        # aborted. É obrigatório fazer rollback antes de executar o ALTER TABLE.
        try:
            conn.rollback()
        except Exception:
            pass
        cur = conn.cursor()
        cur.execute("ALTER TABLE tblVendaPacotes ADD COLUMN StatusPagamento TEXT(20)")
        conn.commit()

    # Migra registros antigos: quem já tem pagamento vira PAGO; os demais ABERTO.
    cur = conn.cursor()
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


def _ensure_autoincrement_ids(conn):
    """
    Corrige tabelas migradas do Access que ficaram sem auto-incremento
    de verdade no PostgreSQL. No Access, o campo "Numeração Automática"
    gera o próximo ID sozinho; ao migrar pro Postgres, essas colunas
    viraram INTEGER comuns, sem sequence/DEFAULT associado. Resultado:
    todo INSERT que não informa o ID explicitamente (como o app sempre
    fez, contando com o Access) falha com "null value ... violates
    not-null constraint".

    Roda a cada conexão (mesmo padrão dos outros _ensure_*), corrigindo
    o schema da vez (public ou o schema privado do fotógrafo).
    """
    if not _is_postgres():
        return
    tabelas_colunas = [
        ("tbldespesasevento", "iddespesa"),
        ("tblmodalidades", "idmodalidade"),
        ("tblequipes", "idequipe"),
        ("tblevento", "idevento"),
    ]
    cur = conn.cursor()
    for tabela, coluna in tabelas_colunas:
        try:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = %s",
                (tabela,),
            )
            if not cur.fetchone():
                continue

            cur.execute(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
                (tabela, coluna),
            )
            row = cur.fetchone()
            if row and row[0] and "nextval" in str(row[0]):
                continue  # já tem sequence, não mexe

            seq_nome = f"{tabela}_{coluna}_seq"
            cur.execute(f'CREATE SEQUENCE IF NOT EXISTS "{seq_nome}"')
            cur.execute(
                f'ALTER TABLE "{tabela}" ALTER COLUMN "{coluna}" '
                f"SET DEFAULT nextval('{seq_nome}')"
            )
            cur.execute(f'ALTER SEQUENCE "{seq_nome}" OWNED BY "{tabela}"."{coluna}"')
            cur.execute(
                f"SELECT setval('{seq_nome}', "
                f'COALESCE((SELECT MAX("{coluna}") FROM "{tabela}"), 0) + 1, false)'
            )
            conn.commit()
        except Exception as exc:
            print(f"[SGFE] Aviso migração auto-incremento {tabela}.{coluna}: {exc}", flush=True)
            try:
                conn.rollback()
            except Exception:
                pass
    try:
        cur.close()
    except Exception:
        pass


def _ensure_fotografos(conn):
    """Garante a estrutura de fotógrafos, incluindo o campo de bloqueio."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT TOP 1 IDFotografo, Nome, Celular, [Login], SenhaHash, PainelLiberado, DataCadastro FROM tblFotografos")
        return
    except Exception:
        pass
    try:
        cur.execute("SELECT TOP 1 IDFotografo FROM tblFotografos")
        try:
            cur.execute("SELECT TOP 1 * FROM tblFotografos")
            nomes = {str(d[0]).lower() for d in (cur.description or [])}
        except Exception:
            nomes = set()
        adds = [
            ("nome", "ALTER TABLE tblFotografos ADD COLUMN Nome TEXT(255)"),
            ("celular", "ALTER TABLE tblFotografos ADD COLUMN Celular TEXT(50)"),
            ("login", "ALTER TABLE tblFotografos ADD COLUMN [Login] TEXT(100)"),
            ("senhahash", "ALTER TABLE tblFotografos ADD COLUMN SenhaHash TEXT(255)"),
            ("painelliberado", "ALTER TABLE tblFotografos ADD COLUMN PainelLiberado YESNO"),
            ("datacadastro", "ALTER TABLE tblFotografos ADD COLUMN DataCadastro DATETIME"),
        ]
        for nome, sql in adds:
            if nome not in nomes:
                try: cur.execute(sql)
                except Exception: pass
        try: cur.execute("UPDATE tblFotografos SET PainelLiberado=True WHERE PainelLiberado IS NULL")
        except Exception: pass
        conn.commit()
        return
    except Exception:
        pass
    cur.execute("""
        CREATE TABLE tblFotografos (
            IDFotografo AUTOINCREMENT PRIMARY KEY,
            Nome TEXT(255), Celular TEXT(50), [Login] TEXT(100),
            SenhaHash TEXT(255), PainelLiberado YESNO, DataCadastro DATETIME
        )
    """)
    conn.commit()

def _log_postgres_status(conn, origem="get_connection"):
    """Registra no log do Render exatamente qual banco/schema o SGFE está usando."""
    if not _is_postgres():
        return
    try:
        cur = conn.cursor()
        cur.execute("SELECT current_database(), current_schema(), current_user")
        info = cur.fetchone() or ("", "", "")
        cur.execute("""
            SELECT tablename
            FROM pg_catalog.pg_tables
            WHERE schemaname = current_schema()
            ORDER BY tablename
        """)
        tabelas = [str(r[0]) for r in cur.fetchall()]
        cur.close()
        importantes = [
            "tblclientes", "tblevento", "tblagendamentos",
            "tblvendapacotes", "tblpagamento", "tblbalizamentoprovas"
        ]
        presentes = [t for t in importantes if t in tabelas]
        print(
            f"[SGFE-DB] {origem} | database={info[0]} | schema={info[1]} "
            f"| user={info[2]} | tabelas={len(tabelas)} | importantes={presentes}",
            flush=True
        )
    except Exception as e:
        print(f"[SGFE-DB] falha ao diagnosticar conexão: {e}", flush=True)


@app.route("/api/diagnostico-banco")
def diagnostico_banco():
    """Diagnóstico somente leitura da conexão PostgreSQL do SGFE."""
    if not session.get("admin_autenticado"):
        return jsonify({"ok": False, "erro": "Acesso restrito ao administrador."}), 403
    if not _is_postgres():
        return jsonify({
            "ok": False,
            "modo": DB_MODE,
            "erro": "O SGFE não está em modo PostgreSQL nesta instância.",
        }), 500
    if not DATABASE_URL:
        return jsonify({"ok": False, "erro": "DATABASE_URL não está configurada."}), 500

    conn = None
    try:
        conn = PostgreSQLCompatConnection(DATABASE_URL, schema="public")
        cur = conn.cursor()
        cur.execute("SELECT current_database(), current_schema(), current_user")
        banco, schema, usuario = cur.fetchone()

        tabelas_alvo = [
            "tblModalidades", "tblSexos", "tblStatusAgendamento",
            "tblStatusVenda", "tblFormaPagamento", "tblEquipes",
            "tblFotografos", "tblEvento", "tblClientes", "tblPrecoEvento",
            "tblDespesasEvento", "tblAgendamentos", "tblVendaPacotes",
            "tblPagamento", "tblParcelas", "tblBalizamentoProvas",
            "tblBalizamentoManualProvas", "tblMarcacaoBalizamento",
            "tblFiltroCarteira"
        ]
        resultado = {}
        for tabela in tabelas_alvo:
            nome = tabela.lower()
            cur.execute(
                "SELECT COUNT(*) FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=current_schema() AND c.relname=%s",
                [nome]
            )
            existe = bool((cur.fetchone() or [0])[0])
            if existe:
                cur.execute(f'SELECT COUNT(*) FROM "{nome}"')
                resultado[tabela] = int((cur.fetchone() or [0])[0])
            else:
                resultado[tabela] = None

        cur.close()
        return jsonify({
            "ok": True,
            "modo": "postgresql",
            "database": banco,
            "schema": schema,
            "usuario": usuario,
            "tabelas": resultado,
        })
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: pass
        return jsonify({"ok": False, "erro": str(e)}), 500
    finally:
        if conn:
            conn.close()


def get_connection():
    # ADMIN trabalha no banco administrativo.
    # Cada fotógrafo trabalha no seu schema privado quando o SGFE está no PostgreSQL.
    if _is_postgres():
        if session.get("perfil") == "FOTOGRAFO" and session.get("fotografo_id"):
            schema = _tenant_db_path(session["fotografo_id"])
            conn = PostgreSQLCompatConnection(DATABASE_URL, schema=schema)
        else:
            conn = PostgreSQLCompatConnection(DATABASE_URL, schema="public")
    else:
        if session.get("perfil") == "FOTOGRAFO" and session.get("fotografo_id"):
            db_path = _tenant_db_path(session["fotografo_id"])
            if not os.path.exists(db_path):
                _criar_banco_fotografo(session["fotografo_id"])
        else:
            db_path = ADMIN_DB if _is_sqlite() else DB_PATH

        if not os.path.exists(db_path):
            raise FileNotFoundError(f"Banco não encontrado: {db_path}")

        conn = _abrir_conexao(db_path)

    _log_postgres_status(conn)
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
    try:
        _ensure_fotografos(conn)
    except Exception:
        pass
    try:
        _ensure_autoincrement_ids(conn)
    except Exception:
        pass
    return conn


def scalar(cursor, sql, default=0, params=None):
    """Executa uma consulta escalar sem contaminar a transação em caso de erro."""
    try:
        if params is None:
            cursor.execute(sql)
        else:
            cursor.execute(sql, params)
        row = cursor.fetchone()
        if not row or row[0] is None:
            return default
        return row[0]
    except Exception as exc:
        # PostgreSQL aborta a transação quando uma consulta falha.
        # Sem rollback, a próxima consulta recebe apenas:
        # "current transaction is aborted".
        try:
            raw = getattr(cursor, "_cursor", None)
            conn = getattr(raw, "connection", None)
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        print("[SGFE-SCALAR-ERRO]", flush=True)
        print(f"[SGFE-SCALAR-ERRO] sql={sql!r}", flush=True)
        print(f"[SGFE-SCALAR-ERRO] params={params!r}", flush=True)
        print(f"[SGFE-SCALAR-ERRO] erro={exc}", flush=True)
        return default


def money(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _status_venda_id_por_nome(cur, *nomes_candidatos):
    """
    Procura em tblStatusVenda um status cujo nome bata (sem diferenciar
    maiúsculas/minúsculas) com algum dos nomes candidatos, na ordem dada,
    e devolve o IDStatusVenda já normalizado. Usada para manter
    IDStatusVenda (campo que a tela VENDAS exibe) sincronizado com
    StatusPagamento (campo que Pagamentos/Entregas/Histórico usam) sempre
    que um dos dois é alterado. Devolve None se nenhum nome candidato foi
    encontrado na tabela.
    """
    cur.execute("SELECT IDStatusVenda, StatusVenda FROM tblStatusVenda")
    por_nome = {}
    for r in cur.fetchall():
        por_nome.setdefault(str(r[1] or "").strip().upper(), access_int(r[0]))
    for nome in nomes_candidatos:
        if nome.strip().upper() in por_nome:
            return por_nome[nome.strip().upper()]
    return None


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
# A estrutura é criada automaticamente no banco para não exigir
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


@app.route("/login", methods=["GET", "POST"])
def login():
    erro = ""
    next_url = _next_seguro(request.values.get("next", ""))

    if request.method == "POST":
        login_digitado = request.form.get("login", "").strip()
        senha = request.form.get("senha", "")
        lembrar = request.form.get("lembrar") == "1"

        # ADMIN: mantém exatamente o acesso administrativo que já funcionava.
        if login_digitado.casefold() == ADMIN_LOGIN.casefold() and senha == ADMIN_PASSWORD:
            session.clear()
            session.permanent = lembrar
            session["perfil"] = "ADMIN"
            session["admin_autenticado"] = True
            session["admin_login"] = ADMIN_LOGIN
            return redirect(next_url)

        # FOTÓGRAFO: usa diretamente o cadastro existente em tblFotografos.
        conn = None
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT TOP 1 IDFotografo, Nome, SenhaHash, PainelLiberado
                FROM tblFotografos
                WHERE UCASE([Login])=UCASE(?)
            """, [login_digitado])
            r = cur.fetchone()

            if not r:
                erro = "Login ou senha inválidos."
            elif not bool(r[3]):
                erro = "Seu acesso está bloqueado. Procure o administrador."
            elif not _senha_confere(senha, r[2] or ""):
                erro = "Login ou senha inválidos."
            else:
                fotografo_id = access_int(r[0])
                # O primeiro acesso cria o banco privado sem copiar os dados
                # operacionais existentes no banco do administrador.
                _criar_banco_fotografo(fotografo_id)
                session.clear()
                session.permanent = lembrar
                session["perfil"] = "FOTOGRAFO"
                session["fotografo_id"] = fotografo_id
                session["fotografo_nome"] = str(r[1] or "")
                return redirect(next_url)
        except Exception as e:
            import traceback
            print("[SGFE-LOGIN-ERRO]", flush=True)
            print(f"[SGFE-LOGIN-ERRO] login={login_digitado!r}", flush=True)
            print(f"[SGFE-LOGIN-ERRO] erro={e}", flush=True)
            traceback.print_exc()
            erro = "Não foi possível validar o acesso agora."
        finally:
            if conn:
                conn.close()

    return render_template("login.html", erro=erro, next=next_url)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    session.permanent = False
    session.modified = True
    return redirect(url_for("login"))


@app.route("/api/evento-geral")
def api_evento_geral():
    """
    PAINEL SOMENTE.

    IDEvento é character varying no PostgreSQL (herança da migração do
    Access) e vários registros antigos foram migrados em representações
    diferentes (bytes/hex/texto). Comparar com CAST(...AS TEXT)=CAST(?AS
    TEXT) ou com JOIN direto falha silenciosamente para esses registros
    (zero linhas, sem erro) — por isso os cartões apareciam todos
    zerados. A solução, igual às demais telas (Vendas, Pagamentos,
    Entregas, Histórico, Eventos/Geral), é buscar cada tabela por
    completo e cruzar os IDs já normalizados em Python.
    """
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        # -----------------------------------------------------
        # 1) Lista de eventos
        # -----------------------------------------------------
        cur.execute("""
            SELECT IDEvento, NomeEvento, DataEvento, Cidade, Ativo
            FROM tblEvento
            ORDER BY DataEvento DESC, NomeEvento
        """)

        eventos = []
        for r in cur.fetchall():
            eventos.append({
                "id": access_int(r[0]),
                "nome": str(r[1] or ""),
                "data": r[2].strftime("%d/%m/%Y")
                        if hasattr(r[2], "strftime") else str(r[2] or ""),
                "cidade": str(r[3] or ""),
                "ativo": bool(r[4]) if r[4] is not None else False,
            })

        # -----------------------------------------------------
        # 2) ID do evento selecionado, já normalizado
        # -----------------------------------------------------
        evento_id_raw = request.args.get("evento", "").strip()
        if evento_id_raw:
            evento_selecionado = access_int(evento_id_raw)
        elif eventos:
            evento_selecionado = eventos[0]["id"]
        else:
            evento_selecionado = 0

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

        evento_info = None

        if evento_selecionado:
            evento_info = next(
                (e for e in eventos if e["id"] == evento_selecionado), None
            )

            if evento_info:
                # -------------------------------------------------
                # 3) AGENDAMENTOS
                # -------------------------------------------------
                cur.execute("SELECT IDEvento FROM tblAgendamentos")
                resumo["agendamentos"] = sum(
                    1 for r in cur.fetchall()
                    if access_int(r[0]) == evento_selecionado
                )

                # -------------------------------------------------
                # 4) VENDAS (excluindo CANCELADAS, mesmo critério das
                #    demais telas)
                # -------------------------------------------------
                cur.execute("SELECT IDStatusVenda, StatusVenda FROM tblStatusVenda")
                status_nome_map = {access_int(r[0]): str(r[1] or "") for r in cur.fetchall()}

                cur.execute("""
                    SELECT IDVenda, ValorFinal, Finalizado, StatusPagamento,
                           IDStatusVenda, IDEvento
                    FROM tblVendaPacotes
                """)
                vendas_evento = [
                    r for r in cur.fetchall()
                    if access_int(r[5]) == evento_selecionado
                    and "CANCEL" not in status_nome_map.get(access_int(r[4]), "").upper()
                    and str(r[3] or "").strip().lower() != "cancelado"
                ]

                resumo["vendas"] = len(vendas_evento)
                # Cortesia não participa do fechamento financeiro do evento:
                # entra na contagem de vendas, mas com valor zerado.
                resumo["total_vendido"] = sum(
                    float(r[1] or 0) for r in vendas_evento
                    if str(r[3] or "").strip().lower() != "cortesia"
                )
                resumo["entregas_pendentes"] = sum(
                    1 for r in vendas_evento
                    if r[2] is None or not bool(r[2])
                )

                # -------------------------------------------------
                # 5) PAGAMENTOS
                # -------------------------------------------------
                cur.execute("SELECT IDVenda, Sum(ValorPago) FROM tblPagamento GROUP BY IDVenda")
                pagamentos = {}
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

                # -------------------------------------------------
                # 6) DESPESAS
                # -------------------------------------------------
                try:
                    cur.execute("SELECT IDEvento, ValorDespesa FROM tblDespesasEvento")
                    resumo["despesas"] = money(sum(
                        float(r[1] or 0) for r in cur.fetchall()
                        if access_int(r[0]) == evento_selecionado
                    ))
                except Exception:
                    resumo["despesas"] = 0.0

                resumo["lucro"] = (
                    resumo["total_recebido"] - resumo["despesas"]
                )

        return jsonify({
            "ok": True,
            "eventos": eventos,
            "evento": evento_info,
            "resumo": resumo
        })

    except Exception as e:
        print("[SGFE-PAINEL-ERRO]", flush=True)
        print(f"[SGFE-PAINEL-ERRO] {e}", flush=True)

        try:
            if conn:
                conn.rollback()
        except Exception:
            pass

        return jsonify({
            "ok": False,
            "erro": str(e)
        }), 500

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
    """
    ATLETAS SOMENTE.

    O Painel (/api/evento-geral) não é alterado nesta versão.

    A consulta retorna os 53 clientes do PostgreSQL e cria aliases para
    os nomes de campos usados pelo template. Isso evita a situação em que
    a consulta encontra os registros, mas a tabela fica visualmente vazia
    porque o template procura outro nome de chave.
    """
    search = request.args.get("q", "").strip()
    conn = get_connection()

    try:
        cur = conn.cursor()

        sql = """
        SELECT
            C.IDCliente,
            C.Nome,
            C.Nome AS Atleta,
            X.Sexo,
            X.Sexo AS Categoria,
            C.AnoNascimento,
            E.NomeEquipe,
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

        params = []

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
        rows = cur.fetchall()

        data = []

        for row in rows:
            # Acesso por posição, sem depender do nome retornado pelo driver.
            id_cliente = row[0]
            nome = row[1]
            atleta = row[2]
            sexo = row[3]
            categoria = row[4]
            ano = row[5]
            nome_equipe = row[6]
            equipe = row[7]
            modalidade = row[8]
            telefone = row[9]
            contato = row[10]
            cidade = row[11]
            estado = row[12]

            # Mantemos todos os aliases necessários para o template.
            item = {
                "IDCliente": "" if id_cliente is None else str(id_cliente),
                "id": "" if id_cliente is None else str(id_cliente),

                "Nome": "" if nome is None else str(nome),
                "nome": "" if nome is None else str(nome),

                "Atleta": "" if atleta is None else str(atleta),
                "atleta": "" if atleta is None else str(atleta),

                "Sexo": "" if sexo is None else str(sexo),
                "sexo": "" if sexo is None else str(sexo),

                "Categoria": "" if categoria is None else str(categoria),
                "categoria": "" if categoria is None else str(categoria),

                "AnoNascimento": "" if ano is None else str(ano),
                "ano_nascimento": "" if ano is None else str(ano),

                "NomeEquipe": "" if nome_equipe is None else str(nome_equipe),
                "Equipe": "" if equipe is None else str(equipe),
                "equipe": "" if equipe is None else str(equipe),

                "Modalidade": "" if modalidade is None else str(modalidade),
                "modalidade": "" if modalidade is None else str(modalidade),

                "Telefone": "" if telefone is None else str(telefone),
                "telefone": "" if telefone is None else str(telefone),

                "Contato": "" if contato is None else str(contato),
                "contato": "" if contato is None else str(contato),

                "Cidade": "" if cidade is None else str(cidade),
                "cidade": "" if cidade is None else str(cidade),

                "Estado": "" if estado is None else str(estado),
                "estado": "" if estado is None else str(estado),
            }

            data.append(item)

    finally:
        conn.close()

    return render_template(
        "module.html",
        page="atletas",
        title="Atletas",
        subtitle="Cadastro e histórico dos atletas do SGFE",
        columns=[
            "IDCliente",
            "Atleta",
            "Sexo",
            "AnoNascimento",
            "Equipe",
            "Modalidade",
            "Telefone",
            "Contato",
            "Cidade",
            "Estado"
        ],
        data=data,
        search=search,
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
            caminho = os.path.join(_tenant_balizamento_folder(), nome_final)
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

            # PostgreSQL migrado: a coluna IDBalizamento pode ter vindo do Access
            # como NOT NULL, mas sem DEFAULT/sequence. No Access o AUTOINCREMENT
            # preenchia esse campo sozinho. Aqui geramos os IDs explicitamente.
            cur.execute("SELECT COALESCE(MAX(IDBalizamento::bigint), 0) FROM tblBalizamentoProvas")
            proximo_id = int(cur.fetchone()[0] or 0) + 1

            for item in registros:
                cur.execute("""
                    INSERT INTO tblBalizamentoProvas
                    (IDBalizamento, IDEvento, NumeroProva, NomeProva, NumeroSerie, Raia, NomeAtleta, Registro, Pagina)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, [
                    proximo_id, evento_id, item["prova"], item["nome_prova"], item["serie"],
                    item["raia"], item["nome_atleta"], item["registro"], item["pagina"]
                ])
                proximo_id += 1

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
    """
    Abre o PDF ORIGINAL do balizamento e acrescenta somente as marcações
    fotográficas. O layout, páginas, tabelas e textos do PDF original
    permanecem intactos.
    """
    if fitz is None:
        return "PyMuPDF não está instalado. Instale com: pip install pymupdf", 500

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT IDEvento, NomeEvento, DataEvento, Cidade,
                   BalizamentoArquivo, BalizamentoCaminho
            FROM tblEvento
            WHERE IDEvento=?
        """, [evento_id])
        ev = cur.fetchone()
        if not ev:
            return "Evento não encontrado", 404

        arquivo_nome = str(ev[4] or "")
        caminho_pdf = str(ev[5] or "")

        # Compatibilidade com registros antigos: procura o PDF salvo na pasta.
        if not caminho_pdf or not os.path.isfile(caminho_pdf):
            if arquivo_nome and os.path.isdir(_tenant_balizamento_folder()):
                candidatos = []
                alvo = arquivo_nome.lower()
                for nome in os.listdir(_tenant_balizamento_folder()):
                    if nome.lower().endswith(".pdf") and nome.lower().endswith(alvo):
                        candidatos.append(os.path.join(_tenant_balizamento_folder(), nome))
                if candidatos:
                    caminho_pdf = max(candidatos, key=os.path.getmtime)

        if not caminho_pdf or not os.path.isfile(caminho_pdf):
            return redirect(url_for(
                "web_agendamentos",
                erro="O arquivo PDF original do balizamento não foi encontrado."
            ))

        # Todas as marcações fotográficas ativas deste evento.
        cur.execute("""
            SELECT M.IDMarcacao, M.IDCliente, C.Nome AS Atleta,
                   M.NumeroProva, M.NumeroSerie, M.Raia, M.Cor, M.NomeProva
            FROM tblMarcacaoBalizamento AS M
            LEFT JOIN tblClientes AS C ON M.IDCliente=C.IDCliente
            WHERE M.IDEvento=? AND M.Ativa=True
            ORDER BY M.NumeroProva, M.NumeroSerie, M.IDMarcacao
        """, [evento_id])
        marcacoes = cur.fetchall()

        # Localiza cada ocorrência no PDF original usando a página registrada
        # na importação. Assim, a marcação cai exatamente na linha original.
        cur.execute("""
            SELECT NumeroProva, NumeroSerie, Raia, NomeAtleta, Pagina
            FROM tblBalizamentoProvas
            WHERE IDEvento=?
        """, [evento_id])
        ocorrencias = cur.fetchall()

        ocorrencia_map = {}
        for r in ocorrencias:
            chave = (
                access_int(r[0]), access_int(r[1]), access_int(r[2]),
                _normalizar_nome(r[3])
            )
            ocorrencia_map[chave] = access_int(r[4])

        doc = fitz.open(caminho_pdf)

        def encontrar_linha(page, atleta, serie, raia):
            """Retorna o retângulo da linha original onde o atleta aparece."""
            nome_norm = _normalizar_nome(atleta)
            words = page.get_text("words") or []
            linhas = {}

            for w in words:
                if len(w) < 5:
                    continue
                x0, y0, x1, y1, texto = w[:5]
                # Agrupa palavras que pertencem à mesma linha visual.
                chave_y = round(float(y0) / 2.0) * 2.0
                linhas.setdefault(chave_y, []).append(w)

            candidatos = []
            for _, ws in linhas.items():
                ws = sorted(ws, key=lambda z: z[0])
                texto_linha = " ".join(str(z[4]) for z in ws)
                if not nome_norm or nome_norm not in _normalizar_nome(texto_linha):
                    continue

                numeros = []
                for z in ws:
                    txt = str(z[4]).strip()
                    if txt.isdigit():
                        numeros.append(access_int(txt))

                serie_ok = access_int(serie) in numeros
                raia_ok = (not raia) or (access_int(raia) in numeros)
                score = (2 if serie_ok else 0) + (2 if raia_ok else 0)
                candidatos.append((score, min(z[1] for z in ws), ws))

            if candidatos:
                candidatos.sort(key=lambda x: (-x[0], x[1]))
                ws = candidatos[0][2]
                return fitz.Rect(
                    min(z[0] for z in ws),
                    min(z[1] for z in ws),
                    max(z[2] for z in ws),
                    max(z[3] for z in ws),
                )

            hits = page.search_for(str(atleta)) if atleta else []
            return hits[0] if hits else None

        for m in marcacoes:
            atleta = str(m[2] or "").strip()
            prova = access_int(m[3])
            serie = access_int(m[4])
            raia = access_int(m[5])
            cor = str(m[6] or "amarelo").strip().lower()

            pagina = ocorrencia_map.get(
                (prova, serie, raia, _normalizar_nome(atleta)), 0
            )
            if not pagina:
                continue

            idx = pagina - 1
            if idx < 0 or idx >= len(doc):
                continue

            page = doc[idx]
            rect = encontrar_linha(page, atleta, serie, raia)
            if not rect:
                continue

            # Não recria a tabela. Apenas pinta discretamente a linha existente.
            margem_y = max(2.0, min(5.0, rect.height * 0.35))
            faixa = fitz.Rect(
                1,
                max(0, rect.y0 - margem_y),
                page.rect.width - 1,
                min(page.rect.height, rect.y1 + margem_y),
            )

            if cor == "azul-claro":
                rgb = (0.35, 0.75, 0.95)
            else:
                rgb = (1.00, 0.82, 0.10)

            # Fundo translúcido + pequena faixa lateral. O conteúdo original
            # continua sendo o conteúdo principal do PDF.
            page.draw_rect(
                faixa,
                color=rgb,
                fill=rgb,
                width=1.0,
                stroke_opacity=0.75,
                fill_opacity=0.18,
                overlay=True,
            )
            page.draw_rect(
                fitz.Rect(1, faixa.y0, 5, faixa.y1),
                color=rgb,
                fill=rgb,
                width=0,
                stroke_opacity=1,
                fill_opacity=0.95,
                overlay=True,
            )

        pdf_bytes = doc.tobytes(garbage=4, deflate=True)
        doc.close()

        base = os.path.splitext(os.path.basename(arquivo_nome))[0]
        nome_saida = f"{base or 'balizamento_evento_' + str(evento_id)}_COM_MARCACOES.pdf"

        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=False,
            download_name=nome_saida,
        )
    finally:
        conn.close()



@app.route("/agendamentos", methods=["GET", "POST"])
def web_agendamentos():
    """
    V16 - SOMENTE AGENDAMENTOS.

    Correção: joins entre IDs do PostgreSQL são comparados como TEXT,
    pois a migração possui alguns IDs com tipos diferentes (varchar/integer).

    Painel e Atletas permanecem intocados.
    No PostgreSQL, esta rota usa psycopg2 diretamente, sem passar pela
    camada de compatibilidade que estava produzindo o "list index out of range".
    """
    mensagem = ""
    erro = ""
    busca = request.args.get("q", "").strip()
    filtro_evento = request.args.get("evento", "").strip()
    filtro_status = request.args.get("status", "").strip()

    data = []
    atletas = []
    eventos = []
    status = []
    equipes_cadastro = []
    sexos_cadastro = []
    modalidades_cadastro = []

    conn = None
    cur = None

    try:
        # =========================================================
        # POSTGRESQL: conexão direta e cursor normal
        # =========================================================
        if _is_postgres():
            if not DATABASE_URL:
                raise RuntimeError("DATABASE_URL não foi configurada no Render.")

            schema = "public"
            if session.get("perfil") == "FOTOGRAFO" and session.get("fotografo_id"):
                schema = _tenant_db_path(session["fotografo_id"])

            conn = psycopg2.connect(DATABASE_URL, connect_timeout=15)
            conn.autocommit = False
            cur = conn.cursor()

            if schema == "public":
                cur.execute('SET search_path TO "public"')
            else:
                cur.execute(f'SET search_path TO "{schema}", "public"')

            # -----------------------------------------------------
            # SALVAR NOVO AGENDAMENTO
            # -----------------------------------------------------
            if request.method == "POST":
                id_cliente = request.form.get("id_cliente", "").strip()
                id_evento = request.form.get("id_evento", "").strip()
                observacoes = request.form.get("observacoes") or ""

                if not id_cliente:
                    raise ValueError("Selecione um atleta.")
                if not id_evento:
                    raise ValueError("Selecione um evento.")

                # IDs da migração PostgreSQL são INTEGER.
                try:
                    id_cliente_int = int(id_cliente)
                    id_evento_int = int(id_evento)
                except ValueError:
                    raise ValueError("Atleta ou evento inválido.")

                cur.execute("""
                    SELECT idstatusagendamento
                    FROM tblstatusagendamento
                    WHERE UPPER(statusagendamento)='AGENDADO'
                    ORDER BY idstatusagendamento
                    LIMIT 1
                """)
                status_row = cur.fetchone()
                if status_row is None:
                    raise ValueError("O status AGENDADO não foi encontrado no SGFE.")

                id_status = access_int(status_row[0])

                cur.execute("""
                    SELECT a.idagendamento, COALESCE(s.statusagendamento,'')
                    FROM tblagendamentos a
                    LEFT JOIN tblstatusagendamento s
                      ON a.idstatusagendamento::text=s.idstatusagendamento::text
                    WHERE a.idcliente::text=%s::text AND a.idevento::text=%s::text
                    ORDER BY a.idagendamento DESC
                """, (id_cliente_int, id_evento_int))

                for ex in cur.fetchall():
                    status_antigo = str(ex[1] or "").upper()
                    if "CANCEL" not in status_antigo:
                        raise ValueError(
                            f"Este atleta já possui o agendamento #{ex[0]} "
                            "para este evento."
                        )

                # IDAgendamento não possui geração automática no PostgreSQL.
                # Gera o próximo ID preservando os registros existentes.
                cur.execute("""
                    SELECT COALESCE(MAX(idagendamento), 0) + 1
                    FROM tblagendamentos
                """)
                novo_id_agendamento = int(cur.fetchone()[0])

                cur.execute("""
                    INSERT INTO tblagendamentos
                    (idagendamento, dataagendamento, idcliente, idevento,
                     idstatusagendamento, observacoes, ultimaatualizacao)
                    VALUES (%s,CURRENT_TIMESTAMP,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                """, (
                    novo_id_agendamento,
                    id_cliente_int,
                    id_evento_int,
                    id_status,
                    observacoes,
                ))

                conn.commit()
                mensagem = "Agendamento realizado com sucesso."

            # -----------------------------------------------------
            # LISTA DE AGENDAMENTOS
            # -----------------------------------------------------
            sql = """
                SELECT
                    a.idagendamento,
                    a.dataagendamento,
                    c.idcliente,
                    c.nome AS atleta,
                    COALESCE(c.telefone,'') AS telefone,
                    COALESCE(c.contato,'') AS contato,
                    COALESCE(sx.sexo,'') AS sexo,
                    e.idevento,
                    e.nomeevento AS evento,
                    e.dataevento,
                    COALESCE(e.cidade,'') AS cidade,
                    COALESCE(e.balizamentoarquivo,'') AS balizamentoarquivo,
                    sa.idstatusagendamento,
                    COALESCE(sa.statusagendamento,'') AS status,
                    COALESCE(a.observacoes,'') AS observacoes,
                    a.idvendas
                FROM tblagendamentos a
                LEFT JOIN tblclientes c ON a.idcliente::text=c.idcliente::text
                LEFT JOIN tblevento e ON a.idevento::text=e.idevento::text
                LEFT JOIN tblstatusagendamento sa
                  ON a.idstatusagendamento::text=sa.idstatusagendamento::text
                LEFT JOIN tblsexos sx ON c.idsexo::text=sx.idsexo::text
                WHERE (a.idvendas IS NULL OR a.idvendas::text='0')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM tblvendapacotes v
                      WHERE v.idagendamento::text=a.idagendamento::text
                  )
                  AND (
                      sa.statusagendamento IS NULL
                      OR UPPER(sa.statusagendamento) NOT LIKE '%%CANCEL%%'
                  )
            """
            params = []

            if busca:
                sql += """
                    AND (
                        c.nome ILIKE %s
                        OR COALESCE(c.telefone,'') ILIKE %s
                        OR COALESCE(c.contato,'') ILIKE %s
                        OR e.nomeevento ILIKE %s
                    )
                """
                like = "%" + busca + "%"
                params.extend([like, like, like, like])

            if filtro_evento:
                try:
                    params.append(int(filtro_evento))
                    sql += " AND a.idevento::text=%s::text"
                except ValueError:
                    sql += " AND 1=0"

            if filtro_status:
                try:
                    params.append(int(filtro_status))
                    sql += " AND a.idstatusagendamento::text=%s::text"
                except ValueError:
                    sql += " AND 1=0"

            sql += " ORDER BY a.dataagendamento ASC, a.idagendamento ASC"

            cur.execute(sql, tuple(params))
            rows = cur.fetchall()

            # Mapeamento explícito. Nenhum acesso variável por índice.
            for row in rows:
                item = {
                    "IDAgendamento": row[0],
                    "DataAgendamento": row[1],
                    "IDCliente": row[2],
                    "Atleta": row[3] or "",
                    "Telefone": row[4] or "",
                    "Contato": row[5] or "",
                    "Sexo": row[6] or "",
                    "IDEvento": row[7],
                    "Evento": row[8] or "",
                    "DataEvento": row[9],
                    "Cidade": row[10] or "",
                    "BalizamentoArquivo": row[11] or "",
                    "IDStatusAgendamento": row[12],
                    "Status": row[13] or "",
                    "Observacoes": row[14] or "",
                    "IDVendas": row[15],
                }

                for campo in ("DataAgendamento", "DataEvento"):
                    if hasattr(item[campo], "strftime"):
                        item[campo] = item[campo].strftime("%d/%m/%Y")
                    elif item[campo] is None:
                        item[campo] = ""

                data.append(item)

            # -----------------------------------------------------
            # ATLETAS DO AGENDAMENTO
            # -----------------------------------------------------
            cur.execute("""
                SELECT c.idcliente,
                       COALESCE(c.nome,''),
                       COALESCE(eq.nomeequipe,''),
                       COALESCE(sx.sexo,'')
                FROM tblclientes c
                LEFT JOIN tblequipes eq ON c.idequipe::text=eq.idequipe::text
                LEFT JOIN tblsexos sx ON c.idsexo::text=sx.idsexo::text
                ORDER BY c.nome
            """)
            for r in cur.fetchall():
                atletas.append({
                    "id": r[0],
                    "nome": r[1] or "",
                    "equipe": r[2] or "",
                    "sexo": r[3] or ""
                })

            # -----------------------------------------------------
            # EVENTOS
            # -----------------------------------------------------
            cur.execute("""
                SELECT idevento, COALESCE(nomeevento,''),
                       dataevento, COALESCE(cidade,'')
                FROM tblevento
                ORDER BY dataevento ASC, nomeevento
            """)
            for r in cur.fetchall():
                data_evento = r[2]
                eventos.append({
                    "id": r[0],
                    "nome": r[1] or "",
                    "data": data_evento.strftime("%d/%m/%Y")
                        if hasattr(data_evento, "strftime")
                        else str(data_evento or ""),
                    "cidade": r[3] or ""
                })

            # -----------------------------------------------------
            # STATUS
            # -----------------------------------------------------
            cur.execute("""
                SELECT idstatusagendamento, COALESCE(statusagendamento,'')
                FROM tblstatusagendamento
                ORDER BY idstatusagendamento
            """)
            for r in cur.fetchall():
                status.append({"id": r[0], "nome": r[1] or ""})

            # -----------------------------------------------------
            # DADOS DO CADASTRO RÁPIDO DE ATLETA
            # -----------------------------------------------------
            cur.execute("""
                SELECT idequipe, COALESCE(nomeequipe,''),
                       COALESCE(cidade,''), COALESCE(estado,'')
                FROM tblequipes
                ORDER BY nomeequipe
            """)
            for r in cur.fetchall():
                equipes_cadastro.append({
                    "id": r[0],
                    "nome": r[1] or "",
                    "cidade": r[2] or "",
                    "estado": r[3] or ""
                })

            cur.execute("""
                SELECT idsexo, COALESCE(sexo,'')
                FROM tblsexos
                ORDER BY sexo
            """)
            for r in cur.fetchall():
                sexos_cadastro.append({"id": r[0], "nome": r[1] or ""})

            cur.execute("""
                SELECT idmodalidade, COALESCE(modalidade,'')
                FROM tblmodalidades
                ORDER BY modalidade
            """)
            for r in cur.fetchall():
                modalidades_cadastro.append({"id": r[0], "nome": r[1] or ""})

        # =========================================================
        # ACCESS / SQLITE
        # Mantém o comportamento anterior fora do Render.
        # =========================================================
        else:
            conn = get_connection()
            cur = conn.cursor()

            if request.method == "POST":
                id_cliente = access_int(request.form.get("id_cliente") or 0)
                id_evento = access_int(request.form.get("id_evento") or 0)
                observacoes = request.form.get("observacoes") or ""

                cur.execute(
                    "SELECT TOP 1 IDStatusAgendamento "
                    "FROM tblStatusAgendamento "
                    "WHERE UCASE(StatusAgendamento)='AGENDADO'"
                )
                status_row = cur.fetchone()
                if not status_row:
                    raise ValueError("O status AGENDADO não foi encontrado no SGFE.")

                id_status = access_int(status_row[0])

                if not id_cliente:
                    raise ValueError("Selecione um atleta.")
                if not id_evento:
                    raise ValueError("Selecione um evento.")

                cur.execute("""
                    SELECT A.IDAgendamento, S.StatusAgendamento
                    FROM tblAgendamentos AS A
                    LEFT JOIN tblStatusAgendamento AS S
                      ON A.IDStatusAgendamento=S.IDStatusAgendamento
                    WHERE A.IDCliente=? AND A.IDEvento=?
                    ORDER BY A.IDAgendamento DESC
                """, [id_cliente, id_evento])

                for ex in cur.fetchall():
                    if "CANCEL" not in str(ex[1] or "").upper():
                        raise ValueError(
                            f"Este atleta já possui o agendamento #{ex[0]} "
                            "para este evento."
                        )

                cur.execute("""
                    INSERT INTO tblAgendamentos
                    (DataAgendamento, IDCliente, IDEvento,
                     IDStatusAgendamento, Observacoes, UltimaAtualizacao)
                    VALUES (Now(),?,?,?,?,Now())
                """, [id_cliente, id_evento, id_status, observacoes])
                conn.commit()
                mensagem = "Agendamento realizado com sucesso."

            cur.execute("""
                SELECT
                    A.IDAgendamento,A.DataAgendamento,C.IDCliente,C.Nome,
                    C.Telefone,C.Contato,X.Sexo,E.IDEvento,E.NomeEvento,
                    E.DataEvento,E.Cidade,E.BalizamentoArquivo,
                    S.IDStatusAgendamento,S.StatusAgendamento,
                    A.Observacoes,A.IDVendas
                FROM ((((tblAgendamentos A
                LEFT JOIN tblClientes C ON A.IDCliente=C.IDCliente)
                LEFT JOIN tblEvento E ON A.IDEvento=E.IDEvento)
                LEFT JOIN tblStatusAgendamento S
                  ON A.IDStatusAgendamento=S.IDStatusAgendamento)
                LEFT JOIN tblSexos X ON C.IDSexo=X.IDSexo)
                WHERE (A.IDVendas Is Null OR A.IDVendas=0)
                  AND NOT EXISTS (
                      SELECT V.IDVenda FROM tblVendaPacotes V
                      WHERE V.IDAgendamento=A.IDAgendamento
                  )
                  AND (S.StatusAgendamento Is Null
                       OR UCase(S.StatusAgendamento) NOT LIKE '%CANCEL%')
            """)
            for row in cur.fetchall():
                item = {
                    "IDAgendamento": row[0], "DataAgendamento": row[1],
                    "IDCliente": row[2], "Atleta": row[3] or "",
                    "Telefone": row[4] or "", "Contato": row[5] or "",
                    "Sexo": row[6] or "", "IDEvento": row[7],
                    "Evento": row[8] or "", "DataEvento": row[9],
                    "Cidade": row[10] or "", "BalizamentoArquivo": row[11] or "",
                    "IDStatusAgendamento": row[12], "Status": row[13] or "",
                    "Observacoes": row[14] or "", "IDVendas": row[15]
                }
                for campo in ("DataAgendamento", "DataEvento"):
                    if hasattr(item[campo], "strftime"):
                        item[campo] = item[campo].strftime("%d/%m/%Y")
                    elif item[campo] is None:
                        item[campo] = ""
                data.append(item)

            cur.execute("""
                SELECT C.IDCliente,C.Nome,E.NomeEquipe,X.Sexo
                FROM ((tblClientes C
                LEFT JOIN tblEquipes E ON C.IDEquipe=E.IDEquipe)
                LEFT JOIN tblSexos X ON C.IDSexo=X.IDSexo)
                ORDER BY C.Nome
            """)
            for r in cur.fetchall():
                atletas.append({
                    "id": r[0], "nome": r[1] or "",
                    "equipe": r[2] or "", "sexo": r[3] or ""
                })

            cur.execute("""
                SELECT IDEvento,NomeEvento,DataEvento,Cidade
                FROM tblEvento ORDER BY DataEvento ASC,NomeEvento
            """)
            for r in cur.fetchall():
                eventos.append({
                    "id": r[0], "nome": r[1] or "",
                    "data": r[2].strftime("%d/%m/%Y")
                        if hasattr(r[2], "strftime") else str(r[2] or ""),
                    "cidade": r[3] or ""
                })

            cur.execute("""
                SELECT IDStatusAgendamento,StatusAgendamento
                FROM tblStatusAgendamento ORDER BY IDStatusAgendamento
            """)
            for r in cur.fetchall():
                status.append({"id": r[0], "nome": r[1] or ""})

            cur.execute("""
                SELECT IDEquipe,NomeEquipe,Cidade,Estado
                FROM tblEquipes ORDER BY NomeEquipe
            """)
            for r in cur.fetchall():
                equipes_cadastro.append({
                    "id": r[0], "nome": r[1] or "",
                    "cidade": r[2] or "", "estado": r[3] or ""
                })

            cur.execute("SELECT IDSexo,Sexo FROM tblSexos ORDER BY Sexo")
            for r in cur.fetchall():
                sexos_cadastro.append({"id": r[0], "nome": r[1] or ""})

            cur.execute("SELECT IDModalidade,Modalidade FROM tblModalidades ORDER BY Modalidade")
            for r in cur.fetchall():
                modalidades_cadastro.append({"id": r[0], "nome": r[1] or ""})

    except Exception as e:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass

        erro = str(e)
        print(f"[SGFE-AGENDAMENTOS-V16] ERRO: {e}", flush=True)
        print(f"[SGFE-AGENDAMENTOS-V16] tipo: {type(e).__name__}", flush=True)

    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    return render_template(
        "agendamentos.html",
        page="agendamentos",
        title="Agendamentos",
        subtitle="Agenda completa do SGFE",
        columns=[],
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
        WHERE CAST(IdEvento AS TEXT)=CAST(? AS TEXT) ORDER BY QtdProvas
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
    data = []
    columns = [
        "IDVenda", "DataVenda", "Atleta", "Evento", "QtdProvas",
        "ValorPacote", "ValorDesconto", "ValorFinal", "Status"
    ]

    def _txt(v):
        if v is None:
            return ""
        if hasattr(v, "strftime"):
            return v.strftime("%d/%m/%Y")
        return str(v)

    def _num(v):
        """Normaliza IDs somente nesta tela, incluindo bytea/memoryview migrado."""
        if v is None:
            return 0

        # psycopg2 pode devolver BYTEA como bytes ou memoryview.
        if isinstance(v, memoryview):
            v = v.tobytes()

        if isinstance(v, (bytes, bytearray)):
            raw = bytes(v).rstrip(b"\\x00")
            if not raw:
                return 0

            # Bytea textual/hex, caso algum registro tenha sido importado
            # como texto em vez de BYTEA nativo.
            try:
                txt = raw.decode("ascii").strip()
                if txt.isdigit():
                    return int(txt)
                if txt.lower().startswith("\\x"):
                    hx = txt[2:]
                    if hx and len(hx) % 2 == 0:
                        return int.from_bytes(bytes.fromhex(hx), "little", signed=False)
            except Exception:
                pass

            # IDs antigos do Access gravados em binário.
            return int.from_bytes(raw, byteorder="little", signed=False)

        if isinstance(v, str):
            # IDs migrados como um único caractere: o code point é o ID.
            # Ex.: '4'=52, '5'=53, '\\t'=9, '\\x1f'=31, 'ć'=263.
            if len(v) == 1:
                return ord(v)

            s = v.strip()
            if not s:
                return 0

            try:
                return int(s)
            except Exception:
                pass

            # PostgreSQL pode entregar um BYTEA textual como "\\x08".
            if s.lower().startswith("\\x"):
                try:
                    hx = s[2:]
                    if hx and len(hx) % 2 == 0:
                        return int.from_bytes(bytes.fromhex(hx), "little", signed=False)
                except Exception:
                    pass

            # String contendo os bytes binários já decodificados.
            try:
                raw = s.encode("latin1").rstrip(b"\\x00")
                if raw:
                    return int.from_bytes(raw, byteorder="little", signed=False)
            except Exception:
                pass

            return 0

        try:
            return int(v)
        except Exception:
            return 0

    try:
        cur = conn.cursor()

        # Eventos. Os IDs são normalizados em Python, exatamente como na
        # listagem de ATLETAS, sem tentar fazer conversões do Access dentro
        # do SQL do PostgreSQL.
        cur.execute("""
            SELECT IDEvento, NomeEvento, DataEvento, Cidade, Ativo
            FROM tblEvento
            ORDER BY DataEvento DESC, IDEvento DESC
        """)
        evento_map = {}
        for r in cur.fetchall():
            # IMPORTANTE: o filtro antigo que funcionava usava access_int().
            # Mantemos isso aqui para o ID escolhido no combo ser o mesmo ID
            # usado em tblVendaPacotes.
            eid = access_int(r[0])
            evento_map[eid] = {
                "id": eid,
                "nome": _txt(r[1]),
                "data": _txt(r[2]),
                "cidade": _txt(r[3]),
                "ativo": bool(r[4]) if r[4] is not None else False,
            }
        eventos = list(evento_map.values())

        if evento_param:
            try:
                # Mesma conversão da versão em que o filtro funcionava.
                candidato = access_int(evento_param)
            except Exception:
                candidato = 0
            if candidato in evento_map:
                evento_selecionado = candidato
        elif eventos:
            evento_selecionado = eventos[0]["id"]

        # Clientes e agendamentos são carregados somente nesta tela.
        # Além do ID normalizado, guardamos a representação original do ID.
        # Isso é importante porque alguns registros antigos migrados do Access
        # chegaram ao PostgreSQL como texto contendo bytes/hex.
        def _id_chaves(v):
            chaves = []
            if v is None:
                return chaves

            # Chave textual original, para casos em que o banco trouxe o ID
            # como string diferente da representação numérica.
            try:
                bruto = str(v).strip()
                if bruto:
                    chaves.append(("raw", bruto))
            except Exception:
                pass

            n = _num(v)
            if n:
                chaves.append(("num", n))
            return chaves

        cur.execute("SELECT IDCliente, Nome FROM tblClientes")
        cliente_map = {}
        cliente_raw_map = {}
        for r in cur.fetchall():
            valor_id = r[0]
            nome = _txt(r[1]).strip()
            if not nome:
                continue
            for tipo, chave in _id_chaves(valor_id):
                if tipo == "num":
                    cliente_map[chave] = nome
                else:
                    cliente_raw_map[chave] = nome

        # A venda nasce de um agendamento. O vínculo mais direto e seguro para
        # recuperar o atleta de uma venda antiga é tblAgendamentos.IDVendas ->
        # tblVendaPacotes.IDVenda. Só depois usamos IDAgendamento e IDCliente
        # como compatibilidade para registros antigos. Nada é gravado/alterado.
        cur.execute("SELECT IDVendas, IDAgendamento, IDCliente FROM tblAgendamentos")
        agendamento_venda_cliente_map = {}
        agendamento_venda_raw_map = {}
        agendamento_cliente_map = {}
        agendamento_cliente_raw_map = {}
        for r in cur.fetchall():
            idv_raw, aid_raw, cid_raw = r[0], r[1], r[2]
            idv = _num(idv_raw)
            aid = _num(aid_raw)
            cid = _num(cid_raw)

            if idv:
                agendamento_venda_cliente_map[idv] = cid
            for tipo, chave in _id_chaves(idv_raw):
                if tipo == "raw":
                    agendamento_venda_raw_map[chave] = cid_raw

            if aid:
                agendamento_cliente_map[aid] = cid
            for tipo, chave in _id_chaves(aid_raw):
                if tipo == "raw":
                    agendamento_cliente_raw_map[chave] = cid_raw

        def _resolver_cliente(valor_id):
            # Resolve o cliente pelo ID real da venda/agendamento.
            # A consulta direta é a fonte de verdade para a exibição do nome,
            # evitando depender de mapas que podem conter representações
            # diferentes dos IDs antigos migrados.
            cid = _num(valor_id)
            if cid:
                try:
                    cur.execute("SELECT Nome FROM tblClientes WHERE IDCliente=? LIMIT 1", [cid])
                    row_cliente = cur.fetchone()
                    if row_cliente and row_cliente[0]:
                        nome = str(row_cliente[0]).strip()
                        if nome:
                            return nome, cid
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass

            # Compatibilidade com representações antigas/textuais.
            if cid and cid in cliente_map:
                return cliente_map[cid], cid

            try:
                bruto = str(valor_id).strip()
            except Exception:
                bruto = ""
            if bruto and bruto in cliente_raw_map:
                return cliente_raw_map[bruto], cid

            return "", cid

        def _num_status(v):
            """Normaliza IDStatusVenda especificamente.

            Diferente de _num() (usada para cliente/evento/venda, onde um
            caractere único pode ser um código binário migrado do Access),
            aqui um caractere único que já é um dígito ("1", "2", "3"...)
            deve ser lido pelo valor literal, porque é assim que a tela de
            edição de venda grava o status escolhido no formulário. Só cai
            na decodificação por code point quando não é um número válido.
            """
            if v is None:
                return 0
            if isinstance(v, memoryview):
                v = v.tobytes()
            if isinstance(v, (bytes, bytearray)):
                raw = bytes(v).rstrip(b"\x00")
                if not raw:
                    return 0
                try:
                    txt = raw.decode("ascii").strip()
                    if txt.isdigit():
                        return int(txt)
                except Exception:
                    pass
                return int.from_bytes(raw, byteorder="little", signed=False)
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    return 0
                try:
                    return int(s)
                except Exception:
                    pass
                if len(s) == 1:
                    return ord(s)
                return 0
            try:
                return int(v)
            except Exception:
                return 0

        cur.execute("SELECT IDStatusVenda, StatusVenda FROM tblStatusVenda")
        status_map = {_num_status(r[0]): _txt(r[1]) for r in cur.fetchall()}

        # A tela VENDAS mostrava o status a partir de um campo manual
        # (IDStatusVenda), separado do valor realmente pago (tblPagamento).
        # Isso desincronizava sempre que um pagamento era registrado por
        # outra tela (Pagamentos/Entregas) sem que alguém lembrasse de
        # também atualizar esse campo manual. Agora PAGA/PARCIAL/ABERTA são
        # calculados a partir do valor pago de verdade — só CANCELADA e
        # CORTESIA continuam vindo do campo manual, pois não dá pra
        # calculá-los só com base no valor.
        cur.execute("SELECT IDVenda, Sum(ValorPago) FROM tblPagamento GROUP BY IDVenda")
        pagamentos_map = {}
        for r2 in cur.fetchall():
            try:
                pagamentos_map[_num(r2[0])] = float(r2[1] or 0)
            except Exception:
                pass

        nomes_status_existentes = {}
        for _v in status_map.values():
            nomes_status_existentes.setdefault(_v.strip().upper(), _v)
        nome_status_paga = nomes_status_existentes.get("PAGA") or nomes_status_existentes.get("PAGO") or "Paga"
        nome_status_parcial = nomes_status_existentes.get("PARCIAL") or "Parcial"
        nome_status_aberta = nomes_status_existentes.get("ABERTA") or "Aberta"
        nome_status_cancelada = nomes_status_existentes.get("CANCELADA") or "Cancelada"
        nome_status_cortesia = nomes_status_existentes.get("CORTESIA") or "Cortesia"

        # A venda é lida diretamente, sem JOIN por IDs e SEM WHERE IDEvento.
        # Esta é a mesma estratégia usada pela tela ENTREGAS, que já está
        # funcionando corretamente. No PostgreSQL, IDEvento é character varying
        # e alguns registros antigos podem ter representações diferentes.
        # Por isso o filtro é feito em Python, depois de normalizar o ID.
        cur.execute("""
            SELECT
                IDVenda,
                DataVenda,
                IDCliente,
                IDEvento,
                QtdProvas,
                ValorPacote,
                ValorDesconto,
                ValorFinal,
                IDStatusVenda,
                IDAgendamento,
                Finalizado,
                DataFinalizacao,
                StatusPagamento
            FROM tblVendaPacotes
            ORDER BY IDVenda DESC
        """)

        for r in cur.fetchall():
            venda_id = _num(r[0])
            cliente_id = _num(r[2])
            evento_id = _num(r[3])
            status_id = _num_status(r[8])

            # FILTRO DE EVENTO: exatamente como na tela ENTREGAS.
            # Só compara depois que IDEvento foi normalizado em Python.
            if evento_selecionado and evento_id != evento_selecionado:
                continue

            atleta, cliente_id = _resolver_cliente(r[2])

            # FONTE PRINCIPAL PARA A LISTAGEM DE VENDAS:
            # a venda foi criada a partir de um agendamento e o agendamento
            # guarda o ID da venda em IDVendas. Portanto, quando o IDCliente
            # gravado na venda antiga não consegue localizar o nome, cruzamos
            # primeiro VENDA -> AGENDAMENTO -> CLIENTE. Isso resolve somente a
            # exibição do nome e não altera nenhuma regra nem nenhum registro.
            if not atleta and venda_id:
                cliente_id_ag_venda = agendamento_venda_cliente_map.get(venda_id, 0)
                if cliente_id_ag_venda:
                    atleta = cliente_map.get(cliente_id_ag_venda, "")
                    if atleta:
                        cliente_id = cliente_id_ag_venda

            # Mesmo vínculo acima, preservando a representação textual original
            # caso IDVendas tenha sido migrado como texto/bytea.
            if not atleta and venda_id:
                venda_id_txt = str(r[0]).strip() if r[0] is not None else ""
                if venda_id_txt:
                    cid_raw_venda = agendamento_venda_raw_map.get(venda_id_txt)
                    if cid_raw_venda is not None:
                        atleta, cliente_id = _resolver_cliente(cid_raw_venda)

            # Fallback de compatibilidade: se ainda não encontrou pelo vínculo
            # da própria venda, tenta o IDAgendamento registrado na venda.
            id_agendamento_raw = r[9]
            id_agendamento = _num(id_agendamento_raw)
            if not atleta and id_agendamento:
                cliente_id_ag = agendamento_cliente_map.get(id_agendamento, 0)
                if cliente_id_ag:
                    atleta = cliente_map.get(cliente_id_ag, "")
                    if atleta:
                        cliente_id = cliente_id_ag

            # Último fallback para agendamentos cujo ID também foi migrado como
            # texto: usa a representação original do IDAgendamento.
            if not atleta:
                try:
                    aid_raw_txt = str(id_agendamento_raw).strip()
                except Exception:
                    aid_raw_txt = ""
                if aid_raw_txt:
                    cid_raw_ag = agendamento_cliente_raw_map.get(aid_raw_txt)
                    if cid_raw_ag is not None:
                        atleta, cliente_id = _resolver_cliente(cid_raw_ag)

            # ÚLTIMA RECUPERAÇÃO PARA OS REGISTROS MIGRADOS:
            # quando o PostgreSQL recebeu os IDs antigos do Access em formatos
            # diferentes, a conversão em Python pode não reproduzir exatamente
            # o valor textual armazenado. Nesse caso cruzamos diretamente no
            # PostgreSQL o ID da venda com tblAgendamentos.IDVendas e, depois,
            # o ID do cliente com tblClientes.IDCliente.
            #
            # Esta etapa é SOMENTE leitura para montar a coluna ATLETA.
            if not atleta and venda_id and _is_postgres():
                try:
                    cur.execute("""
                        SELECT A.IDCliente
                        FROM tblAgendamentos AS A
                        WHERE CAST(A.IDVendas AS TEXT)=CAST(? AS TEXT)
                        ORDER BY A.IDAgendamento DESC
                        LIMIT 1
                    """, [venda_id])
                    ag_row = cur.fetchone()

                    if ag_row:
                        cid_raw = ag_row[0]
                        atleta, cliente_id = _resolver_cliente(cid_raw)

                        if not atleta:
                            cur.execute("""
                                SELECT Nome
                                FROM tblClientes
                                WHERE CAST(IDCliente AS TEXT)=CAST(? AS TEXT)
                                LIMIT 1
                            """, [cid_raw])
                            cliente_row = cur.fetchone()
                            if cliente_row and cliente_row[0]:
                                atleta = str(cliente_row[0]).strip()
                except Exception:
                    pass

            # Se o vínculo IDVendas não existir em registros antigos, tenta
            # diretamente o IDAgendamento gravado na venda, também sem depender
            # da conversão Python do ID.
            if not atleta and id_agendamento_raw is not None and _is_postgres():
                try:
                    cur.execute("""
                        SELECT A.IDCliente
                        FROM tblAgendamentos AS A
                        WHERE CAST(A.IDAgendamento AS TEXT)=CAST(? AS TEXT)
                        ORDER BY A.IDAgendamento DESC
                        LIMIT 1
                    """, [id_agendamento_raw])
                    ag_row = cur.fetchone()

                    if ag_row:
                        cid_raw = ag_row[0]
                        atleta, cliente_id = _resolver_cliente(cid_raw)

                        if not atleta:
                            cur.execute("""
                                SELECT Nome
                                FROM tblClientes
                                WHERE CAST(IDCliente AS TEXT)=CAST(? AS TEXT)
                                LIMIT 1
                            """, [cid_raw])
                            cliente_row = cur.fetchone()
                            if cliente_row and cliente_row[0]:
                                atleta = str(cliente_row[0]).strip()
                except Exception:
                    pass

            if not atleta:
                print(f"[SGFE-VENDAS] atleta_nao_localizado venda={venda_id} IDCliente={r[2]!r} IDAgendamento={r[9]!r}")

            evento_nome = evento_map.get(evento_id, {}).get("nome", "")

            qtd = _num(r[4])
            valor_pacote = r[5] if r[5] is not None else 0
            valor_desconto = r[6] if r[6] is not None else 0
            valor_final = r[7] if r[7] is not None else 0
            finalizado = r[10]

            status_bruto = status_map.get(status_id, "")
            status_bruto_upper = status_bruto.strip().upper()
            status_pagamento_bruto = str(r[12] or "").strip().lower()

            venda_cancelada = "CANCEL" in status_bruto_upper or status_pagamento_bruto == "cancelado"
            venda_cortesia = "CORTESIA" in status_bruto_upper or status_pagamento_bruto == "cortesia"

            if venda_cancelada:
                status_nome = status_bruto if "CANCEL" in status_bruto_upper else nome_status_cancelada
            elif venda_cortesia:
                status_nome = status_bruto if "CORTESIA" in status_bruto_upper else nome_status_cortesia
            else:
                pago = pagamentos_map.get(venda_id, 0.0)
                valor_num = float(valor_final or 0)
                if valor_num > 0 and pago >= valor_num - 0.009:
                    status_nome = nome_status_paga
                elif pago > 0:
                    status_nome = nome_status_parcial
                else:
                    status_nome = nome_status_aberta

            # Cancelada/cortesia não participam do fechamento financeiro do
            # evento: aparecem na lista, mas com valor zerado.
            if venda_cancelada or venda_cortesia:
                valor_pacote = 0
                valor_desconto = 0
                valor_final = 0

            # Aliases em maiúsculo e minúsculo para manter compatibilidade
            # com o template atual, sem alterar o template nem outras telas.
            item = {
                "IDVenda": venda_id,
                "id": venda_id,
                "OS": venda_id,
                "os": venda_id,
                "DataVenda": _txt(r[1]),
                "Data": _txt(r[1]),
                "data": _txt(r[1]),
                "Atleta": atleta,
                "atleta": atleta,
                "Nome": atleta,
                "nome": atleta,
                "Evento": evento_nome,
                "evento": evento_nome,
                "NomeEvento": evento_nome,
                "QtdProvas": qtd,
                "Provas": qtd,
                "provas": qtd,
                "ValorPacote": valor_pacote,
                "Pacote": valor_pacote,
                "pacote": valor_pacote,
                "ValorDesconto": valor_desconto,
                "Desconto": valor_desconto,
                "desconto": valor_desconto,
                "ValorFinal": valor_final,
                "valor_final": valor_final,
                "Status": status_nome,
                "status": status_nome,
                "IDStatusVenda": status_id,
                "IDAgendamento": id_agendamento,
                "Finalizado": finalizado,
                "finalizado": finalizado,
                "DataFinalizacao": _txt(r[11]),
                "data_finalizacao": _txt(r[11]),
            }

            if search:
                alvo = " ".join([
                    str(atleta), str(evento_nome), str(status_nome),
                    str(venda_id), str(qtd)
                ]).lower()
                if search.lower() not in alvo:
                    continue

            data.append(item)

        print(f"[SGFE-VENDAS] evento_selecionado={evento_selecionado} vendas_exibidas={len(data)}")
        if data:
            print(f"[SGFE-VENDAS] primeira_venda={data[0].get('IDVenda')} atleta={data[0].get('Atleta')} evento={data[0].get('Evento')}")

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        erro = str(e)
        data = []
        print(f"[SGFE-VENDAS] ERRO_LISTAGEM={e}")
    finally:
        conn.close()

    html = render_template(
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

    # Ajuste visual da tela Vendas / Pacotes.
    # A legenda sai da coluna lateral e vira uma linha própria abaixo
    # do título do filtro. Nenhuma regra de negócio ou campo é alterado.
    ajuste_vendas = r"""
<style id="sgfe-vendas-filtro-enquadrado">
/* AJUSTE FINAL DA TELA VENDAS/PACOTES
   Não reduz fontes. Não altera regras de negócio.
   Apenas elimina o contador lateral e reorganiza os espaços. */

.sgfe-vendas-legenda-linha{
    display:block !important;
    width:100% !important;
    grid-column:1 / -1 !important;
    flex-basis:100% !important;
    margin:2px 0 14px 0 !important;
    line-height:1.35 !important;
    box-sizing:border-box !important;
}

.sgfe-vendas-filtro-grid{
    width:100% !important;
    max-width:100% !important;
    box-sizing:border-box !important;
}

/* Depois que o contador lateral é retirado, a tabela ocupa todo o card. */
.sgfe-vendas-tabela-expandida{
    width:100% !important;
    max-width:100% !important;
    box-sizing:border-box !important;
}

/* Impede que os containers empurrem os botões para fora da tela. */
/* AJUSTE PONTUAL SOLICITADO
   Não altera o card/filtro de vendas.
   Não altera tabela, banco ou regras.
   Apenas remove o bloco lateral "Evento selecionado:"
   e mantém "Vendas / Pacotes" em uma única linha. */

.sgfe-vendas-evento-direita-remover{
    display:none !important;
}

/* SOMENTE NA TELA /vendas: reserva de espaço do MENU LATERAL.
   Não altera os demais menus nem o botão individual. */
.sgfe-vendas-sidebar-281{
    width:281px !important;
    min-width:281px !important;
    max-width:281px !important;
    flex:0 0 281px !important;
    box-sizing:border-box !important;
}

/* CORREÇÃO DE ENQUADRAMENTO DA TELA /vendas
   Mantém a fonte original e o menu com 281px, mas impede que a coluna
   principal crie uma largura maior que a viewport. O conteúdo interno
   pode se ajustar ou rolar dentro do próprio bloco, sem empurrar a página. */
html, body{
    width:100% !important;
    max-width:100% !important;
    overflow-x:hidden !important;
    box-sizing:border-box !important;
}

*, *::before, *::after{
    box-sizing:border-box;
}

.layout{
    width:100% !important;
    max-width:100% !important;
    min-width:0 !important;
    grid-template-columns:281px minmax(0,1fr) !important;
    overflow:hidden !important;
}

.layout > .sidebar{
    width:281px !important;
    min-width:281px !important;
    max-width:281px !important;
    flex:0 0 281px !important;
    box-sizing:border-box !important;
}

.layout > .main,
.main{
    width:100% !important;
    max-width:100% !important;
    min-width:0 !important;
    box-sizing:border-box !important;
    overflow-x:hidden !important;
}

/* O topo nunca ultrapassa a área útil. Os botões podem quebrar de linha,
   sem reduzir a fonte. */
.page-actions{
    width:100% !important;
    max-width:100% !important;
    min-width:0 !important;
    box-sizing:border-box !important;
    display:flex !important;
    flex-wrap:wrap !important;
    gap:10px !important;
}

.sgfe-vendas-top-actions{
    width:100% !important;
    max-width:100% !important;
    min-width:0 !important;
    box-sizing:border-box !important;
    display:flex !important;
    flex-wrap:wrap !important;
    align-items:center !important;
    gap:10px !important;
}

.sgfe-vendas-top-actions > *{
    flex:0 0 auto !important;
    max-width:100% !important;
}

.sgfe-vendas-layout-281{
    width:100% !important;
    max-width:100% !important;
    min-width:0 !important;
    grid-template-columns:281px minmax(0,1fr) !important;
}

/* Filtro ocupa somente a largura realmente disponível.
   A fonte não é reduzida. */
.sgfe-vendas-filtro-grid{
    width:100% !important;
    max-width:100% !important;
    min-width:0 !important;
    box-sizing:border-box !important;
    overflow:hidden !important;
}

.sgfe-vendas-filtro-grid input,
.sgfe-vendas-filtro-grid select,
.sgfe-vendas-filtro-grid button{
    min-width:0 !important;
    max-width:100% !important;
    box-sizing:border-box !important;
}

/* A tabela continua com fonte normal e rolagem somente dentro da tabela. */
.sgfe-vendas-tabela-expandida{
    width:100% !important;
    max-width:100% !important;
    min-width:0 !important;
    box-sizing:border-box !important;
    overflow-x:auto !important;
    overflow-y:hidden !important;
}

.sgfe-vendas-top-actions{
    max-width:100% !important;
    box-sizing:border-box !important;
    display:flex !important;
    flex-wrap:wrap !important;
    align-items:center !important;
    gap:10px !important;
}

.sgfe-vendas-top-actions > *{
    flex:0 0 auto !important;
}

@media (max-width:1100px){
    .sgfe-vendas-filtro-grid{
        grid-template-columns:minmax(150px,.45fr) minmax(220px,1fr) minmax(220px,1fr) auto !important;
    }
}

/* AJUSTE FINAL DO CABEÇALHO DA TELA VENDAS/PACOTES
   Somente posicionamento. Não reduz fonte. */
.module-top{
    width:100% !important;
    max-width:100% !important;
    min-width:0 !important;
    display:flex !important;
    flex-direction:row !important;
    align-items:flex-end !important;
    justify-content:space-between !important;
    flex-wrap:nowrap !important;
    gap:18px !important;
    box-sizing:border-box !important;
}

.module-top > div:first-child{
    min-width:0 !important;
    flex:1 1 auto !important;
}

.module-top h1{
    white-space:nowrap !important;
    margin:5px 0 !important;
}

.module-top p{
    white-space:nowrap !important;
    margin:0 !important;
}

.module-top .actions{
    margin-left:auto !important;
    flex:0 0 auto !important;
    display:flex !important;
    justify-content:flex-end !important;
    align-items:center !important;
    white-space:nowrap !important;
}

.module-top .action-btn{
    flex:0 0 auto !important;
    white-space:nowrap !important;
    width:180px !important;
    height:44px !important;
    min-width:180px !important;
    padding:0 18px !important;
    margin:0 24px 0 0 !important;
    display:inline-flex !important;
    align-items:center !important;
    justify-content:center !important;
    box-sizing:border-box !important;
}
/* AJUSTE PONTUAL: ENQUADRAMENTO DA TABELA DE VENDAS/PACOTES
   Somente faz a tabela caber na largura disponível.
   Não altera filtro, dados, rotas ou regras de negócio. */
.sgfe-vendas-tabela-expandida table{
    width:100% !important;
    max-width:100% !important;
    min-width:0 !important;
    table-layout:fixed !important;
    box-sizing:border-box !important;
}

.sgfe-vendas-tabela-expandida table th,
.sgfe-vendas-tabela-expandida table td{
    min-width:0 !important;
    max-width:none !important;
    overflow:hidden !important;
    text-overflow:ellipsis !important;
}

/* AJUSTE PONTUAL: reduz os espaços horizontais entre as colunas.
   Mantém espaço suficiente para ATLETA e EVENTO. */
.sgfe-vendas-tabela-expandida table th:nth-child(1),
.sgfe-vendas-tabela-expandida table td:nth-child(1){ width:60px !important; }
.sgfe-vendas-tabela-expandida table th:nth-child(2),
.sgfe-vendas-tabela-expandida table td:nth-child(2){ width:90px !important; }
.sgfe-vendas-tabela-expandida table th:nth-child(3),
.sgfe-vendas-tabela-expandida table td:nth-child(3){ width:205px !important; }
.sgfe-vendas-tabela-expandida table th:nth-child(4),
.sgfe-vendas-tabela-expandida table td:nth-child(4){ width:220px !important; }
.sgfe-vendas-tabela-expandida table th:nth-child(5),
.sgfe-vendas-tabela-expandida table td:nth-child(5){ width:65px !important; }
.sgfe-vendas-tabela-expandida table th:nth-child(6),
.sgfe-vendas-tabela-expandida table td:nth-child(6){ width:100px !important; }
.sgfe-vendas-tabela-expandida table th:nth-child(7),
.sgfe-vendas-tabela-expandida table td:nth-child(7){ width:100px !important; }
.sgfe-vendas-tabela-expandida table th:nth-child(8),
.sgfe-vendas-tabela-expandida table td:nth-child(8){ width:115px !important; }
.sgfe-vendas-tabela-expandida table th:nth-child(9),
.sgfe-vendas-tabela-expandida table td:nth-child(9){ width:110px !important; }
.sgfe-vendas-tabela-expandida table th:nth-child(10),
.sgfe-vendas-tabela-expandida table td:nth-child(10){ width:110px !important; }

.sgfe-vendas-tabela-expandida table th:nth-child(4),
.sgfe-vendas-tabela-expandida table td:nth-child(4){
    white-space:nowrap !important;
    overflow:hidden !important;
    text-overflow:clip !important;
}

.sgfe-vendas-tabela-expandida table th:last-child,
.sgfe-vendas-tabela-expandida table td:last-child{
    width:120px !important;
}

.sgfe-vendas-tabela-expandida table td:last-child .action-btn{
    min-width:92px !important;
    width:92px !important;
    padding:0 8px !important;
}

</style>

<script>
(function(){
    'use strict';

    function texto(el){
        return (el && (el.innerText || el.textContent) || '').replace(/\s+/g,' ').trim();
    }

    function removerSomenteEventoDireita(){
        /* Esconde somente o elemento que contém diretamente
           "Evento selecionado:". Não sobe para o card. */
        document.querySelectorAll('body *').forEach(function(el){
            const t=texto(el);
            if(!/^Evento selecionado:/i.test(t)) return;

            const filhoComFrase=Array.from(el.children).some(function(c){
                return /^Evento selecionado:/i.test(texto(c));
            });
            if(filhoComFrase) return;

            /* O bloco lateral é curto. O card inteiro do filtro não é. */
            if(t.length <= 220){
                el.classList.add('sgfe-vendas-evento-direita-remover');
            }
        });
    }

    function ajustarSomenteMenuVendasPacotes(){
        /* O espaço do menu já é definido diretamente pelo CSS acima,
           exclusivamente na resposta da rota /vendas. */
        return;
    }

    function removerContadorVendas(){
        /* Remove SOMENTE "0 venda(s) encontrada(s)" ou equivalente. */
        const regex=/^\d+\s+venda\(s\)\s+encontrada\(s\)$/i;

        document.querySelectorAll('body *').forEach(function(el){
            const t=texto(el);
            if(!regex.test(t)) return;

            const filhoComMesmoTexto=Array.from(el.children).some(function(c){
                return regex.test(texto(c));
            });

            if(!filhoComMesmoTexto){
                el.remove();
            }
        });
    }

    function organizarTabela(){
        const table=document.querySelector('table');
        if(!table) return;

        /* VENDAS/PACOTES: remove somente a coluna DATA FINALIZAÇÃO
           e acrescenta o botão EDITAR. A rota /vendas/<id>/editar já existe
           no backend. Nenhuma regra do filtro ou dos dados é alterada. */
        const headRow=table.tHead ? table.tHead.rows[0] : table.querySelector('thead tr');
        if(headRow && !table.dataset.sgfeEdicaoAcoes){
            const headers=Array.from(headRow.cells);
            let indiceDataFinalizacao=-1;
            let indiceFinalizado=-1;
            let indiceData=-1;
            let indiceAcaoAntiga=-1;
            headers.forEach(function(cell, i){
                const t=texto(cell).toUpperCase();
                if(t==='DATA FINALIZAÇÃO' || t==='DATA FINALIZACAO') indiceDataFinalizacao=i;
                if(t==='FINALIZADO') indiceFinalizado=i;
                if(t==='DATA') indiceData=i;
                if(t==='AÇÃO' || t==='ACAO') indiceAcaoAntiga=i;
            });

            if(indiceDataFinalizacao>=0){
                Array.from(table.rows).forEach(function(row){
                    if(row.cells[indiceDataFinalizacao]) row.cells[indiceDataFinalizacao].style.display='none';
                });
            }

            /* Retira somente a coluna FINALIZADO da visualização da tabela.
               O campo continua existindo no banco e nas regras internas do
               sistema, para não alterar nenhuma função já funcionando. */
            if(indiceFinalizado>=0){
                Array.from(table.rows).forEach(function(row){
                    if(row.cells[indiceFinalizado]) row.cells[indiceFinalizado].style.display='none';
                });
            }

            /* Retira somente a coluna DATA da visualização para liberar espaço
               para EVENTO e para o botão EDITAR. Nenhuma regra de dados é alterada. */
            if(indiceData>=0){
                Array.from(table.rows).forEach(function(row){
                    if(row.cells[indiceData]) row.cells[indiceData].style.display='none';
                });
            }

            /* Remove somente a coluna AÇÃO antiga, deixando apenas o novo EDITAR
               na coluna AÇÕES. */
            if(indiceAcaoAntiga>=0){
                Array.from(table.rows).forEach(function(row){
                    if(row.cells[indiceAcaoAntiga]) row.cells[indiceAcaoAntiga].style.display='none';
                });
            }

            const th=document.createElement('th');
            th.textContent='AÇÕES';
            th.style.whiteSpace='nowrap';
            headRow.appendChild(th);

            const rows=Array.from(table.tBodies).flatMap(function(tb){ return Array.from(tb.rows); });
            rows.forEach(function(row){
                const td=document.createElement('td');
                td.style.whiteSpace='nowrap';

                let id='';
                const primeira=row.cells[0];
                if(primeira){
                    const m=texto(primeira).match(/\d+/);
                    if(m) id=m[0];
                }

                if(id){
                    const a=document.createElement('a');
                    a.href='/vendas/'+encodeURIComponent(id)+'/editar';
                    a.textContent='✎ EDITAR';
                    a.className='action-btn';
                    a.style.cssText='display:inline-flex!important;align-items:center;justify-content:center;white-space:nowrap;text-decoration:none;width:auto;min-width:92px;height:38px;padding:0 14px;margin:0;box-sizing:border-box;';
                    td.appendChild(a);
                }
                row.appendChild(td);
            });

            table.dataset.sgfeEdicaoAcoes='1';
        }

        let box=table.parentElement;
        while(box && box!==document.body){
            const cs=getComputedStyle(box);
            if(cs.overflowX==='auto' || cs.overflowX==='scroll' ||
               box.classList.contains('table-responsive')){
                break;
            }
            box=box.parentElement;
        }

        if(!box || box===document.body) box=table.parentElement;
        if(!box) return;

        box.classList.add('sgfe-vendas-tabela-expandida');
        box.style.maxWidth='100%';
        box.style.boxSizing='border-box';
        box.style.overflowX='auto';

        table.style.boxSizing='border-box';
    }

    function organizarBotoes(){
        const nomes=['NOVA','MENU PRINCIPAL','VOLTAR','←'];

        document.querySelectorAll('body a, body button').forEach(function(el){
            const t=texto(el).toUpperCase();
            if(!t) return;

            const alvo=nomes.some(function(nome){
                return t===nome || (nome==='NOVA' && t.indexOf('NOVA')>=0);
            });
            if(!alvo) return;

            let pai=el.parentElement;
            let melhor=null;

            for(let i=0; pai && pai!==document.body && i<6; i++,pai=pai.parentElement){
                const cs=getComputedStyle(pai);
                if(cs.display==='flex' || cs.display==='grid'){
                    melhor=pai;
                    break;
                }
            }

            if(melhor) melhor.classList.add('sgfe-vendas-top-actions');
        });
    }

    function ajustarVendas(){
        const frase='Escolha o evento para visualizar somente as vendas/pacotes dele.';
        const evento=document.querySelector('select[name="evento"]');
        if(!evento) return;

        /* FILTRO DE EVENTO: a troca do evento precisa efetivamente
           recarregar /vendas com ?evento=ID.
           Mantemos a pesquisa q, quando existir.
           Não altera os nomes dos atletas nem qualquer dado da venda. */
        /* FILTRO DE EVENTO - versão robusta.
           O listener é delegado no document para continuar funcionando mesmo
           se algum script reconstruir o <select> depois do carregamento.
           A mudança também é colocada em onchange como segunda camada. */
        function aplicarFiltroEvento(el){
            if(!el) return;
            const valor=String(el.value || '').trim();
            const url=new URL(window.location.href);
            url.pathname='/vendas';
            if(valor && valor !== '0'){
                url.searchParams.set('evento',valor);
            }else{
                url.searchParams.delete('evento');
            }
            const q=url.searchParams.get('q');
            if(q) url.searchParams.set('q',q);
            else url.searchParams.delete('q');
            window.location.assign(url.toString());
        }

        if(!window.__sgfeFiltroEventoDelegado){
            window.__sgfeFiltroEventoDelegado=true;
            document.addEventListener('change',function(e){
                const alvo=e.target;
                if(alvo && alvo.matches && alvo.matches('select[name="evento"]')){
                    e.stopPropagation();
                    aplicarFiltroEvento(alvo);
                }
            },true);
        }

        if(!evento.dataset.sgfeFiltroEventoAtivo){
            evento.dataset.sgfeFiltroEventoAtivo='1';
            evento.onchange=function(){ aplicarFiltroEvento(this); };
        }

        removerContadorVendas();

        /* Mantém a legenda como linha abaixo dos controles. */
        let legenda=null;

        document.querySelectorAll('body *').forEach(function(el){
            if(legenda) return;

            const t=texto(el);
            if(t.indexOf(frase)!==-1 && t.length<500){
                const filho=Array.from(el.children).some(function(c){
                    return texto(c).indexOf(frase)!==-1;
                });
                if(!filho) legenda=el;
            }
        });

        let bloco=evento.parentElement;

        while(bloco && bloco!==document.body){
            if(bloco.querySelector('select[name="evento"]') &&
               (bloco.classList.contains('card') ||
                bloco.querySelector('input[name="q"]') ||
                bloco.querySelector('button'))) break;
            bloco=bloco.parentElement;
        }

        if(!bloco || bloco===document.body){
            bloco=evento.closest('.card') || evento.parentElement.parentElement;
        }

        if(bloco){
            let grid=evento.parentElement;

            while(grid && grid!==bloco && grid!==document.body){
                const cs=getComputedStyle(grid);
                if(cs.display==='grid' || cs.display==='flex') break;
                grid=grid.parentElement;
            }

            if(!grid || grid===document.body) grid=bloco;

            grid.classList.add('sgfe-vendas-filtro-grid');

            if(legenda){
                if(legenda.parentElement!==grid) grid.appendChild(legenda);
                legenda.classList.add('sgfe-vendas-legenda-linha');
            }

            if(getComputedStyle(grid).display==='grid'){
                const colunas=getComputedStyle(grid).gridTemplateColumns.split(' ').length;

                if(colunas>=4){
                    grid.style.gridTemplateColumns=
                        'minmax(150px,.45fr) minmax(220px,1fr) minmax(260px,1.1fr) auto';
                }
            }
        }

        organizarTabela();
        organizarBotoes();
    }

    function iniciar(){
        ajustarVendas();
        removerSomenteEventoDireita();
        ajustarSomenteMenuVendasPacotes();
        setTimeout(ajustarVendas,120);
        setTimeout(ajustarVendas,500);
        setTimeout(removerSomenteEventoDireita,300);
        setTimeout(ajustarSomenteMenuVendasPacotes,300);
        setTimeout(removerSomenteEventoDireita,800);
        setTimeout(ajustarSomenteMenuVendasPacotes,800);
    }

    if(document.readyState==='loading'){
        document.addEventListener('DOMContentLoaded',iniciar);
    }else{
        iniciar();
    }
})();
</script>
"""
    if '</body>' in html:
        html=html.replace('</body>', ajuste_vendas+'</body>', 1)
    else:
        html += ajuste_vendas

    return html


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
    dados = {"eventos": 0, "modalidades": 0, "equipes": 0, "despesas": 0, "total_despesas": 0.0, "fotografos": 0}
    erro = ""
    try:
        cur = conn.cursor()
        dados["eventos"] = access_int(scalar(cur, "SELECT Count(*) FROM tblEvento", 0))
        dados["modalidades"] = access_int(scalar(cur, "SELECT Count(*) FROM tblModalidades", 0))
        dados["equipes"] = access_int(scalar(cur, "SELECT Count(*) FROM tblEquipes", 0))
        dados["despesas"] = access_int(scalar(cur, "SELECT Count(*) FROM tblDespesasEvento", 0))
        dados["total_despesas"] = money(scalar(cur, "SELECT Sum(ValorDespesa) FROM tblDespesasEvento", 0))
        dados["fotografos"] = access_int(scalar(cur, "SELECT Count(*) FROM tblFotografos", 0))
    except Exception as e:
        erro = str(e)
    finally:
        conn.close()
    return render_template("configuracoes.html", dados=dados, erro=erro)


# ==========================
# FOTÓGRAFOS
# ==========================
@app.route("/fotografos", methods=["GET", "POST"])
def web_fotografos():
    if session.get("perfil") != "ADMIN":
        return redirect(url_for("index"))
    conn = get_connection()
    erro = ""
    mensagem = request.args.get("ok", "")
    fotografos = []
    try:
        cur = conn.cursor()
        if request.method == "POST":
            nome = request.form.get("nome", "").strip()
            celular = request.form.get("celular", "").strip()
            login = request.form.get("login", "").strip()
            senha = request.form.get("senha", "")
            if not nome or not celular or not login or not senha:
                raise ValueError("Preencha nome, celular, login e senha.")

            cur.execute("SELECT TOP 1 IDFotografo FROM tblFotografos WHERE UCASE([Login])=UCASE(?)", [login])
            if cur.fetchone():
                raise ValueError("Esse login já está cadastrado.")

            # tblFotografos é uma tabela migrada do Access e IDFotografo não
            # tem auto-incremento (sequence) configurado no PostgreSQL. Sem
            # isso, o INSERT tentava gravar o campo vazio e violava a
            # restrição NOT NULL. Geramos o próximo ID aqui, como já é
            # feito para tblPagamento e tblBalizamentoProvas.
            cur.execute("SELECT COALESCE(MAX(IDFotografo::bigint), 0) FROM tblFotografos")
            novo_id_fotografo = access_int(cur.fetchone()[0]) + 1

            senha_hash = generate_password_hash(senha)
            cur.execute("""
                INSERT INTO tblFotografos (IDFotografo, Nome, Celular, [Login], SenhaHash, PainelLiberado, DataCadastro, Ativo)
                VALUES (?, ?, ?, ?, ?, True, Now(), True)
            """, [novo_id_fotografo, nome, celular, login, senha_hash])
            conn.commit()
            # Fecha o banco principal antes de criar a cópia privada, evitando
            # que o arquivo .accdb fique bloqueado pelo ODBC no Windows.
            conn.close()
            conn = None
            # O cadastro de acesso fica no banco do administrador;
            # os dados operacionais ficam em um banco separado por fotógrafo.
            _criar_banco_fotografo(novo_id_fotografo)
            return redirect(url_for("web_fotografos", ok="Fotógrafo cadastrado com sucesso."))

        cur.execute("""
            SELECT IDFotografo, Nome, Celular, [Login], PainelLiberado
            FROM tblFotografos
            ORDER BY Nome
        """)
        fotografos = [
            {"id": access_int(r[0]), "nome": r[1] or "", "celular": r[2] or "", "login": r[3] or "", "liberado": bool(r[4])}
            for r in cur.fetchall()
        ]
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        erro = str(e)
    finally:
        conn.close()
    return render_template("fotografos.html", fotografos=fotografos, mensagem=mensagem, erro=erro)


@app.route("/fotografos/<int:id_fotografo>/editar", methods=["GET", "POST"])
def editar_fotografo(id_fotografo):
    if session.get("perfil") != "ADMIN":
        return redirect(url_for("index"))
    conn = get_connection()
    erro = ""
    mensagem = ""
    try:
        cur = conn.cursor()

        if request.method == "POST":
            nome = request.form.get("nome", "").strip()
            celular = request.form.get("celular", "").strip()
            login = request.form.get("login", "").strip()
            senha = request.form.get("senha", "")

            if not nome or not celular or not login:
                raise ValueError("Preencha nome, celular e login.")

            cur.execute(
                "SELECT TOP 1 IDFotografo FROM tblFotografos WHERE UCASE([Login])=UCASE(?) AND IDFotografo<>?",
                [login, id_fotografo]
            )
            if cur.fetchone():
                raise ValueError("Esse login já está cadastrado para outro fotógrafo.")

            if senha:
                senha_hash = generate_password_hash(senha)
                cur.execute("""
                    UPDATE tblFotografos
                    SET Nome=?, Celular=?, [Login]=?, SenhaHash=?
                    WHERE IDFotografo=?
                """, [nome, celular, login, senha_hash, id_fotografo])
            else:
                cur.execute("""
                    UPDATE tblFotografos
                    SET Nome=?, Celular=?, [Login]=?
                    WHERE IDFotografo=?
                """, [nome, celular, login, id_fotografo])

            if cur.rowcount == 0:
                raise ValueError("Fotógrafo não encontrado.")

            conn.commit()
            return redirect(url_for("web_fotografos", ok="Fotógrafo alterado com sucesso."))

        cur.execute("""
            SELECT IDFotografo, Nome, Celular, [Login], PainelLiberado
            FROM tblFotografos
            WHERE IDFotografo=?
        """, [id_fotografo])
        r = cur.fetchone()
        if not r:
            return redirect(url_for("web_fotografos", erro="Fotógrafo não encontrado."))

        fotografo = {
            "id": access_int(r[0]),
            "nome": r[1] or "",
            "celular": r[2] or "",
            "login": r[3] or "",
            "liberado": bool(r[4])
        }
        return render_template("editar_fotografo.html", fotografo=fotografo, erro=erro)

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        erro = str(e)
        if request.method == "POST":
            try:
                cur.execute("""
                    SELECT IDFotografo, Nome, Celular, [Login], PainelLiberado
                    FROM tblFotografos WHERE IDFotografo=?
                """, [id_fotografo])
                r = cur.fetchone()
                fotografo = {
                    "id": access_int(r[0]), "nome": r[1] or "", "celular": r[2] or "",
                    "login": r[3] or "", "liberado": bool(r[4])
                } if r else {"id": id_fotografo, "nome": "", "celular": "", "login": "", "liberado": False}
            except Exception:
                fotografo = {"id": id_fotografo, "nome": "", "celular": "", "login": "", "liberado": False}
            return render_template("editar_fotografo.html", fotografo=fotografo, erro=erro), 400
        return redirect(url_for("web_fotografos", erro=erro))
    finally:
        conn.close()


@app.route("/fotografos/<int:id_fotografo>/bloquear", methods=["POST"])
def bloquear_fotografo(id_fotografo):
    if session.get("perfil") != "ADMIN":
        return redirect(url_for("index"))
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE tblFotografos SET PainelLiberado=False WHERE IDFotografo=?", [id_fotografo])
        conn.commit()
        return redirect(url_for("web_fotografos", ok="Painel do fotógrafo bloqueado."))
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return redirect(url_for("web_fotografos", erro=str(e)))
    finally:
        conn.close()


@app.route("/fotografos/<int:id_fotografo>/liberar", methods=["POST"])
def liberar_fotografo(id_fotografo):
    if session.get("perfil") != "ADMIN":
        return redirect(url_for("index"))
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE tblFotografos SET PainelLiberado=True WHERE IDFotografo=?", [id_fotografo])
        conn.commit()
        return redirect(url_for("web_fotografos", ok="Painel do fotógrafo liberado."))
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return redirect(url_for("web_fotografos", erro=str(e)))
    finally:
        conn.close()


@app.route("/fotografo/login", methods=["GET", "POST"])
def fotografo_login():
    # Compatibilidade: o fotógrafo usa o mesmo login do SGFE.
    return redirect(url_for("login", next=request.values.get("next", "")))


@app.route("/fotografo/painel")
def fotografo_painel():
    # Compatibilidade com links antigos: fotógrafo trabalha no SGFE principal.
    if session.get("fotografo_id"):
        return redirect(url_for("index"))
    return redirect(url_for("login", next="/"))


@app.route("/fotografo/logout")
def fotografo_logout():
    return logout()


@app.route("/fotografo/senha", methods=["GET", "POST"])
def fotografo_alterar_senha():
    # Autoatendimento: o próprio fotógrafo troca a senha da conta dele,
    # pedindo a senha atual por segurança. Não depende do perfil ADMIN.
    id_fotografo = session.get("fotografo_id")
    if not id_fotografo:
        return redirect(url_for("login", next="/fotografo/senha"))

    erro = ""
    mensagem = request.args.get("ok", "")

    if request.method == "POST":
        conn = get_connection()
        try:
            cur = conn.cursor()
            senha_atual = request.form.get("senha_atual", "")
            nova_senha = request.form.get("nova_senha", "")
            confirmar_senha = request.form.get("confirmar_senha", "")

            if not senha_atual or not nova_senha or not confirmar_senha:
                raise ValueError("Preencha a senha atual e a nova senha (com confirmação).")
            if len(nova_senha) < 4:
                raise ValueError("A nova senha precisa ter pelo menos 4 caracteres.")
            if nova_senha != confirmar_senha:
                raise ValueError("A confirmação não bate com a nova senha.")

            cur.execute("SELECT SenhaHash FROM tblFotografos WHERE IDFotografo=?", [id_fotografo])
            r = cur.fetchone()
            if not r:
                raise ValueError("Fotógrafo não encontrado.")

            if not check_password_hash(str(r[0] or ""), senha_atual):
                raise ValueError("Senha atual incorreta.")

            nova_senha_hash = generate_password_hash(nova_senha)
            cur.execute("UPDATE tblFotografos SET SenhaHash=? WHERE IDFotografo=?", [nova_senha_hash, id_fotografo])
            conn.commit()
            return redirect(url_for("fotografo_alterar_senha", ok="Senha alterada com sucesso."))
        except Exception as e:
            try: conn.rollback()
            except Exception: pass
            erro = str(e)
        finally:
            conn.close()

    return render_template("fotografo_senha.html", erro=erro, mensagem=mensagem)


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
        # Mesmo quando a validação do formulário falha (ex.: nome vazio),
        # a lista de modalidades já cadastradas deve continuar aparecendo.
        # Antes, esse erro acontecia ANTES da consulta que monta essa
        # lista, então ela ficava vazia mesmo já existindo modalidades.
        try:
            cur = conn.cursor()
            cur.execute("SELECT IDModalidade, Modalidade FROM tblModalidades ORDER BY Modalidade")
            modalidades = [{"id": access_int(r[0]), "nome": r[1] or ""} for r in cur.fetchall()]
        except Exception:
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
    # PostgreSQL sempre devolve os nomes das colunas em minúsculo, mesmo
    # que a tabela tenha sido criada com maiúsculas. Normalizamos aqui
    # para minúsculo, e as comparações feitas com esta lista (mais abaixo)
    # também usam minúsculo — isso funciona igual no Access, cujos
    # identificadores também não diferenciam maiúsculas de minúsculas.
    return [d[0].lower() if isinstance(d[0], str) else d[0] for d in cur.description]


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
                "descricao": descricao, "descrição": descricao, "descricaodespesa": descricao,
                "valordespesa": valor, "valor": valor,
                "datadespesa": data, "data": data,
                "idevento": access_int(evento_id),
                "observacoes": observacoes, "observações": observacoes
            }
            campos, valores = [], []
            for c in colunas_db:
                if c in mapa and mapa[c] not in (None, ""):
                    campos.append(c)
                    valores.append(mapa[c])

            if "idevento" not in campos:
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
        # IMPORTANTE: NÃO filtrar por D.IDEvento no SQL comparando com um
        # número — mesmo problema de tipos (varchar x integer) já visto e
        # corrigido em várias outras telas. O filtro é feito em Python,
        # depois de normalizar o ID.
        eid_filtro = access_int(evento_filtro) if evento_filtro else 0

        # Consulta amigável, sem expor IDs internos como a tela antiga fazia.
        descricao_col = next((c for c in ["descricao", "descrição", "descricaodespesa"] if c in colunas_db), None)
        valor_col = next((c for c in ["valordespesa", "valor"] if c in colunas_db), None)
        data_col = next((c for c in ["datadespesa", "data"] if c in colunas_db), None)
        obs_col = next((c for c in ["observacoes", "observações"] if c in colunas_db), None)

        if not descricao_col or not valor_col or not data_col or "idevento" not in colunas_db:
            raise ValueError("A tabela de despesas precisa ter Descricao, ValorDespesa, DataDespesa e IDEvento.")

        obs_sql = f", D.[{obs_col}]" if obs_col else ", Null"
        sql = f"""
            SELECT D.[{descricao_col}], D.[{data_col}], E.NomeEvento, E.Cidade,
                   D.[{valor_col}], D.IDEvento{obs_sql}
            FROM tblDespesasEvento AS D
            LEFT JOIN tblEvento AS E ON D.IDEvento=E.IDEvento
            ORDER BY D.[{data_col}] DESC
        """
        cur.execute(sql)
        rows = [r for r in cur.fetchall() if not eid_filtro or access_int(r[5]) == eid_filtro]

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

        total = money(sum(r["valor"] for r in data))

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
        # O PostgreSQL sempre devolve os nomes das colunas em minúsculo,
        # mesmo com a consulta escrita em maiúsculas. Usar esses nomes
        # (via cur.description) para montar cada linha, enquanto o
        # template recebe a lista abaixo em maiúsculas, fazia a busca
        # item["IDEvento"] não encontrar item["idevento"] — resultando em
        # células vazias mesmo com os registros existindo. Aqui usamos
        # diretamente a mesma lista (na mesma ordem do SELECT) para as duas
        # coisas.
        columns=["IDEvento","NomeEvento","DataEvento","Cidade","Modalidade","Ativo"]
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
            evento_info = next((e for e in eventos if e["id"] == evento_selecionado), None)
            if evento_info:
                # IMPORTANTE: NÃO comparar/juntar IDEvento (character varying,
                # herança da migração do Access) direto no SQL com um número
                # ou outra tabela. Mesmo problema de tipos já visto e
                # corrigido em VENDAS, PAGAMENTOS, PAGAMENTOS PENDENTES,
                # ENTREGAS e HISTÓRICO — aqui ele deixava a conexão numa
                # transação abortada, e a consulta seguinte quebrava com um
                # erro genérico ("list index out of range") em vez do erro
                # real. Cada tabela é lida por completo e cruzada em Python.

                cur.execute("SELECT IDEvento FROM tblAgendamentos")
                resumo["agendamentos"] = sum(
                    1 for r2 in cur.fetchall() if access_int(r2[0]) == evento_selecionado
                )

                cur.execute("SELECT IDStatusVenda, StatusVenda FROM tblStatusVenda")
                status_nome_map = {access_int(r2[0]): str(r2[1] or "") for r2 in cur.fetchall()}

                cur.execute("""
                    SELECT IDVenda, ValorFinal, Finalizado, StatusPagamento,
                           IDStatusVenda, IDEvento
                    FROM tblVendaPacotes
                """)
                vendas_evento = [
                    r2 for r2 in cur.fetchall()
                    if access_int(r2[5]) == evento_selecionado
                    and "CANCEL" not in status_nome_map.get(access_int(r2[4]), "").upper()
                    and str(r2[3] or "").strip().lower() != "cancelado"
                ]
                resumo["vendas"] = len(vendas_evento)
                # Cortesia não participa do fechamento financeiro do evento:
                # entra na contagem de vendas, mas com valor zerado.
                resumo["total_vendido"] = sum(
                    float(r2[1] or 0) for r2 in vendas_evento
                    if str(r2[3] or "").strip().lower() != "cortesia"
                )
                resumo["entregas_pendentes"] = sum(1 for r2 in vendas_evento if r2[2] is None or not bool(r2[2]))

                cur.execute("SELECT IDVenda, Sum(ValorPago) FROM tblPagamento GROUP BY IDVenda")
                pagamentos = {}
                for r2 in cur.fetchall():
                    pagamentos[access_int(r2[0])] = float(r2[1] or 0)

                recebido = 0.0
                pendentes = 0
                for r2 in vendas_evento:
                    venda_id = access_int(r2[0])
                    valor = float(r2[1] or 0)
                    status_pg = str(r2[3] or "aberto").strip().lower()
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
                    cur.execute("SELECT IDEvento, ValorDespesa FROM tblDespesasEvento")
                    resumo["despesas"] = money(sum(
                        float(r2[1] or 0) for r2 in cur.fetchall()
                        if access_int(r2[0]) == evento_selecionado
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
    evento_param = request.args.get("evento", "").strip()
    search = request.args.get("q", "").strip()
    mensagem = request.args.get("ok", "")
    erro = request.args.get("erro", "")
    conn = get_connection()

    try:
        cur = conn.cursor()

        # Eventos do seletor. O ID é normalizado em Python, igual à tela Vendas.
        cur.execute("""
            SELECT IDEvento, NomeEvento, DataEvento, Cidade
            FROM tblEvento
            ORDER BY DataEvento DESC, NomeEvento
        """)
        eventos = []
        evento_map = {}
        for r in cur.fetchall():
            eid = access_int(r[0])
            item = {
                "id": eid,
                "nome": str(r[1] or ""),
                "data": r[2].strftime("%d/%m/%Y") if hasattr(r[2], "strftime") else str(r[2] or ""),
                "cidade": str(r[3] or "")
            }
            eventos.append(item)
            evento_map[eid] = item

        # Formas de pagamento usadas pelo formulário da página.
        formas_pagamento = []
        try:
            cur.execute("SELECT IDFormaPagamento, FormaPagamento FROM tblFormaPagamento ORDER BY IDFormaPagamento")
            for r in cur.fetchall():
                formas_pagamento.append({
                    "id": access_int(r[0]),
                    "nome": str(r[1] or "")
                })
        except Exception:
            formas_pagamento = []

        data = []
        evento_selecionado = 0

        if evento_param:
            evento_selecionado = access_int(evento_param)
        elif eventos:
            evento_selecionado = eventos[0]["id"]

        if evento_selecionado:
            evento_info = evento_map.get(evento_selecionado, {})
            evento_nome = evento_info.get("nome", "")
            evento_data = evento_info.get("data", "")

            # =============================================================
            # IMPORTANTE:
            # NÃO filtrar IDEvento dentro do SQL.
            #
            # Na tela Vendas a venda é lida inteira e o IDEvento é
            # normalizado em Python. Isso é necessário porque alguns IDs
            # antigos migrados do Access chegam ao PostgreSQL em formatos
            # diferentes (bytes/bytea/texto). O filtro SQL por CAST(TEXT)
            # fazia a Entregas retornar ZERO linhas mesmo existindo a venda.
            # =============================================================

            # Mapa de clientes usando as mesmas regras da tela Vendas.
            def _num_ent(v):
                if v is None:
                    return 0
                if isinstance(v, memoryview):
                    v = v.tobytes()
                if isinstance(v, (bytes, bytearray)):
                    raw = bytes(v).rstrip(b'\\x00')
                    if not raw:
                        return 0
                    try:
                        txt = raw.decode('ascii').strip()
                        if txt and txt.isdigit():
                            return int(txt)
                    except Exception:
                        pass
                    return int.from_bytes(raw, byteorder='little', signed=False)
                if isinstance(v, str):
                    if len(v) == 1:
                        return ord(v)
                    s = v.strip()
                    if not s:
                        return 0
                    try:
                        return int(s)
                    except Exception:
                        return 0
                try:
                    return int(v)
                except Exception:
                    return 0

            def _id_chaves_ent(v):
                chaves = []
                if v is None:
                    return chaves
                try:
                    bruto = str(v).strip()
                    if bruto:
                        chaves.append(("raw", bruto))
                except Exception:
                    pass
                n = _num_ent(v)
                if n:
                    chaves.append(("num", n))
                return chaves

            cliente_map = {}
            cliente_raw_map = {}
            cur.execute("SELECT IDCliente, Nome FROM tblClientes")
            for r in cur.fetchall():
                nome = str(r[1] or "").strip()
                if not nome:
                    continue
                for tipo, chave in _id_chaves_ent(r[0]):
                    if tipo == "num":
                        cliente_map[chave] = nome
                    else:
                        cliente_raw_map[chave] = nome

            def _resolver_cliente_ent(valor_id):
                # Resolve diretamente em tblClientes. A Entregas usa a mesma
                # origem da Venda e não cria/edita nenhum vínculo.
                cid = _num_ent(valor_id)
                if cid:
                    try:
                        cur.execute("SELECT Nome FROM tblClientes WHERE IDCliente=? LIMIT 1", [cid])
                        row_cliente = cur.fetchone()
                        if row_cliente and row_cliente[0]:
                            nome = str(row_cliente[0]).strip()
                            if nome:
                                return nome, cid
                    except Exception:
                        try:
                            conn.rollback()
                        except Exception:
                            pass

                if cid and cid in cliente_map:
                    return cliente_map[cid], cid
                try:
                    bruto = str(valor_id).strip()
                except Exception:
                    bruto = ""
                if bruto and bruto in cliente_raw_map:
                    return cliente_raw_map[bruto], cid
                return "", cid

            # A venda nasce do agendamento. Guardamos os dois caminhos de
            # vínculo para recuperar o cliente quando o IDCliente da venda
            # antiga não resolver diretamente.
            cur.execute("SELECT IDVendas, IDAgendamento, IDCliente FROM tblAgendamentos")
            ag_venda_cliente = {}
            ag_venda_raw = {}
            ag_agendamento_cliente = {}
            ag_agendamento_raw = {}
            for r in cur.fetchall():
                idv_raw, aid_raw, cid_raw = r[0], r[1], r[2]
                idv = _num_ent(idv_raw)
                aid = _num_ent(aid_raw)
                cid = _num_ent(cid_raw)
                if idv:
                    ag_venda_cliente[idv] = cid
                try:
                    raw = str(idv_raw).strip() if idv_raw is not None else ""
                except Exception:
                    raw = ""
                if raw:
                    ag_venda_raw[raw] = cid_raw
                if aid:
                    ag_agendamento_cliente[aid] = cid
                try:
                    raw = str(aid_raw).strip() if aid_raw is not None else ""
                except Exception:
                    raw = ""
                if raw:
                    ag_agendamento_raw[raw] = cid_raw

            status_map = {}
            try:
                cur.execute("SELECT IDStatusVenda, StatusVenda FROM tblStatusVenda")
                status_map = {access_int(r[0]): str(r[1] or "").strip() for r in cur.fetchall()}
            except Exception:
                status_map = {}

            # MESMA LEITURA DA TELA VENDAS: sem JOIN e sem WHERE IDEvento.
            cur.execute("""
                SELECT IDVenda, DataVenda, IDCliente, IDEvento,
                       QtdProvas, ValorFinal, Finalizado,
                       DataFinalizacao, StatusPagamento, IDStatusVenda,
                       IDAgendamento
                FROM tblVendaPacotes
                ORDER BY IDVenda DESC
            """)
            vendas = cur.fetchall()

            termo = search.lower()

            for r in vendas:
                id_venda = access_int(r[0])
                id_cliente_raw = r[2]
                id_evento_raw = r[3]
                id_agendamento_raw = r[10]
                id_evento = access_int(id_evento_raw)

                # O filtro acontece DEPOIS da normalização do ID, não no SQL.
                if id_evento != evento_selecionado:
                    continue

                # Exclui OS CANCELADA (mesmo critério já usado nas telas
                # VENDAS e PAGAMENTOS). Esta tela buscava os nomes de
                # status mas nunca chegava a usá-los para filtrar, então
                # uma OS cancelada continuava aparecendo como pendente.
                status_venda_nome = status_map.get(access_int(r[9]), "")
                status_pagamento_bruto = str(r[8] or "").strip().lower()
                if "CANCEL" in status_venda_nome.upper() or status_pagamento_bruto == "cancelado":
                    continue

                atleta, cliente_id = _resolver_cliente_ent(id_cliente_raw)
                telefone = ""
                contato = ""

                # Caminho principal: VENDA -> AGENDAMENTO -> CLIENTE.
                if not atleta and id_venda:
                    cid_ag = ag_venda_cliente.get(id_venda, 0)
                    if cid_ag:
                        atleta = cliente_map.get(cid_ag, "")
                        if atleta:
                            cliente_id = cid_ag

                if not atleta and id_venda:
                    try:
                        venda_txt = str(r[0]).strip() if r[0] is not None else ""
                    except Exception:
                        venda_txt = ""
                    if venda_txt:
                        cid_raw = ag_venda_raw.get(venda_txt)
                        if cid_raw is not None:
                            atleta, cliente_id = _resolver_cliente_ent(cid_raw)

                # Fallback: VENDA -> IDAgendamento -> CLIENTE.
                id_agendamento = _num_ent(id_agendamento_raw)
                if not atleta and id_agendamento:
                    cid_ag = ag_agendamento_cliente.get(id_agendamento, 0)
                    if cid_ag:
                        atleta = cliente_map.get(cid_ag, "")
                        if atleta:
                            cliente_id = cid_ag

                if not atleta:
                    try:
                        aid_txt = str(id_agendamento_raw).strip() if id_agendamento_raw is not None else ""
                    except Exception:
                        aid_txt = ""
                    if aid_txt:
                        cid_raw = ag_agendamento_raw.get(aid_txt)
                        if cid_raw is not None:
                            atleta, cliente_id = _resolver_cliente_ent(cid_raw)

                # Último recurso no PostgreSQL, mantendo a mesma estratégia da
                # tela Vendas para IDs antigos que não sejam reproduzidos pelo
                # valor normalizado em Python.
                if not atleta and id_venda and _is_postgres():
                    try:
                        cur.execute("""
                            SELECT A.IDCliente
                            FROM tblAgendamentos AS A
                            WHERE CAST(A.IDVendas AS TEXT)=CAST(? AS TEXT)
                            ORDER BY A.IDAgendamento DESC
                            LIMIT 1
                        """, [id_venda])
                        ag_row = cur.fetchone()
                        if ag_row:
                            atleta, cliente_id = _resolver_cliente_ent(ag_row[0])
                            if not atleta:
                                cur.execute("""
                                    SELECT Nome FROM tblClientes
                                    WHERE CAST(IDCliente AS TEXT)=CAST(? AS TEXT)
                                    LIMIT 1
                                """, [ag_row[0]])
                                cr = cur.fetchone()
                                if cr and cr[0]:
                                    atleta = str(cr[0]).strip()
                    except Exception:
                        pass

                if not atleta and id_agendamento_raw is not None and _is_postgres():
                    try:
                        cur.execute("""
                            SELECT A.IDCliente
                            FROM tblAgendamentos AS A
                            WHERE CAST(A.IDAgendamento AS TEXT)=CAST(? AS TEXT)
                            ORDER BY A.IDAgendamento DESC
                            LIMIT 1
                        """, [id_agendamento_raw])
                        ag_row = cur.fetchone()
                        if ag_row:
                            atleta, cliente_id = _resolver_cliente_ent(ag_row[0])
                            if not atleta:
                                cur.execute("""
                                    SELECT Nome FROM tblClientes
                                    WHERE CAST(IDCliente AS TEXT)=CAST(? AS TEXT)
                                    LIMIT 1
                                """, [ag_row[0]])
                                cr = cur.fetchone()
                                if cr and cr[0]:
                                    atleta = str(cr[0]).strip()
                    except Exception:
                        pass

                # Recupera telefone/contato somente depois de descobrir o
                # cliente. Esses campos são apenas auxiliares da tela.
                if cliente_id:
                    try:
                        cur.execute("""
                            SELECT Telefone, Contato
                            FROM tblClientes
                            WHERE CAST(IDCliente AS TEXT)=CAST(? AS TEXT)
                            LIMIT 1
                        """, [cliente_id])
                        cr = cur.fetchone()
                        if cr:
                            telefone = str(cr[0] or "")
                            contato = str(cr[1] or "")
                    except Exception:
                        pass

                status_pagamento = str(r[8] or "aberto").strip().lower()

                item = {
                    "id": id_venda,
                    "cliente_id": cliente_id,
                    "atleta": atleta,
                    "telefone": telefone,
                    "contato": contato,
                    "evento_id": id_evento,
                    "evento": evento_nome,
                    "data_evento": evento_data,
                    "provas": access_int(r[4]),
                    "valor": money(r[5]),
                    "entregue": bool(r[6]) if r[6] is not None else False,
                    "data_finalizacao": (
                        r[7].strftime("%d/%m/%Y")
                        if hasattr(r[7], "strftime")
                        else str(r[7] or "")
                    ),
                    "status_pagamento": status_pagamento,
                    "pago": status_pagamento == "pago",
                    "cortesia": status_pagamento == "cortesia"
                }

                if not termo or termo in (item["atleta"] + " " + item["evento"]).lower():
                    data.append(item)

            print(f"[SGFE-ENTREGAS] evento_selecionado={evento_selecionado} vendas_exibidas={len(data)}")
            for item in data:
                if not item["atleta"]:
                    print(f"[SGFE-ENTREGAS] atleta_nao_localizado venda={item['id']} cliente={item['cliente_id']}")

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        erro = str(e)
        data = []
        print(f"[SGFE-ENTREGAS] ERRO_LISTAGEM={e}")
    finally:
        conn.close()

    return render_template(
        "entregas.html", page="entregas", title="Entregas",
        subtitle="Controle de entrega das fotos e situação do pagamento",
        eventos=eventos, evento_selecionado=evento_selecionado,
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

        # IMPORTANTE: NÃO fazer JOIN direto entre tblVendaPacotes e
        # tblClientes/tblEvento/tblPagamento aqui. Mesmo problema de tipos
        # (varchar x bigint/integer, herança da migração do Access) já
        # visto e corrigido nas telas VENDAS, PAGAMENTOS e ENTREGAS. Cada
        # tabela é lida separadamente e cruzada em Python.

        def _num(v):
            """Normaliza IDs migrados do Access (idêntica à usada nas
            telas VENDAS/PAGAMENTOS para cliente/evento/venda)."""
            if v is None:
                return 0
            if isinstance(v, memoryview):
                v = v.tobytes()
            if isinstance(v, (bytes, bytearray)):
                raw = bytes(v).rstrip(b"\x00")
                if not raw:
                    return 0
                try:
                    txt = raw.decode("ascii").strip()
                    if txt.isdigit():
                        return int(txt)
                    if txt.lower().startswith("\\x"):
                        hx = txt[2:]
                        if hx and len(hx) % 2 == 0:
                            return int.from_bytes(bytes.fromhex(hx), "little", signed=False)
                except Exception:
                    pass
                return int.from_bytes(raw, byteorder="little", signed=False)
            if isinstance(v, str):
                if len(v) == 1:
                    return ord(v)
                s = v.strip()
                if not s:
                    return 0
                try:
                    return int(s)
                except Exception:
                    pass
                if s.lower().startswith("\\x"):
                    try:
                        hx = s[2:]
                        if hx and len(hx) % 2 == 0:
                            return int.from_bytes(bytes.fromhex(hx), "little", signed=False)
                    except Exception:
                        pass
                try:
                    raw = s.encode("latin1").rstrip(b"\x00")
                    if raw:
                        return int.from_bytes(raw, byteorder="little", signed=False)
                except Exception:
                    pass
                return 0
            try:
                return int(v)
            except Exception:
                return 0

        def _txt(v):
            if v is None:
                return ""
            if hasattr(v, "strftime"):
                return v.strftime("%d/%m/%Y")
            return str(v)

        # Clientes (nome/telefone/contato).
        cur.execute("SELECT IDCliente, Nome, Telefone, Contato FROM tblClientes")
        cliente_map = {}
        cliente_raw_map = {}
        for r in cur.fetchall():
            info = {"nome": _txt(r[1]), "telefone": _txt(r[2]), "contato": _txt(r[3])}
            n = _num(r[0])
            if n:
                cliente_map[n] = info
            bruto = str(r[0]).strip() if r[0] is not None else ""
            if bruto:
                cliente_raw_map[bruto] = info

        # Vínculo VENDA -> AGENDAMENTO -> CLIENTE, para vendas antigas cujo
        # IDCliente gravado na própria venda não localiza o nome direto.
        cur.execute("SELECT IDVendas, IDCliente FROM tblAgendamentos")
        agendamento_venda_cliente_map = {}
        for r in cur.fetchall():
            idv = _num(r[0])
            if idv:
                agendamento_venda_cliente_map[idv] = _num(r[1])

        def _resolver_cliente(venda_id_norm, cliente_id_raw):
            cid = _num(cliente_id_raw)
            info = cliente_map.get(cid)
            if info and info["nome"]:
                return info
            bruto = str(cliente_id_raw).strip() if cliente_id_raw is not None else ""
            if bruto and bruto in cliente_raw_map and cliente_raw_map[bruto]["nome"]:
                return cliente_raw_map[bruto]
            cid_ag = agendamento_venda_cliente_map.get(venda_id_norm)
            if cid_ag:
                info2 = cliente_map.get(cid_ag)
                if info2 and info2["nome"]:
                    return info2
            return {"nome": "", "telefone": "", "contato": ""}

        # Nomes de evento.
        cur.execute("SELECT IDEvento, NomeEvento FROM tblEvento")
        evento_nome_map = {access_int(r[0]): str(r[1] or "") for r in cur.fetchall()}

        # Vendas que já possuem pagamento, para excluir da lista de pendentes.
        cur.execute("SELECT IDVenda FROM tblPagamento")
        vendas_com_pagamento = {access_int(r[0]) for r in cur.fetchall()}

        cur.execute("""
            SELECT IDVenda, IDCliente, IDEvento, QtdProvas, ValorFinal,
                   DataVenda, StatusPagamento
            FROM tblVendaPacotes
            ORDER BY IDVenda DESC
        """)

        eid_filtro = access_int(venda_id) if venda_id else 0
        termo = search.lower()

        pendentes = []
        for r in cur.fetchall():
            id_venda = access_int(r[0])

            if id_venda in vendas_com_pagamento:
                continue
            status_pg = str(r[6] or "").strip().lower()
            if status_pg not in ("", "aberto"):
                continue
            if eid_filtro and id_venda != eid_filtro:
                continue

            cli = _resolver_cliente(id_venda, r[1])
            evento_nome = evento_nome_map.get(access_int(r[2]), "")

            if not eid_filtro and termo and termo not in (cli["nome"] + " " + evento_nome).lower():
                continue

            pendentes.append({
                "id": id_venda, "atleta": cli["nome"],
                "evento": evento_nome, "provas": access_int(r[3]),
                "valor": money(r[4]),
                "data": _txt(r[5]),
                "telefone": cli["telefone"], "contato": cli["contato"]
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

        # tblPagamento é uma tabela migrada do Access e IDPagamento não tem
        # auto-incremento (sequence) configurado no PostgreSQL. Sem isso, o
        # INSERT tentava gravar o campo vazio e violava a restrição NOT
        # NULL. Geramos o próximo ID aqui, como já é feito para
        # tblBalizamentoProvas nesta mesma aplicação.
        cur.execute("SELECT COALESCE(MAX(IDPagamento::bigint), 0) FROM tblPagamento")
        proximo_id_pagamento = access_int(cur.fetchone()[0]) + 1

        cur.execute("""
            INSERT INTO tblPagamento (IDPagamento, IDVenda, DataPagamento, IDFormaPagamento, Parcelas, ValorPago, Observacoes)
            VALUES (?, ?, Now(), ?, ?, ?, ?)
        """,[proximo_id_pagamento,venda_id,forma_id,parcelas,valor,observacoes])

        # A tela VENDAS mostra o status a partir de IDStatusVenda (ex.:
        # "Aberta"/"Paga"), um campo separado do StatusPagamento usado
        # pelas telas Pagamentos/Entregas. Sem atualizar os dois juntos,
        # a venda ficava com StatusPagamento='pago' mas continuava
        # aparecendo como "ABERTA" na tela Vendas.
        cur.execute("SELECT IDStatusVenda, StatusVenda FROM tblStatusVenda")
        status_paga_id = None
        for r in cur.fetchall():
            nome = str(r[1] or "").strip().upper()
            if nome in ("PAGA", "PAGO"):
                status_paga_id = access_int(r[0])
                break

        if status_paga_id is not None:
            cur.execute(
                "UPDATE tblVendaPacotes SET StatusPagamento='pago', IDStatusVenda=? WHERE IDVenda=?",
                [status_paga_id, venda_id]
            )
        else:
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

        # Mantém IDStatusVenda (campo que a tela VENDAS exibe) sincronizado
        # com StatusPagamento (campo que esta tela usa), para os dois lados
        # nunca ficarem desencontrados.
        nomes_por_status = {
            "pago": ("PAGA", "PAGO"),
            "cortesia": ("CORTESIA",),
            "aberto": ("ABERTA",),
        }
        status_venda_id = _status_venda_id_por_nome(cur, *nomes_por_status.get(status, ()))
        if status_venda_id is not None:
            cur.execute(
                "UPDATE tblVendaPacotes SET StatusPagamento=?, IDStatusVenda=? WHERE IDVenda=?",
                [status, status_venda_id, venda_id]
            )
        else:
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
            evento_map = {ev["id"]: ev for ev in eventos}

            # IMPORTANTE: NÃO fazer JOIN direto entre tblVendaPacotes e
            # outras tabelas (tblEvento, tblClientes, tblStatusVenda) aqui.
            # Colunas como IDEvento/IDCliente/IDStatusVenda em
            # tblVendaPacotes são character varying (herança da migração
            # do Access), enquanto nas tabelas de referência elas podem ser
            # bigint/integer. O PostgreSQL não compara/junta tipos
            # diferentes sem CAST explícito e isso já gerou dois erros 500
            # nesta tela ("operator does not exist"). A mesma armadilha já
            # foi resolvida assim nas telas VENDAS e ENTREGAS: cada tabela
            # é lida separadamente e o cruzamento é feito em Python.

            def _num(v):
                """Normaliza IDs migrados do Access (idêntica à usada na
                tela VENDAS para cliente/evento/venda)."""
                if v is None:
                    return 0
                if isinstance(v, memoryview):
                    v = v.tobytes()
                if isinstance(v, (bytes, bytearray)):
                    raw = bytes(v).rstrip(b"\x00")
                    if not raw:
                        return 0
                    try:
                        txt = raw.decode("ascii").strip()
                        if txt.isdigit():
                            return int(txt)
                        if txt.lower().startswith("\\x"):
                            hx = txt[2:]
                            if hx and len(hx) % 2 == 0:
                                return int.from_bytes(bytes.fromhex(hx), "little", signed=False)
                    except Exception:
                        pass
                    return int.from_bytes(raw, byteorder="little", signed=False)
                if isinstance(v, str):
                    if len(v) == 1:
                        return ord(v)
                    s = v.strip()
                    if not s:
                        return 0
                    try:
                        return int(s)
                    except Exception:
                        pass
                    if s.lower().startswith("\\x"):
                        try:
                            hx = s[2:]
                            if hx and len(hx) % 2 == 0:
                                return int.from_bytes(bytes.fromhex(hx), "little", signed=False)
                        except Exception:
                            pass
                    try:
                        raw = s.encode("latin1").rstrip(b"\x00")
                        if raw:
                            return int.from_bytes(raw, byteorder="little", signed=False)
                    except Exception:
                        pass
                    return 0
                try:
                    return int(v)
                except Exception:
                    return 0

            def _num_status(v):
                """Normaliza IDStatusVenda: tenta o dígito literal primeiro
                (é assim que a edição de venda grava), só decodifica como
                code point se não for um número válido (registros antigos
                migrados do Access)."""
                if v is None:
                    return 0
                if isinstance(v, (bytes, bytearray, memoryview)):
                    return _num(v)
                if isinstance(v, str):
                    s = v.strip()
                    if not s:
                        return 0
                    try:
                        return int(s)
                    except Exception:
                        pass
                    if len(s) == 1:
                        return ord(s)
                    return 0
                try:
                    return int(v)
                except Exception:
                    return 0

            def _txt(v):
                if v is None:
                    return ""
                if hasattr(v, "strftime"):
                    return v.strftime("%d/%m/%Y")
                return str(v)

            # Clientes (nome/telefone/contato), por ID normalizado e também
            # pela representação textual bruta, igual à tela VENDAS.
            cur.execute("SELECT IDCliente, Nome, Telefone, Contato FROM tblClientes")
            cliente_map = {}
            cliente_raw_map = {}
            for r in cur.fetchall():
                info = {"nome": _txt(r[1]), "telefone": _txt(r[2]), "contato": _txt(r[3])}
                n = _num(r[0])
                if n:
                    cliente_map[n] = info
                bruto = str(r[0]).strip() if r[0] is not None else ""
                if bruto:
                    cliente_raw_map[bruto] = info

            # Vínculo VENDA -> AGENDAMENTO -> CLIENTE, para vendas antigas
            # cujo IDCliente gravado na própria venda não localiza o nome
            # diretamente (mesmo fallback usado na tela VENDAS).
            cur.execute("SELECT IDVendas, IDCliente FROM tblAgendamentos")
            agendamento_venda_cliente_map = {}
            for r in cur.fetchall():
                idv = _num(r[0])
                if idv:
                    agendamento_venda_cliente_map[idv] = _num(r[1])

            def _resolver_cliente(venda_id, cliente_id_raw):
                cid = _num(cliente_id_raw)
                info = cliente_map.get(cid)
                if info and info["nome"]:
                    return info
                bruto = str(cliente_id_raw).strip() if cliente_id_raw is not None else ""
                if bruto and bruto in cliente_raw_map and cliente_raw_map[bruto]["nome"]:
                    return cliente_raw_map[bruto]
                cid_ag = agendamento_venda_cliente_map.get(venda_id)
                if cid_ag:
                    info2 = cliente_map.get(cid_ag)
                    if info2 and info2["nome"]:
                        return info2
                return {"nome": "", "telefone": "", "contato": ""}

            # Nomes de status, só para identificar e excluir vendas
            # CANCELADAS (mesmo critério que estava no SQL original).
            cur.execute("SELECT IDStatusVenda, StatusVenda FROM tblStatusVenda")
            status_nome_map = {_num_status(r[0]): _txt(r[1]) for r in cur.fetchall()}

            cur.execute("""
                SELECT IDVenda, DataVenda, IDCliente, IDEvento, QtdProvas,
                       ValorFinal, Finalizado, StatusPagamento, IDStatusVenda
                FROM tblVendaPacotes
                ORDER BY IDVenda
            """)
            vendas = [r for r in cur.fetchall() if access_int(r[3]) == eid]

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

                status_venda_nome = status_nome_map.get(_num_status(r[8]), "")
                status_pagamento_bruto = str(r[7] or "").strip().lower()
                if "CANCEL" in status_venda_nome.upper() or status_pagamento_bruto == "cancelado":
                    continue

                cli = _resolver_cliente(id_venda, r[2])
                valor = float(r[5] or 0)
                status_registrado = str(r[7] or "aberto").strip().lower()
                recebido = float(pagamentos.get(id_venda, 0) or 0)
                if status_registrado == "cortesia":
                    # Cortesia não participa do fechamento financeiro do
                    # evento: aparece com valor zerado.
                    valor = 0.0
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

                ev_info = evento_map.get(access_int(r[3]), {})

                item = {
                    "id": id_venda,
                    "atleta": cli["nome"],
                    "telefone": cli["telefone"],
                    "contato": cli["contato"],
                    "evento": ev_info.get("nome", ""),
                    "data_evento": ev_info.get("data", ""),
                    "provas": access_int(r[4]),
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
        evento_map = {}
        for r in cur.fetchall():
            eid = access_int(r[0])
            info = {
                "id": eid,
                "nome": str(r[1] or ""),
                "data": r[2].strftime("%d/%m/%Y") if hasattr(r[2], "strftime") else str(r[2] or ""),
                "cidade": str(r[3] or "")
            }
            eventos.append(info)
            evento_map[eid] = info

        # IMPORTANTE: NÃO fazer JOIN direto entre tblVendaPacotes e
        # tblClientes/tblEvento/tblStatusVenda aqui. Mesmo problema de
        # tipos (varchar x bigint/integer, herança da migração do Access)
        # já visto e corrigido nas telas VENDAS, PAGAMENTOS, PAGAMENTOS
        # PENDENTES e ENTREGAS. Cada tabela é lida separadamente e cruzada
        # em Python.

        def _num(v):
            """Normaliza IDs migrados do Access (idêntica à usada nas
            telas VENDAS/PAGAMENTOS para cliente/evento/venda)."""
            if v is None:
                return 0
            if isinstance(v, memoryview):
                v = v.tobytes()
            if isinstance(v, (bytes, bytearray)):
                raw = bytes(v).rstrip(b"\x00")
                if not raw:
                    return 0
                try:
                    txt = raw.decode("ascii").strip()
                    if txt.isdigit():
                        return int(txt)
                    if txt.lower().startswith("\\x"):
                        hx = txt[2:]
                        if hx and len(hx) % 2 == 0:
                            return int.from_bytes(bytes.fromhex(hx), "little", signed=False)
                except Exception:
                    pass
                return int.from_bytes(raw, byteorder="little", signed=False)
            if isinstance(v, str):
                if len(v) == 1:
                    return ord(v)
                s = v.strip()
                if not s:
                    return 0
                try:
                    return int(s)
                except Exception:
                    pass
                if s.lower().startswith("\\x"):
                    try:
                        hx = s[2:]
                        if hx and len(hx) % 2 == 0:
                            return int.from_bytes(bytes.fromhex(hx), "little", signed=False)
                    except Exception:
                        pass
                try:
                    raw = s.encode("latin1").rstrip(b"\x00")
                    if raw:
                        return int.from_bytes(raw, byteorder="little", signed=False)
                except Exception:
                    pass
                return 0
            try:
                return int(v)
            except Exception:
                return 0

        def _txt(v):
            if v is None:
                return ""
            if hasattr(v, "strftime"):
                return v.strftime("%d/%m/%Y")
            return str(v)

        # Clientes (nome/telefone/contato).
        cur.execute("SELECT IDCliente, Nome, Telefone, Contato FROM tblClientes")
        cliente_map = {}
        cliente_raw_map = {}
        for r in cur.fetchall():
            info = {"nome": _txt(r[1]), "telefone": _txt(r[2]), "contato": _txt(r[3])}
            n = _num(r[0])
            if n:
                cliente_map[n] = info
            bruto = str(r[0]).strip() if r[0] is not None else ""
            if bruto:
                cliente_raw_map[bruto] = info

        # Vínculo VENDA -> AGENDAMENTO -> CLIENTE para vendas antigas cujo
        # IDCliente gravado na própria venda não localiza o nome direto.
        cur.execute("SELECT IDVendas, IDCliente FROM tblAgendamentos")
        agendamento_venda_cliente_map = {}
        for r in cur.fetchall():
            idv = _num(r[0])
            if idv:
                agendamento_venda_cliente_map[idv] = _num(r[1])

        def _resolver_cliente(venda_id_norm, cliente_id_raw):
            cid = _num(cliente_id_raw)
            info = cliente_map.get(cid)
            if info and info["nome"]:
                return info
            bruto = str(cliente_id_raw).strip() if cliente_id_raw is not None else ""
            if bruto and bruto in cliente_raw_map and cliente_raw_map[bruto]["nome"]:
                return cliente_raw_map[bruto]
            cid_ag = agendamento_venda_cliente_map.get(venda_id_norm)
            if cid_ag:
                info2 = cliente_map.get(cid_ag)
                if info2 and info2["nome"]:
                    return info2
            return {"nome": "", "telefone": "", "contato": ""}

        # Nomes de status (para excluir OS CANCELADA, mesmo critério das
        # demais telas).
        cur.execute("SELECT IDStatusVenda, StatusVenda FROM tblStatusVenda")
        status_nome_map = {access_int(r[0]): str(r[1] or "") for r in cur.fetchall()}

        # Todas as vendas/OS. O histórico não deve esconder uma OS por estar
        # finalizada ou paga: ele é justamente a memória do sistema.
        cur.execute("""
            SELECT IDVenda, DataVenda, IDCliente, IDEvento, QtdProvas,
                   ValorPacote, ValorDesconto, ValorFinal, IDStatusVenda,
                   Finalizado, DataFinalizacao, StatusPagamento, IDAgendamento
            FROM tblVendaPacotes
            ORDER BY DataVenda DESC, IDVenda DESC
        """)
        vendas_brutas = cur.fetchall()

        eid_filtro = access_int(evento_id) if evento_id else 0
        vendas_evento = []
        for r in vendas_brutas:
            if eid_filtro and access_int(r[3]) != eid_filtro:
                continue
            vendas_evento.append(r)

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

        termo = search.lower()
        resolved_rows = []
        for r in vendas_evento:
            id_venda = access_int(r[0])
            cli = _resolver_cliente(id_venda, r[2])
            evento_id_norm = access_int(r[3])
            evento_info = evento_map.get(evento_id_norm, {})
            valor = float(r[7] or 0)
            status_venda_nome = status_nome_map.get(access_int(r[8]), "")
            status_venda_lower = status_venda_nome.strip().lower()
            status_registrado = str(r[11] or "aberto").strip().lower()
            recebido = pagos.get(id_venda, 0.0)
            cancelado = "cancel" in status_venda_lower or status_registrado == "cancelado"
            cortesia = status_registrado == "cortesia"

            if cancelado:
                recebido = 0.0
                aberto = 0.0
                situacao_pagamento = "cancelado"
            elif cortesia:
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

            # Cancelada/cortesia não participam do fechamento financeiro do
            # evento: aparecem no histórico, mas com valor zerado.
            pacote_exibido = float(r[5] or 0)
            desconto_exibido = float(r[6] or 0)
            if cancelado or cortesia:
                valor = 0.0
                pacote_exibido = 0.0
                desconto_exibido = 0.0

            resolved_rows.append({
                "id": id_venda,
                "data_venda": _txt(r[1]),
                "cliente_id": _num(r[2]),
                "atleta": cli["nome"],
                "telefone": cli["telefone"],
                "contato": cli["contato"],
                "evento_id": evento_id_norm,
                "evento": evento_info.get("nome", ""),
                "data_evento": evento_info.get("data", ""),
                "cidade": evento_info.get("cidade", ""),
                "provas": access_int(r[4]),
                "pacote": pacote_exibido,
                "desconto": desconto_exibido,
                "valor": valor,
                "status": status_venda_nome,
                "entregue": bool(r[9]) if r[9] is not None else False,
                "data_entrega": _txt(r[10]),
                "recebido": recebido,
                "aberto": aberto,
                "situacao_pagamento": situacao_pagamento,
                "status_pagamento": status_registrado,
                "cancelado": cancelado,
            })

        data = []
        for item in resolved_rows:
            if status_filtro != "todos" and item["situacao_pagamento"] != status_filtro:
                continue
            if termo and termo not in (
                item["atleta"] + " " + item["evento"] + " " +
                item["telefone"] + " " + item["contato"]
            ).lower():
                continue
            data.append(item)

        # Resumo respeitando o evento selecionado, mas mostrando a realidade
        # completa das OS daquele filtro, independente do filtro de situação.
        vendas_validas = [item for item in resolved_rows if not item["cancelado"]]
        resumo_vendido = sum(item["valor"] for item in vendas_validas)
        resumo_recebido = sum(pagos.get(item["id"], 0.0) for item in vendas_validas)
        resumo_aberto = sum(
            (0.0 if item["status_pagamento"] == "cortesia"
             else max(item["valor"] - pagos.get(item["id"], 0.0), 0.0))
            for item in vendas_validas
        )
        resumo_entregues = sum(1 for item in vendas_validas if item["entregue"])
        resumo_pagos = sum(
            1 for item in vendas_validas
            if pagos.get(item["id"], 0.0) >= item["valor"] - 0.009
        )
        resumo_abertos = sum(
            1 for item in vendas_validas
            if pagos.get(item["id"], 0.0) <= 0.009
        )
        resumo_cortesias = sum(
            1 for item in vendas_validas if item["status_pagamento"] == "cortesia"
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
            "os": len(resolved_rows),
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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5001")), debug=False)
