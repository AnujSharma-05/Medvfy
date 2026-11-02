@echo off
rem Guided test of the ESP32 pill box (LEDs + buzzer). Close the Arduino Serial Monitor first.
cd /d "%~dp0"
.venv\Scripts\python.exe tools\hardware_check.py %*
pause
