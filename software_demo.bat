@echo off
rem Software-only demo: exactly the live_demo.bat flow (same server, camera, counting and alerts),
rem with a simulated pill box instead of the ESP32. Use it to rehearse without the hardware.
call "%~dp0live_demo.bat" mock
