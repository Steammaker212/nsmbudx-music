# Builds dist\nsmbudx-music.exe (editor) and dist\nsmbudx-music-cli.exe with PyInstaller.
#   pip install pyinstaller numpy
#   powershell -File packaging\build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot)
python -m PyInstaller --noconfirm --onefile --windowed --name nsmbudx-music --paths . packaging\editor.py
python -m PyInstaller --noconfirm --onefile --console --name nsmbudx-music-cli --paths . packaging\cli.py
