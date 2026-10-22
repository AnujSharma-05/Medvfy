@echo off
rem Grades recorded trials (data\trials) and suggests thresholds. Add --apply to save them.
cd /d "%~dp0"
.venv\Scripts\python.exe tools\evaluate_trials.py %*
pause
