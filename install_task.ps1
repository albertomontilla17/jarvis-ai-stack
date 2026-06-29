# install_task.ps1
# Registra la tarea de paper trading en Windows Task Scheduler.
# Corre cada 15 minutos. Ejecutar UNA VEZ.

$TaskName   = "Jarvis_PaperTrading"
$ProjectDir = "C:\Users\alber\Documents\Projects\jarvis-ai-stack"
$RunScript  = "$ProjectDir\run_paper_trading.ps1"

Write-Host "Registrando tarea: $TaskName"

# Borrar tarea previa si existe
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Tarea previa encontrada, eliminando..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Accion: correr PowerShell con el script (oculto)
$argString = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RunScript`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argString

# Trigger: cada 15 min, indefinidamente, empezando ahora
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15)

# Settings: correr en bateria, empezar cuando este disponible
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

# Correr como el usuario actual, solo con sesion iniciada
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Jarvis paper trading Polymarket cada 15 min con resumen a Telegram."

Write-Host ""
Write-Host "Tarea registrada correctamente: $TaskName"
Write-Host ""
Write-Host "Comandos utiles:"
Write-Host "  Probar ahora:  Start-ScheduledTask -TaskName Jarvis_PaperTrading"
Write-Host "  Ver estado:    Get-ScheduledTask -TaskName Jarvis_PaperTrading | Get-ScheduledTaskInfo"
Write-Host "  Pausar:        Disable-ScheduledTask -TaskName Jarvis_PaperTrading"
Write-Host "  Reactivar:     Enable-ScheduledTask -TaskName Jarvis_PaperTrading"
Write-Host "  Eliminar:      Unregister-ScheduledTask -TaskName Jarvis_PaperTrading -Confirm:0"
