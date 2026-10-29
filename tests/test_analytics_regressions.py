"""Business-result acceptance checks. All fixture writes roll back."""

import psycopg2
import pytest
from etl import config as C
from etl import load_staging as L
from etl.pipeline import _date_bounds


@pytest.fixture
def db(conn, loaded):
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.autocommit = True


def execute(c, sql):
    with c.cursor() as q:
        q.execute(sql)
        return q.fetchall() if q.description else []


def fixture(c):
    execute(
        c,
        """
 CREATE TEMP TABLE dim_date(date_key int,full_date date,year_month text,year int);
 CREATE TEMP TABLE dim_product(product_key int,brand_name text,is_competitor bool,therapeutic_area text);
 CREATE TEMP TABLE dim_hcp(hcp_key int,hcp_id text,full_name text,specialty text,decile int,territory_key int,valid_from date,valid_to date,is_current bool);
 CREATE TEMP TABLE dim_territory(territory_key int,territory_code text,territory_name text,region text);
 CREATE TEMP TABLE fact_prescriptions(date_key int,hcp_key int,product_key int,territory_key int,trx_count int,gross_sales numeric);
 CREATE TEMP TABLE fact_sales_calls(date_key int,hcp_key int,product_key int,territory_key int);
 INSERT INTO dim_date VALUES(20240531,'2024-05-31','2024-05',2024),(20240601,'2024-06-01','2024-06',2024),(20240620,'2024-06-20','2024-06',2024);
 INSERT INTO dim_product VALUES(1,'CardioOwn',false,'Cardiology'),(2,'CardioCompetitor',true,'Cardiology'),(3,'EndoOwn',false,'Endocrinology');
 INSERT INTO dim_hcp VALUES(1,'H1','Dr A','GP',5,1,'2024-01-01','2024-05-31',false),(2,'H1','Dr A','GP',5,2,'2024-06-01','9999-12-31',true);
 INSERT INTO dim_territory VALUES(1,'T1','Territory 1','North'),(2,'T2','Territory 2','South');
 """,
    )


def report(c, name):
    sql = (C.SQL_DIR / "analytics" / name).read_text().replace("warehouse.", "pg_temp.")
    with c.cursor() as q:
        q.execute(sql)
        names = [v.name for v in q.description]
        return [dict(zip(names, row)) for row in q.fetchall()]


def test_brand_share_uses_therapeutic_market_denominator(db):
    fixture(db)
    execute(
        db,
        "INSERT INTO fact_prescriptions VALUES(20240601,2,1,2,60,600),(20240601,2,2,2,40,400),(20240601,2,3,2,100,1000)",
    )
    rows = report(db, "brand_share_mom.sql")
    own = next(r for r in rows if r["brand_name"] == "CardioOwn")
    assert own["share_pct"] == 60, (
        f"Cardiology share should be 60/(60+40)=60%; got {own['share_pct']}%"
    )


def test_brand_share_totals_100_across_current_report_universe(db):
    fixture(db)
    execute(
        db,
        "INSERT INTO fact_prescriptions VALUES(20240601,2,1,2,60,600),(20240601,2,2,2,40,400),(20240601,2,3,2,100,1000)",
    )
    from collections import defaultdict

    totals = defaultdict(int)
    for r in report(db, "brand_share_mom.sql"):
        totals[r["therapeutic_area"]] += r["share_pct"]
    assert totals and all(value == 100 for value in totals.values())


def test_call_before_scd_transfer_still_exposes_same_hcp(db):
    fixture(db)
    execute(
        db,
        "INSERT INTO fact_sales_calls VALUES(20240531,1,1,1); INSERT INTO fact_prescriptions VALUES(20240601,2,1,2,10,100)",
    )
    rows = report(db, "call_effectiveness.sql")
    national = [r for r in rows if r["stratum"] == "ALL DECILES"]
    assert national[0]["cohort"] == "CALLED", national


