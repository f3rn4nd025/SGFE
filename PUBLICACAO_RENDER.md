# Publicação SGFE no Render

1. No Windows, instale/tenha Python + pyodbc + Microsoft Access Database Engine.
2. Execute:
   `python migrar_access_para_sqlite.py --access "C:\FG FOTOS\SGFE.accdb" --saida "dados\sgfe_admin.sqlite3"`
3. Confira se `dados/sgfe_admin.sqlite3` foi criado.
4. Suba para o repositório GitHub apenas a versão de produção do SGFE.
5. No Render, use `pip install -r requirements.txt` no Build e `gunicorn app_online_teste:app` no Start.
6. Configure `SGFE_DB_MODE=sqlite`, `SGFE_DATA_DIR=/var/data`, `SGFE_ADMIN_LOGIN`, `SGFE_ADMIN_PASSWORD` e `SGFE_SECRET_KEY`.
7. Para dados permanentes dos fotógrafos, o serviço precisa de armazenamento persistente montado em `/var/data`. Sem isso, arquivos SQLite podem ser perdidos em reinícios/deploys.
8. Depois do deploy, teste primeiro `https://sgfe.onrender.com`; só então aponte o domínio personalizado.
