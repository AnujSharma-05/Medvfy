@echo off
rem MedVerify control: run ".\mv" in the VS Code terminal for the list of commands.
"%~dp0.venv\Scripts\python.exe" "%~dp0tools\mv.py" %*
