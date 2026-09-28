# Backend run helper (PowerShell)
param([int]$Port = 8770)
$ErrorActionPreference = "Stop"
$env:PORT = "$Port"
python -m uvicorn app.main:app --host 127.0.0.1 --port $Port --reload
