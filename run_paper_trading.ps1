# run_paper_trading.ps1
# ─────────────────────────────────────────────────────────────────
# Script que ejecuta Task Scheduler. Carga credenciales y corre el runner.
# NO se llama directamente — lo invoca la tarea programada.

$ErrorActionPreference = "Stop"
$ProjectDir = "C:\Users\alber\Documents\Projects\jarvis-ai-stack"

Set-Location $ProjectDir

# Cargar credenciales (Task Scheduler no hereda la sesión)
. "$ProjectDir\set_env.ps1"

# Correr el runner multi-mercado (manda a Telegram solo)
python "$ProjectDir\jarvis_paper_runner.py"
