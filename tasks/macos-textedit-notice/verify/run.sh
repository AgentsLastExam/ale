#!/usr/bin/env bash
set -euo pipefail
exec "${ALE_VERIFY_PYTHON:?ALE_VERIFY_PYTHON is required}" verify.py
