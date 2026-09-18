param([string]$TaskName = 'Farm C2 - Ordens')
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path $PSScriptRoot -Parent
$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Crie a .venv antes de instalar o agendamento.' }
if ((Get-TimeZone).Id -ne 'E. South America Standard Time') { throw 'Configure o fuso do Windows para Brasília antes de instalar a tarefa.' }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'Tarefa já existe; revise no Agendador em vez de sobrescrever.' }
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument 'main.py --run' -WorkingDirectory $projectPath
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '14:56'
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 45)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal
