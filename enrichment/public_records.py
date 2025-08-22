"""
Public records enrichment (placeholder).
Add address, phones, next-of-kin from permissible sources.
"""
from typing import Dict, Any

def enrich_contact(person: Dict[str, Any]) -> Dict[str, Any]:
    # TODO: implement lookups (property, cases, permitted brokers)
    person.setdefault("contact", {})
    person["contact"].setdefault("addresses", [])
    person["contact"].setdefault("phones", [])
    return person
