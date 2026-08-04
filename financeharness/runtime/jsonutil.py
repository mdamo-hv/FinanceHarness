"""Robust JSON-object extraction from model output (fenced / prose-wrapped)."""

from __future__ import annotations

import json
import re


def extract_json_obj(raw):
  """Parse a JSON object from model text, tolerating code fences / surrounding

  prose. Returns None if no object is found.
  """
  raw = (raw or "").strip()
  try:
    obj = json.loads(raw)
    return obj if isinstance(obj, dict) else None
  except (json.JSONDecodeError, ValueError):
    pass
  m = re.search(r"\{.*\}", raw, re.DOTALL)
  if not m:
    return None
  try:
    obj = json.loads(m.group(0))
    return obj if isinstance(obj, dict) else None
  except (json.JSONDecodeError, ValueError):
    return None
