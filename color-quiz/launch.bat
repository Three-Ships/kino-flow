@echo off
cd /d "%~dp0"
title Aura Index
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1"
