from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, List

from .transforms import TRANSFORMS, DERIVE, get_path, apply_transforms


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_mapping(raw: Dict[str, Any], mapping: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    fields = mapping.get("fields", {})

    for k, rule in fields.items():
        # shorthand: allow string to mean {from: "..."}
        if isinstance(rule, str):
            out[k] = get_path(raw, rule)
            continue

        rule = dict(rule) if isinstance(rule, dict) else {"const": rule}

        # 1) const
        if "const" in rule:
            out[k] = rule["const"]
            continue

        # 2) select value (from / any_of)
        val = None
        if "from" in rule:
            val = get_path(raw, rule["from"])
        elif "any_of" in rule:
            for cand in rule["any_of"]:
                if isinstance(cand, dict) and "from" in cand:
                    v = get_path(raw, cand["from"])
                elif isinstance(cand, str):
                    v = get_path(raw, cand)
                else:
                    v = None
                if v not in (None, ""):
                    val = v
                    break

        # 3) transforms
        steps = rule.get("transforms") or ([] if "transform" not in rule else [rule["transform"]])
        if steps:
            try:
                val = apply_transforms(val, steps)
            except KeyError as ke:
                tname = str(ke).strip("'")
                raise KeyError(f"Unknown transform '{tname}' on field '{k}'") from ke
            except Exception as te:
                raise RuntimeError(f"Transform failure on field '{k}': {te}") from te

        # default value if empty
        if val in (None, "") and "default" in rule:
            val = rule.get("default")

        # 4) derive (can override val and look at src/out)
        if "derive" in rule:
            dname = rule["derive"]
            if dname not in DERIVE:
                raise KeyError(f"Unknown derive '{dname}' on field '{k}'")
            # support derive with params (e.g., paths)
            params = {k2: v2 for k2, v2 in rule.items() if k2 not in {"from","any_of","transforms","transform","derive"}}
            try:
                val = DERIVE[dname](raw, out, **params)
            except Exception as de:
                raise RuntimeError(f"Derive failure '{dname}' on field '{k}': {de}") from de

        # 5) required
        if rule.get("required") and val in (None, ""):
            raise ValueError(f"Required field {k} missing")

        out[k] = val

    # default normalized_at if missing
    out.setdefault("normalized_at", now_iso())
    return out