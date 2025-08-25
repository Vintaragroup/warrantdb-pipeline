# scripts/migrate_galveston_data.py
"""
Migrate existing Galveston data from custody_events to galveston_events
with time-based categorization added.
"""

import re
from datetime import datetime
from storage.mongo_client import get_db

def _iso_date_guess(s):
    """Parse various date formats to ISO date string."""
    if not s:
        return None
    s = s.strip()
    
    # Handle MM/DD/YYYY format
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mm, dd, yy = map(int, m.groups())
        try:
            return datetime(yy, mm, dd).date().isoformat()
        except Exception:
            pass
    
    # Handle datetime strings like "9/13/2021 1:17:00 PM"
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+\d{1,2}:\d{2}:\d{2}\s+[AP]M", s)
    if m:
        mm, dd, yy = map(int, m.groups())
        try:
            return datetime(yy, mm, dd).date().isoformat()
        except Exception:
            pass
    
    try:
        return datetime.fromisoformat(s.replace("Z","")).date().isoformat()
    except Exception:
        return None

def _calculate_booking_age_category(booked_date_str):
    """Calculate how long ago someone was booked and return a category."""
    if not booked_date_str:
        return "unknown"
    
    try:
        booked_date = datetime.fromisoformat(booked_date_str).date()
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
        print(f"Error calculating booking age for {booked_date_str}: {e}")
        return "unknown"

def migrate_galveston_data():
    """Migrate Galveston data from custody_events to galveston_events."""
    db = get_db()
    
    print("Starting Galveston data migration...")
    
    # Find all Galveston records in custody_events
    galveston_records = list(db['custody_events'].find({'county': 'Galveston'}))
    print(f"Found {len(galveston_records)} Galveston records in custody_events")
    
    if len(galveston_records) == 0:
        print("No Galveston records found to migrate")
        return
    
    # Check if galveston_events already has data
    existing_count = db['galveston_events'].count_documents({})
    if existing_count > 0:
        response = input(f"galveston_events collection already has {existing_count} records. Continue? (y/N): ")
        if response.lower() != 'y':
            print("Migration cancelled")
            return
    
    migrated_count = 0
    error_count = 0
    
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
    
    for record in galveston_records:
        try:
            # Extract arrest_date or booked_at for time categorization
            arrest_date = record.get('arrest_date') or record.get('booked_at')
            
            # Convert to ISO format if needed
            booking_date_iso = _iso_date_guess(arrest_date)
            
            # Calculate booking age category
            booking_age_category = _calculate_booking_age_category(booking_date_iso)
            priority = priority_map.get(booking_age_category, 7)
            
            # Create the new record with time-based fields
            new_record = record.copy()
            new_record['booked_at'] = booking_date_iso
            new_record['booking_age_category'] = booking_age_category
            new_record['booking_priority'] = priority
            new_record['migrated_at'] = datetime.utcnow().isoformat() + "Z"
            
            # Insert into galveston_events
            db['galveston_events'].insert_one(new_record)
            migrated_count += 1
            
            if migrated_count % 100 == 0:
                print(f"Migrated {migrated_count}/{len(galveston_records)} records...")
                
        except Exception as e:
            print(f"Error migrating record {record.get('_id')}: {e}")
            error_count += 1
    
    print(f"\nMigration completed:")
    print(f"  Successfully migrated: {migrated_count} records")
    print(f"  Errors: {error_count} records")
    
    # Show time-based breakdown
    print(f"\nTime-based breakdown:")
    for category in ["24_hours_or_less", "0_to_30_days", "30_to_60_days", 
                     "60_to_180_days", "180_to_365_days", "365_days_or_older", "unknown"]:
        count = db['galveston_events'].count_documents({'booking_age_category': category})
        if count > 0:
            print(f"  {category}: {count} inmates")
    
    # Ask about cleanup
    if migrated_count > 0:
        response = input(f"\nDelete {migrated_count} Galveston records from custody_events? (y/N): ")
        if response.lower() == 'y':
            result = db['custody_events'].delete_many({'county': 'Galveston'})
            print(f"Deleted {result.deleted_count} Galveston records from custody_events")
        else:
            print("Kept original records in custody_events")

if __name__ == "__main__":
    migrate_galveston_data()