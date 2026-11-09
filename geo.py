"""Where Members live, roughly: postal codes to places on the map.

The map shows each Member as a dot in a rough area about 5 km across,
never their street. It needs to know roughly where each postal code is.
That comes from GeoNames (https://www.geonames.org), which publishes the
location of the postal codes of most countries, free, under the Creative
Commons Attribution 4.0 licence.

    python3 geo.py build /path/to/data/folder

downloads GeoNames' postal codes and country names and builds postal.db
in that folder. The deploy runs it on the server when postal.db is missing
or more than six months old.
"""

from __future__ import annotations

import io
import math
import os
import sqlite3
import sys
import urllib.request
import zipfile

POSTAL_URL = "https://download.geonames.org/export/zip/allCountries.zip"
COUNTRIES_URL = "https://download.geonames.org/export/dump/countryInfo.txt"
CELL_DEGREES = 0.045  # about 5 km north to south

# Common ways people write a country that aren't GeoNames' own name for it.
ALIASES = {
    "usa": "US", "u.s.": "US", "u.s.a.": "US", "us": "US", "united states of america": "US", "america": "US",
    "uk": "GB", "u.k.": "GB", "great britain": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "northern ireland": "GB", "holland": "NL", "the netherlands": "NL", "méxico": "MX", "deutschland": "DE",
    "españa": "ES", "brasil": "BR", "south korea": "KR", "korea": "KR", "russia": "RU", "czech republic": "CZ",
}


def Build(folder, postal_zip_bytes=None, countries_text=None):
    """Build postal.db in `folder`, downloading from GeoNames unless the data is given."""
    if postal_zip_bytes is None:
        postal_zip_bytes = _Download(POSTAL_URL)
    if countries_text is None:
        countries_text = _Download(COUNTRIES_URL).decode("utf-8")
    final = os.path.join(folder, "postal.db")
    temp = final + ".new"
    if os.path.exists(temp):
        os.remove(temp)
    db = sqlite3.connect(temp)
    db.create_function("PYLOWER", 1, lambda text: text.lower() if text else text)  # SQLite's LOWER is ASCII-only
    db.executescript(
        """
        CREATE TEMP TABLE raw (country TEXT, code TEXT, place TEXT, lat REAL, lon REAL);
        CREATE TABLE postal (country TEXT, code TEXT, place TEXT, lat REAL, lon REAL, PRIMARY KEY (country, code));
        CREATE TABLE places (country TEXT, place TEXT, lat REAL, lon REAL, PRIMARY KEY (country, place));
        CREATE TABLE countries (name TEXT PRIMARY KEY, iso TEXT);
        """
    )
    # Read the rows straight into the database (not into memory: there are
    # over a million), then let SQLite average places listed more than once.
    with zipfile.ZipFile(io.BytesIO(postal_zip_bytes)) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".txt") and "readme" not in n.lower())
        with archive.open(name) as raw:
            rows = (
                (f[0], _NormalCode(f[1]), f[2], float(f[9]), float(f[10]))
                for f in (line.rstrip("\n").split("\t") for line in io.TextIOWrapper(raw, encoding="utf-8"))
                if len(f) >= 11 and f[9] and f[10]
            )
            db.executemany("INSERT INTO raw VALUES (?, ?, ?, ?, ?)", rows)
    db.execute(
        "INSERT INTO postal SELECT country, code, MIN(place), ROUND(AVG(lat), 3), ROUND(AVG(lon), 3) FROM raw GROUP BY country, code"
    )
    db.execute(
        "INSERT INTO places SELECT country, PYLOWER(place), ROUND(AVG(lat), 3), ROUND(AVG(lon), 3) FROM raw GROUP BY country, PYLOWER(place)"
    )
    db.execute("DROP TABLE raw")
    for line in countries_text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) > 4:
            db.execute("INSERT OR IGNORE INTO countries VALUES (?, ?)", (fields[4].strip().lower(), fields[0]))
            db.execute("INSERT OR IGNORE INTO countries VALUES (?, ?)", (fields[0].lower(), fields[0]))
            db.execute("INSERT OR IGNORE INTO countries VALUES (?, ?)", (fields[1].lower(), fields[0]))
    for alias, iso in ALIASES.items():
        db.execute("INSERT OR IGNORE INTO countries VALUES (?, ?)", (alias, iso))
    db.commit()
    db.close()
    os.replace(temp, final)
    return final


def _Download(url):
    with urllib.request.urlopen(url, timeout=300) as response:
        return response.read()


def _NormalCode(code):
    return "".join(code.upper().split())


class Locator:
    """Finds the rough spot for a country, postal code and town."""

    def __init__(self, path):
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False) if path and os.path.exists(path) else None

    def Available(self):
        return self.db is not None

    def CountryCode(self, country):
        if self.db is None or not country:
            return None
        name = country.strip().lower()
        row = self.db.execute("SELECT iso FROM countries WHERE name = ?", (name,)).fetchone()
        if row is None and name.startswith("the "):
            row = self.db.execute("SELECT iso FROM countries WHERE name = ?", (name[4:],)).fetchone()
        return row[0] if row else None

    def Locate(self, country, postal_code, town=""):
        """(lat, lon) for the postal code, or for the town if the code isn't known; None if neither is."""
        iso = self.CountryCode(country)
        if iso is None:
            return None
        code = _NormalCode(postal_code or "")
        # Some countries list only the first part of their codes (the UK's
        # "SW1A", Canada's "K1A"), so try shorter and shorter beginnings.
        tries = [code] + [code[:n] for n in range(len(code) - 1, 1, -1)]
        if postal_code and " " in postal_code.strip():
            tries.insert(1, _NormalCode(postal_code.strip().split()[0]))
        for attempt in tries:
            if not attempt:
                continue
            row = self.db.execute("SELECT lat, lon FROM postal WHERE country = ? AND code = ?", (iso, attempt)).fetchone()
            if row:
                return row[0], row[1]
        if town:
            row = self.db.execute(
                "SELECT lat, lon FROM places WHERE country = ? AND place = ?", (iso, town.strip().lower())
            ).fetchone()
            if row and row[0] is not None:
                return row[0], row[1]
        return None


def RoughSpot(lat, lon):
    """The centre of the ~5 km square the point falls in, so no dot is more exact than that."""
    row = math.floor(lat / CELL_DEGREES)
    centre_lat = (row + 0.5) * CELL_DEGREES
    lon_step = CELL_DEGREES / max(math.cos(math.radians(centre_lat)), 0.05)
    column = math.floor(lon / lon_step)
    return round(centre_lat, 4), round((column + 0.5) * lon_step, 4)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "build":
        raise SystemExit(__doc__)
    print("Built", Build(sys.argv[2]))
