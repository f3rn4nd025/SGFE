# SGFE WEB - Publicação Render

Esta é a versão de publicação baseada na V8, com a lógica de bancos separados por fotógrafo.

## Arquitetura
- ADMIN: usa exclusivamente `dados/sgfe_admin.sqlite3` no modo SQLite/Render.
- FOTÓGRAFO: usa exclusivamente `dados/bancos_fotografos/fotografo_ID.sqlite3`.
- O primeiro acesso de um fotógrafo cria seu banco privado a partir do banco administrativo e limpa os dados operacionais, evitando herdar clientes, eventos, vendas, agendamentos e demais dados do administrador.
- O cadastro e bloqueio/liberação de fotógrafos continuam no banco administrativo.

## Render
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app_online_teste:app`
- `SGFE_DB_MODE=sqlite`
- `SGFE_DATA_DIR=/var/data`
- `SGFE_ADMIN_LOGIN` e `SGFE_ADMIN_PASSWORD`
- `SGFE_SECRET_KEY`

## Banco administrativo
O arquivo SQLite com dados reais NÃO deve ser enviado para um repositório GitHub público. Gere-o localmente a partir do Access usando `migrar_access_para_sqlite.py` e disponibilize-o no ambiente privado de produção.
