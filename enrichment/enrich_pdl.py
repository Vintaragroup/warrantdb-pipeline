# enrich_pdl.py
import os
import requests

PDL_API_KEY = os.getenv("PDL_API_KEY")
PDL_URL = "https://api.peopledatalabs.com/v5/person/enrich"

def enrich_person(first_name: str, last_name: str, location: str = None) -> dict:
    """
    Enriches a person profile using People Data Labs.
    """
    if not PDL_API_KEY:
        raise RuntimeError("PDL_API_KEY not set in environment.")

    payload = {
        "first_name": first_name,
        "last_name": last_name
    }
    if location:
        payload["location"] = location

    headers = {
        "X-Api-Key": PDL_API_KEY,
        "Content-Type": "application/json"
    }

    resp = requests.post(PDL_URL, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()