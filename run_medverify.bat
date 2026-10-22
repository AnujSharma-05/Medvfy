@echo off
rem Starts the MedVerify backend (schedule, reminders, notifications, console) and the camera.
cd /d "%~dp0"
start "MedVerify server" cmd /k .venv\Scripts\python.exe -m backend.app
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:5000
start "MedVerify camera" cmd /k .venv\Scripts\python.exe vision\camera_inspector.py
