@echo off
cd /d "%~dp0"
echo ==============================================
echo SGFE - Preparar banco para o Render
echo ==============================================
python migrar_access_para_sqlite.py --access "C:\FG FOTOS\SGFE.accdb" --saida "dados\sgfe_admin.sqlite3"
if errorlevel 1 (
  echo.
  echo ERRO na migracao. Veja a mensagem acima.
  pause
  exit /b 1
)
echo.
echo BANCO ADMINISTRATIVO CRIADO COM SUCESSO.
echo Arquivo: dados\sgfe_admin.sqlite3
pause
