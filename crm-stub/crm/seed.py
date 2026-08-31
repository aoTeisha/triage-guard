"""Seed the CRM with 20 varied mock patients.

Coverage is deliberately mixed: patients with and without prior visits, a
range of chronic conditions, first-time patients (empty history), and a few
records that line up with high-acuity / chronic demo scenarios. Run:

    python -m crm.seed                # seeds patients.db
    python -m crm.seed --db test.db   # seeds a chosen file
    python -m crm.seed --reset        # wipe and reseed
"""

from __future__ import annotations

import argparse
import json
import sqlite3

from .repository import SCHEMA, _now_iso

# (id, name, dob, conditions, prior_visits[])
PATIENTS = [
    ("P-1001", "Alon Mizrahi", "1958-03-12",
     ["hypertension", "type 2 diabetes"],
     [{"date": "2025-11-02", "acuity": 3, "notes": "chest tightness, discharged stable"},
      {"date": "2026-01-15", "acuity": 2, "notes": "shortness of breath"}]),
    ("P-1002", "Noa Cohen", "1991-07-22", [], []),  # first-time, healthy
    ("P-1003", "Yusuf Haddad", "1972-11-30",
     ["asthma"],
     [{"date": "2025-12-20", "acuity": 2, "notes": "acute asthma exacerbation"}]),
    ("P-1004", "Maya Levi", "2015-05-04",
     ["peanut allergy"],
     [{"date": "2026-02-01", "acuity": 4, "notes": "minor allergic reaction, observed"}]),
    ("P-1005", "David Friedman", "1949-09-18",
     ["atrial fibrillation", "hypertension", "CKD stage 3"],
     [{"date": "2025-10-10", "acuity": 2, "notes": "palpitations"},
      {"date": "2026-01-28", "acuity": 1, "notes": "syncope, admitted"}]),
    ("P-1006", "Rania Khalil", "1988-02-14", ["migraine"],
     [{"date": "2025-09-05", "acuity": 4, "notes": "severe headache, resolved"}]),
    ("P-1007", "Tomer Azoulay", "2001-12-01", [], []),  # first-time young adult
    ("P-1008", "Sarah Goldberg", "1965-06-25",
     ["breast cancer (in remission)", "hypothyroidism"],
     [{"date": "2025-08-19", "acuity": 3, "notes": "post-chemo fatigue"}]),
    ("P-1009", "Ibrahim Nasser", "1954-04-08",
     ["COPD", "type 2 diabetes"],
     [{"date": "2025-11-22", "acuity": 2, "notes": "COPD exacerbation"},
      {"date": "2026-02-10", "acuity": 2, "notes": "productive cough, low O2 sat"}]),
    ("P-1010", "Ella Katz", "1997-10-16", ["epilepsy"],
     [{"date": "2025-12-30", "acuity": 2, "notes": "breakthrough seizure"}]),
    ("P-1011", "Omar Suleiman", "1980-01-27", ["lower back pain (chronic)"],
     [{"date": "2026-01-05", "acuity": 4, "notes": "back pain flare"}]),
    ("P-1012", "Hila Barak", "2019-08-11", [], []),  # young child, first visit
    ("P-1013", "Moshe Klein", "1943-03-03",
     ["coronary artery disease", "hypertension", "type 2 diabetes"],
     [{"date": "2025-07-14", "acuity": 1, "notes": "STEMI, cath lab"},
      {"date": "2025-12-02", "acuity": 2, "notes": "angina, observed"}]),
    ("P-1014", "Layla Mansour", "1993-05-29", ["pregnancy (2nd trimester)"],
     [{"date": "2026-02-05", "acuity": 3, "notes": "abdominal pain, monitored"}]),
    ("P-1015", "Daniel Peretz", "1976-09-09", ["anxiety disorder"],
     [{"date": "2025-10-30", "acuity": 4, "notes": "panic episode"}]),
    ("P-1016", "Amira Odeh", "1961-12-19",
     ["rheumatoid arthritis", "osteoporosis"],
     [{"date": "2025-11-11", "acuity": 3, "notes": "joint swelling"}]),
    ("P-1017", "Gilad Shapiro", "2008-06-07", ["ADHD"], []),  # teen, no ED history
    ("P-1018", "Fatima Zahra", "1985-04-21", ["sickle cell disease"],
     [{"date": "2025-09-28", "acuity": 2, "notes": "vaso-occlusive crisis"},
      {"date": "2026-01-19", "acuity": 2, "notes": "pain crisis, IV fluids"}]),
    ("P-1019", "Yael Rosen", "1969-11-05", ["hypertension"],
     [{"date": "2025-12-12", "acuity": 3, "notes": "elevated BP, adjusted meds"}]),
    ("P-1020", "Khaled Barghouti", "1957-08-30",
     ["type 2 diabetes", "diabetic neuropathy", "hypertension"],
     [{"date": "2025-10-01", "acuity": 3, "notes": "foot ulcer"},
      {"date": "2026-02-14", "acuity": 2, "notes": "cellulitis, IV antibiotics"}]),
]


def seed(db_path: str = "patients.db", reset: bool = False) -> int:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        if reset:
            conn.execute("DELETE FROM patients")
        n = 0
        for pid, name, dob, conditions, visits in PATIENTS:
            conn.execute(
                """INSERT OR REPLACE INTO patients
                   (stable_patient_id, name, date_of_birth,
                    known_conditions, prior_visits, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (pid, name, dob, json.dumps(conditions), json.dumps(visits), _now_iso()),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed the CRM stub with mock patients.")
    ap.add_argument("--db", default="patients.db", help="SQLite file path")
    ap.add_argument("--reset", action="store_true", help="wipe existing rows first")
    args = ap.parse_args()
    n = seed(args.db, reset=args.reset)
    print(f"seeded {n} patients into {args.db}")


if __name__ == "__main__":
    main()
