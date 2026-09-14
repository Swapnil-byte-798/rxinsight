"""Central configuration for the RxInsight ETL.

Everything tunable lives here so the pipeline, generator and tests agree on
one source of truth rather than each carrying its own copy of a magic number.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- paths ------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SQL_DIR = PROJECT_ROOT / "sql"

HCP_CSV = DATA_DIR / "hcp.csv"
RX_CSV = DATA_DIR / "prescriptions.csv"
CALLS_CSV = DATA_DIR / "sales_calls.csv"
REJECTS_DIR = DATA_DIR / "rejects"

# --- database ---------------------------------------------------------------
# Host port 5544 rather than 5432/5433, which are usually already occupied.
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://rxinsight:rxinsight@localhost:5544/rxinsight"
)

# --- generation scale -------------------------------------------------------
# The index-tuning measurement is meaningless on a small table: Postgres will
# sequentially scan anything that fits comfortably in cache no matter what
# indexes exist. ~2M prescription rows is the smallest size where the composite
# index visibly changes the plan.
N_HCPS = 2_000
N_TERRITORIES = 50
N_MONTHS = 24
START_DATE = "2024-01-01"
TARGET_RX_ROWS = 2_000_000

RANDOM_SEED = 42

# Rows pulled from staging per batch when building the facts. The pipeline never
# holds more than one batch of prescription rows in memory, so peak RSS is a
# property of this constant rather than of the table size. 2.2M rows in one frame
# needs gigabytes once pandas copies it a few times; at 250k it is comfortable on
# a laptop.
CHUNK_ROWS = 250_000

# Fraction of HCPs who change decile or territory mid-period. These are the
# rows that exercise SCD Type 2 — without them the dimension is just a lookup.
SCD_CHANGE_RATE = 0.02

# Deliberately malformed rows, so the reject path and the data-quality tests
# have something real to catch rather than passing vacuously.
DIRTY_ROW_COUNT = 500

PRODUCTS = [
    # (product_code, brand_name, molecule, therapeutic_area, is_competitor)
    ("RX-CARDIO-01", "Cardiova",  "Atorvastatin",  "Cardiology",   False),
    ("RX-ENDO-01",   "Glucoron",  "Metformin",     "Endocrinology", False),
    ("RX-RESP-01",   "Pulmovent", "Salbutamol",    "Respiratory",   False),
    ("CM-CARDIO-01", "Lipitrex",  "Atorvastatin",  "Cardiology",    True),
    ("CM-CARDIO-02", "Statinex",  "Rosuvastatin",  "Cardiology",    True),
    ("CM-ENDO-01",   "Glycomet",  "Metformin",     "Endocrinology", True),
    ("CM-RESP-01",   "Airomax",   "Salbutamol",    "Respiratory",   True),
    ("CM-RESP-02",   "Bronchol",  "Formoterol",    "Respiratory",   True),
]

SPECIALTIES = [
    "Cardiologist", "Endocrinologist", "Pulmonologist",
    "General Physician", "Internal Medicine",
]

REGIONS = ["North", "South", "East", "West", "Central"]

CALL_TYPES = ["DETAIL", "SAMPLE", "FOLLOW_UP"]
