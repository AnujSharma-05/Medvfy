@echo off
rem MedVerify live demonstration: real pill box, camera, and the three apps. No scripted tests.
rem Double-click, or run  live_demo.bat  (add "mock" to use the simulated box).
cd /d "%~dp0"
if /i "%1"=="mock" (set MEDVERIFY_DEVICE=mock) else (set MEDVERIFY_DEVICE=)

echo == Stopping anything left running ==
call mv down --force
echo == Starting server, camera and pill box ==
call mv up
if errorlevel 1 (echo Start-up failed: see  mv logs server  /  mv logs camera & pause & exit /b 1)
call mv cam monitor on

start "" "http://127.0.0.1:5000/console"
start "" "http://127.0.0.1:5000/patient"
start "" "http://127.0.0.1:5000/care"

echo.
call mv status
echo.
echo ================= LIVE DEMO CONTROLS (type in this window) =================
echo   mv dose start A pills=1      start a dose: LED A + buzzer, patient screen shows A
echo   mv dose cancel               cancel the open dose
echo   mv status                    counts, camera, box, open doses
echo   mv box alarm ^| warn          play the alarm / advisory sound
echo   mv cam loaded A=4 B=4 C=4 D=3   after refilling: new loaded photo with counts
echo   mv down                      stop everything at the end
echo Camera or sheet moved ^(mv status: view blocked A, B, C, D^)?  run  setup_camera.bat  ^(guided, 2 min^)
echo ============================================================================
cmd /k