def test_call_in_same_hcp_version_is_recognized(db):
    fixture(db)
    execute(
        db,
        "UPDATE dim_hcp SET valid_to='2024-04-30' WHERE hcp_key=1; UPDATE dim_hcp SET valid_from='2024-05-01' WHERE hcp_key=2; INSERT INTO fact_sales_calls VALUES(20240531,2,1,2); INSERT INTO fact_prescriptions VALUES(20240601,2,1,2,10,100)",
    )
    rows = report(db, "call_effectiveness.sql")
    assert next(r for r in rows if r["stratum"] == "ALL DECILES")["cohort"] == "CALLED"


def test_two_scd_versions_in_month_are_one_hcp_month(db):
    fixture(db)
    execute(
        db,
        "UPDATE dim_hcp SET valid_to='2024-06-14' WHERE hcp_key=1; UPDATE dim_hcp SET valid_from='2024-06-15' WHERE hcp_key=2; INSERT INTO fact_prescriptions VALUES(20240601,1,1,1,10,100),(20240620,2,1,2,20,200)",
    )
    rows = report(db, "call_effectiveness.sql")
    n = sum(r["hcp_months"] for r in rows if r["stratum"] == "ALL DECILES")
    assert n == 1, f"One person in one month was counted as {n} HCP-months"


def test_calendar_invalid_source_date_is_quarantined_before_cast(db):
    execute(
        db,
        "INSERT INTO staging.stg_prescriptions VALUES('2024-02-30','HCP-00001','RX-CARDIO-01','1','0','1','10')",
    )
    bounds = _date_bounds(db)
    assert all(bounds)


def test_bulk_load_restores_and_validates_foreign_key(db):
    execute(
        db,
        "CREATE TEMP TABLE audit_parent(id int primary key); CREATE TEMP TABLE audit_child(id int references audit_parent(id)); INSERT INTO audit_parent VALUES(1)",
    )
    with L.constraints_suspended(db, ["audit_child"]) as n:
        assert n == 1
        execute(db, "INSERT INTO audit_child VALUES(1)")
    assert (
        execute(
            db,
            "SELECT count(*) FROM pg_constraint WHERE conrelid='audit_child'::regclass AND contype='f' AND convalidated",
        )[0][0]
        == 1
    )


def test_invalid_bulk_load_cannot_leave_orphan_committed(db):
    execute(
        db,
        "CREATE TEMP TABLE audit_parent(id int primary key); CREATE TEMP TABLE audit_child(id int references audit_parent(id))",
    )
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with L.constraints_suspended(db, ["audit_child"]):
            execute(db, "INSERT INTO audit_child VALUES(999)")
    db.rollback()
    assert execute(db, "SELECT to_regclass('pg_temp.audit_child')")[0][0] is None


def test_database_rejects_an_inverted_scd_window(db):
    territory = execute(
        db, "SELECT territory_key FROM warehouse.dim_territory LIMIT 1"
    )[0][0]
    with pytest.raises(psycopg2.errors.CheckViolation):
        with db.cursor() as q:
            q.execute(
                "INSERT INTO warehouse.dim_hcp(hcp_id,full_name,specialty,territory_key,valid_from,valid_to,is_current) VALUES('INVALID-SCD-TEST','Test','GP',%s,'2024-06-01','2024-05-31',false)",
                (territory,),
            )


def test_territory_quota_rounds_an_exact_half_up(db):
    fixture(db)
    execute(
        db,
        """
        UPDATE dim_hcp SET territory_key=1 WHERE hcp_key=2;
        INSERT INTO dim_date VALUES
          (20240101,'2024-01-01','2024-01',2024),
          (20240201,'2024-02-01','2024-02',2024),
          (20240301,'2024-03-01','2024-03',2024),
          (20240401,'2024-04-01','2024-04',2024),
          (20240501,'2024-05-01','2024-05',2024);
        INSERT INTO fact_prescriptions VALUES
          (20240101,1,1,1,1446,100),(20240201,1,1,1,1446,100),
          (20240301,1,1,1,1446,100),(20240401,1,1,1,1446,100),
          (20240501,1,1,1,1446,100),(20240601,2,1,1,1445,100);
    """,
    )
    rows = report(db, "territory_attainment.sql")
    assert len(rows) == 6
    assert all(row["target_trx"] == 1562 for row in rows)
