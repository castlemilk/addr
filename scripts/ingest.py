#!/usr/bin/env python3
"""
Australia Offline Geocoding & POI Ingestion Pipeline
Fetches Australian Address (G-NAF origin) and Place data from Overture Maps
and indexes it into a high-performance local SQLite database with R*Tree and FTS5.
"""

import sys
import os
import argparse
import sqlite3
import time
from typing import Tuple, Dict, Any, List
import overturemaps

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "australia_geo.db")

PRESETS: Dict[str, Tuple[float, float, float, float]] = {
    # minx, miny, maxx, maxy (lon_min, lat_min, lon_max, lat_max)
    "sydney_cbd": (151.195, -33.885, 151.225, -33.855),
    "sydney_inner": (151.150, -33.915, 151.255, -33.825),
    "melbourne_core": (144.955, -37.822, 144.975, -37.810),
    "melbourne_cbd": (144.940, -37.825, 144.985, -37.800),
    "melbourne_inner": (144.920, -37.845, 145.010, -37.780),
    "brisbane_cbd": (153.000, -27.485, 153.045, -27.455),
    "perth_cbd": (115.840, -31.965, 115.875, -31.945),
}

def init_db(con: sqlite3.Connection):
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode = WAL;")
    cur.execute("PRAGMA synchronous = NORMAL;")
    cur.execute("PRAGMA cache_size = -64000;")  # 64MB cache

    # Places table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS places (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uuid TEXT UNIQUE,
        name TEXT NOT NULL,
        category TEXT,
        brand TEXT,
        address_text TEXT,
        suburb TEXT,
        state TEXT,
        postcode TEXT,
        lat REAL NOT NULL,
        lon REAL NOT NULL,
        phone TEXT,
        website TEXT,
        confidence REAL
    );
    """)

    # Places R*Tree
    cur.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS places_rtree USING rtree(
        id,
        minX, maxX,
        minY, maxY
    );
    """)

    # Places FTS5
    cur.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS places_fts USING fts5(
        name,
        category,
        brand,
        address_text,
        suburb,
        content=places,
        content_rowid=id
    );
    """)

    # Triggers for Places FTS & RTree
    cur.execute("""
    CREATE TRIGGER IF NOT EXISTS places_ai AFTER INSERT ON places BEGIN
        INSERT INTO places_rtree(id, minX, maxX, minY, maxY) 
        VALUES (new.id, new.lon, new.lon, new.lat, new.lat);
        INSERT INTO places_fts(rowid, name, category, brand, address_text, suburb) 
        VALUES (new.id, new.name, new.category, new.brand, new.address_text, new.suburb);
    END;
    """)

    cur.execute("""
    CREATE TRIGGER IF NOT EXISTS places_ad AFTER DELETE ON places BEGIN
        DELETE FROM places_rtree WHERE id = old.id;
        INSERT INTO places_fts(places_fts, rowid, name, category, brand, address_text, suburb) 
        VALUES ('delete', old.id, old.name, old.category, old.brand, old.address_text, old.suburb);
    END;
    """)

    # Addresses table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS addresses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uuid TEXT UNIQUE,
        unit TEXT,
        number TEXT,
        street TEXT,
        suburb TEXT,
        state TEXT,
        postcode TEXT,
        full_address TEXT NOT NULL,
        lat REAL NOT NULL,
        lon REAL NOT NULL
    );
    """)

    # Addresses R*Tree
    cur.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS addresses_rtree USING rtree(
        id,
        minX, maxX,
        minY, maxY
    );
    """)

    # Addresses FTS5
    cur.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS addresses_fts USING fts5(
        full_address,
        street,
        suburb,
        postcode,
        content=addresses,
        content_rowid=id
    );
    """)

    # Triggers for Addresses FTS & RTree
    cur.execute("""
    CREATE TRIGGER IF NOT EXISTS addresses_ai AFTER INSERT ON addresses BEGIN
        INSERT INTO addresses_rtree(id, minX, maxX, minY, maxY) 
        VALUES (new.id, new.lon, new.lon, new.lat, new.lat);
        INSERT INTO addresses_fts(rowid, full_address, street, suburb, postcode) 
        VALUES (new.id, new.full_address, new.street, new.suburb, new.postcode);
    END;
    """)

    cur.execute("""
    CREATE TRIGGER IF NOT EXISTS addresses_ad AFTER DELETE ON addresses BEGIN
        DELETE FROM addresses_rtree WHERE id = old.id;
        INSERT INTO addresses_fts(addresses_fts, rowid, full_address, street, suburb, postcode) 
        VALUES ('delete', old.id, old.full_address, old.street, old.suburb, old.postcode);
    END;
    """)

    # Metadata table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS indexed_regions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        bbox TEXT,
        place_count INTEGER,
        address_count INTEGER,
        created_at TEXT
    );
    """)

    con.commit()

def extract_places(con: sqlite3.Connection, bbox: Tuple[float, float, float, float], limit: int = None) -> int:
    print(f"[Places] Fetching from Overture Maps for BBox {bbox}...")
    start_time = time.time()
    reader = overturemaps.record_batch_reader("place", bbox=bbox)
    
    insert_sql = """
    INSERT OR IGNORE INTO places (
        uuid, name, category, brand, address_text, suburb, state, postcode, lat, lon, phone, website, confidence
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """
    
    total = 0
    cur = con.cursor()
    
    for batch in reader:
        rows_to_insert = []
        for r in batch.to_pylist():
            names = r.get("names") or {}
            primary_name = names.get("primary") or names.get("common")
            if not primary_name:
                continue

            categories = r.get("categories") or {}
            category = categories.get("primary")
            
            brand_dict = r.get("brand") or {}
            brand_names = brand_dict.get("names") or {}
            brand = brand_names.get("primary")

            addresses = r.get("addresses") or []
            first_addr = addresses[0] if addresses else {}
            address_text = first_addr.get("freeform")
            suburb = first_addr.get("locality")
            state = first_addr.get("region")
            postcode = first_addr.get("postcode")

            bbox_geom = r.get("bbox") or {}
            lon = (bbox_geom.get("xmin", 0) + bbox_geom.get("xmax", 0)) / 2.0
            lat = (bbox_geom.get("ymin", 0) + bbox_geom.get("ymax", 0)) / 2.0
            if lon == 0 or lat == 0:
                continue

            phones = r.get("phones") or []
            phone = phones[0] if phones else None

            websites = r.get("websites") or []
            website = websites[0] if websites else None

            confidence = r.get("confidence")

            rows_to_insert.append((
                r["id"],
                primary_name,
                category,
                brand,
                address_text,
                suburb,
                state,
                postcode,
                lat,
                lon,
                phone,
                website,
                confidence
            ))
            
            if limit and (total + len(rows_to_insert)) >= limit:
                break

        if rows_to_insert:
            cur.executemany(insert_sql, rows_to_insert)
            con.commit()
            total += len(rows_to_insert)
            print(f"  [Places] Inserted {total} places so far... ({time.time() - start_time:.1f}s)")

        if limit and total >= limit:
            break

    print(f"[Places] Finished. Total places inserted: {total} in {time.time() - start_time:.1f}s")
    return total

def extract_addresses(con: sqlite3.Connection, bbox: Tuple[float, float, float, float], limit: int = None) -> int:
    print(f"[Addresses] Fetching from Overture Maps for BBox {bbox}...")
    start_time = time.time()
    reader = overturemaps.record_batch_reader("address", bbox=bbox)
    
    insert_sql = """
    INSERT OR IGNORE INTO addresses (
        uuid, unit, number, street, suburb, state, postcode, full_address, lat, lon
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """
    
    total = 0
    cur = con.cursor()
    
    for batch in reader:
        rows_to_insert = []
        for r in batch.to_pylist():
            unit = r.get("unit")
            number = r.get("number")
            street = r.get("street")
            postcode = r.get("postcode")
            
            levels = r.get("address_levels") or []
            state = None
            suburb = None
            if len(levels) >= 2:
                state = levels[0].get("value")
                suburb = levels[1].get("value")
            elif len(levels) == 1:
                state = levels[0].get("value")

            parts = []
            if unit:
                parts.append(f"Unit {unit}")
            if number and street:
                parts.append(f"{number} {street}")
            elif street:
                parts.append(street)
            
            loc_parts = []
            if suburb:
                loc_parts.append(suburb.title())
            if state:
                loc_parts.append(state.upper())
            if postcode:
                loc_parts.append(postcode)
                
            loc_str = " ".join(loc_parts)
            if parts and loc_str:
                full_address = f"{' '.join(parts)}, {loc_str}"
            elif parts:
                full_address = " ".join(parts)
            else:
                full_address = loc_str

            bbox_geom = r.get("bbox") or {}
            lon = (bbox_geom.get("xmin", 0) + bbox_geom.get("xmax", 0)) / 2.0
            lat = (bbox_geom.get("ymin", 0) + bbox_geom.get("ymax", 0)) / 2.0
            if lon == 0 or lat == 0:
                continue

            rows_to_insert.append((
                r["id"],
                unit,
                number,
                street,
                suburb,
                state,
                postcode,
                full_address,
                lat,
                lon
            ))

            if limit and (total + len(rows_to_insert)) >= limit:
                break

        if rows_to_insert:
            cur.executemany(insert_sql, rows_to_insert)
            con.commit()
            total += len(rows_to_insert)
            print(f"  [Addresses] Inserted {total} addresses so far... ({time.time() - start_time:.1f}s)")

        if limit and total >= limit:
            break

    print(f"[Addresses] Finished. Total addresses inserted: {total} in {time.time() - start_time:.1f}s")
    return total

def main():
    parser = argparse.ArgumentParser(description="Ingest Australia Addresses and POIs into SQLite")
    parser.add_argument("--preset", choices=list(PRESETS.keys()), help="Named region preset")
    parser.add_argument("--bbox", type=str, help="Custom BBox as minx,miny,maxx,maxy (lon_min, lat_min, lon_max, lat_max)")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="Path to SQLite database")
    parser.add_argument("--type", choices=["all", "places", "addresses"], default="all", help="What to ingest")
    parser.add_argument("--limit", type=int, default=None, help="Max records per type")

    args = parser.parse_args()

    if args.preset:
        bbox = PRESETS[args.preset]
        region_name = args.preset
    elif args.bbox:
        coords = [float(x.strip()) for x in args.bbox.split(",")]
        if len(coords) != 4:
            print("Error: BBox must have 4 coordinates: minx,miny,maxx,maxy")
            sys.exit(1)
        bbox = tuple(coords)
        region_name = f"custom_{bbox[0]}_{bbox[1]}"
    else:
        print("No preset or bbox provided. Defaulting to 'sydney_cbd' preset.")
        bbox = PRESETS["sydney_cbd"]
        region_name = "sydney_cbd"

    os.makedirs(os.path.dirname(args.db), exist_ok=True)
    print(f"Opening SQLite database at: {args.db}")
    con = sqlite3.connect(args.db)
    init_db(con)

    place_count = 0
    address_count = 0

    if args.type in ["all", "places"]:
        place_count = extract_places(con, bbox, args.limit)

    if args.type in ["all", "addresses"]:
        address_count = extract_addresses(con, bbox, args.limit)

    cur = con.cursor()
    cur.execute("""
    INSERT INTO indexed_regions (name, bbox, place_count, address_count, created_at)
    VALUES (?, ?, ?, ?, datetime('now'));
    """, (region_name, f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}", place_count, address_count))
    con.commit()
    con.close()

    print(f"\nSuccessfully populated {args.db} with {place_count} places and {address_count} addresses!")

if __name__ == "__main__":
    main()
