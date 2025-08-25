# shared/audited_scraper.py
from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Dict, Any, Optional

from ingestion.base_scraper import BaseScraper

# Configuration
AUDIT_ENABLE = os.getenv("SCRAPER_AUDIT", "true").strip().lower() in ("1", "true", "yes")


class AuditedScraper(BaseScraper):
    """Enhanced base scraper with comprehensive audit tracking and standardized monitoring."""
    
    def __init__(self, db, county_name: str):
        super().__init__(db)
        self.county = county_name.lower()
        
        self._audit = {
            "run_id": f"{self.county}:{uuid.uuid4()}",
            "county": county_name,
            "source": f"{self.county}_jail",
            "started_at": datetime.utcnow().isoformat() + "Z",
            "letters_spec": None,
            "first_letters_spec": None,
            "append_wildcard": None,
            "prefixes_scanned": 0,
            "detail_links_found": 0,
            "details_parsed_ok": 0,
            "upserts_person_inserted": 0,
            "upserts_person_updated": 0,
            "events_yielded": 0,
            "errors": 0,
            "notes": [],
        }

    def _audit_emit(self, status: str, extra: Dict[str, Any] | None = None):
        """Emit audit record to database."""
        if not AUDIT_ENABLE:
            return
        doc = {
            "kind": "scrape_audit",
            "status": status,
            **self._audit,
            "ts": datetime.utcnow().isoformat() + "Z",
        }
        if extra:
            doc.update(extra)
        try:
            self.db["scrape_audit"].insert_one(doc)
        except Exception:
            pass

    def _audit_note(self, msg: str):
        """Add a note to the audit log."""
        self._audit["notes"].append(msg)
        self._audit_emit("note", {"msg": msg})

    def _audit_inc(self, key: str, n: int = 1):
        """Increment an audit counter."""
        self._audit[key] = int(self._audit.get(key, 0)) + n

    def _calculate_booking_age_category(self, booked_date_str: str) -> str:
        """Calculate how long ago someone was booked and return a category."""
        if not booked_date_str:
            return "unknown"
        
        try:
            booked_date = datetime.fromisoformat(booked_date_str.replace("Z", "")).date()
            current_date = datetime.utcnow().date()
            days_diff = (current_date - booked_date).days
            
            if days_diff < 0:
                return "future_date"
            elif days_diff <= 1:
                return "24_hours_or_less"
            elif days_diff <= 30:
                return "0_to_30_days"
            elif days_diff <= 60:
                return "30_to_60_days"
            elif days_diff <= 180:
                return "60_to_180_days"
            elif days_diff <= 365:
                return "180_to_365_days"
            else:
                return "365_days_or_older"
        except Exception as e:
            print(f"[{self.county}] Error calculating booking age: {e}")
            return "unknown"

    def _get_booking_priority(self, booking_age_category: str) -> int:
        """Get priority ranking based on booking age (1 = highest priority)."""
        priority_map = {
            "24_hours_or_less": 1,
            "0_to_30_days": 2,
            "30_to_60_days": 3,
            "60_to_180_days": 4,
            "180_to_365_days": 5,
            "365_days_or_older": 6,
            "unknown": 7,
            "future_date": 8
        }
        return priority_map.get(booking_age_category, 7)

    def _audit_success(self, name: str, category: str):
        """Log a successful record processing."""
        print(f"[{self.county}] SUCCESS: {name} [{category}]")

    def _audit_start(self, **kwargs):
        """Mark the start of a scraping run."""
        self._audit.update(kwargs)
        self._audit_emit("start")

    def _audit_finish(self, **kwargs):
        """Mark the completion of a scraping run."""
        finished_data = {
            "finished_at": datetime.utcnow().isoformat() + "Z",
            **kwargs
        }
        self._audit_emit("done", finished_data)
        
        # Print summary
        print(f"[{self.county}] COMPLETED AUDIT SUMMARY:")
        print(f"  Prefixes scanned: {self._audit['prefixes_scanned']}")
        print(f"  Detail links found: {self._audit['detail_links_found']}")
        print(f"  Details parsed OK: {self._audit['details_parsed_ok']}")
        print(f"  Person records inserted: {self._audit['upserts_person_inserted']}")
        print(f"  Person records updated: {self._audit['upserts_person_updated']}")
        print(f"  Events yielded: {self._audit['events_yielded']}")
        print(f"  Errors: {self._audit['errors']}")

    def _enhance_event(self, event: Dict[str, Any], booking_date_iso: Optional[str]) -> Dict[str, Any]:
        """Add standardized fields to event records."""
        booking_age_category = self._calculate_booking_age_category(booking_date_iso) if booking_date_iso else "unknown"
        priority = self._get_booking_priority(booking_age_category)
        
        event.update({
            "booking_age_category": booking_age_category,
            "booking_priority": priority,
            "scraped_at": datetime.utcnow().isoformat() + "Z",
        })
        
        return event