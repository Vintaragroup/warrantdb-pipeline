# scripts/run_ingestion.py
import argparse
import importlib
from storage.mongo_client import get_db

# Map source name -> "module_path:ClassName"
SCRAPER_SPECS = {
    "harris_inmate":       "ingestion.harris_inmate:HarrisInmateScraper",
    "galveston_p2c_fast":  "ingestion.galveston_p2c_fast:GalvestonP2CFastScraper",
    "brazoria_jail":       "ingestion.brazoria_jail:BrazoriaJailScraper",
    "fortbend_jail":       "ingestion.fortbend_jail:FortBendJailScraper",
    "jefferson_jail":      "ingestion.jefferson_jail:JeffersonJailScraper",
    "harris_email_roster": "ingestion.harris_email_roster:HarrisEmailRosterImporter",
}

def _load_scraper_class(source: str):
    spec = SCRAPER_SPECS[source]
    mod_path, cls_name = spec.split(":")
    mod = importlib.import_module(mod_path)
    try:
        return getattr(mod, cls_name)
    except AttributeError as e:
        raise ImportError(f"Could not find class '{cls_name}' in module '{mod_path}'") from e

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, choices=SCRAPER_SPECS.keys())
    args = parser.parse_args()

    db = get_db()
    ScraperCls = _load_scraper_class(args.source)
    scraper = ScraperCls(db)

    upserts_ins = 0
    upserts_upd = 0
    events = 0
    last_person_id = None

    if hasattr(scraper, "fetch") and callable(getattr(scraper, "fetch")):
        for doc in scraper.fetch():
            coll = doc.pop("_collection", "persons")

            if coll == "persons":
                res = scraper.upsert_person(doc)
                last_person_id = res.get("_id") or last_person_id
                if res.get("inserted"):
                    upserts_ins += 1
                else:
                    upserts_upd += 1
            else:
                if not doc.get("person_id") and last_person_id:
                    doc["person_id"] = last_person_id
                db[coll].insert_one(doc)
                events += 1
    elif hasattr(scraper, "run") and callable(getattr(scraper, "run")):
        result = scraper.run()
        print(f"[{args.source}] run() completed with result: {result}")
    else:
        raise AttributeError(
            f"{ScraperCls.__name__} has neither fetch() nor run() method"
        )

    print(
        f"Done: {args.source} | "
        f"persons inserted: {upserts_ins} | persons updated: {upserts_upd} | "
        f"events inserted: {events}"
    )

if __name__ == "__main__":
    main()