#!/bin/bash

# Minimal smoke check: list the available profiles and skills.
set -e
uv run python main.py --list
