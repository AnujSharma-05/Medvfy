@echo off
rem Simulates the camera from the keyboard (no webcam needed). Start run_server.bat first.
cd /d "%~dp0"
.venv\Scripts\python.exe tools\fake_camera.py
pause
