"""Refresh the catalog snapshots shipped with the pack.

The category nodes and the LLM node read their model menus from a saved
copy of the catalog, refreshed in the background while ComfyUI runs. These
snapshots are the copy used before the first refresh (first start, or no
network). Run this before a release:

    python scripts/update_catalog.py

Both endpoints are public, so no API key is needed.
"""

from __future__ import annotations

import gzip
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wiro_comfyui import catalog  # noqa: E402


def main() -> int:
    models = catalog.fetch_catalog()
    with gzip.open(catalog.SNAPSHOT, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(models, handle, separators=(",", ":"), ensure_ascii=False)
    print(f"{len(models)} models -> {os.path.relpath(catalog.SNAPSHOT, ROOT)} "
          f"({os.path.getsize(catalog.SNAPSHOT) // 1024} KB)")
    llm = catalog.fetch_llm_models()
    with open(catalog.LLM_SNAPSHOT, "w", encoding="utf-8") as handle:
        json.dump(llm, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    print(f"{len(llm)} LLM gateway models -> {os.path.relpath(catalog.LLM_SNAPSHOT, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
