"""init_db.py - create (or reset) restaurant.db from init_restaurant_db.sql.

    python init_db.py             # create the database if it is missing (rebuilds it if the SQL version changed)
    python init_db.py --reset     # wipe and recreate: menu and stock go back to the defaults
    python init_db.py --demo      # also add sample floor-plan and manager data (staff, expenses, a week of orders)

app.py runs the same initialisation on start-up, so this script is only needed to prepare or reset the
database by hand. The schema and seed data live in init_restaurant_db.sql; the SQLite helpers live in db_tools.py.
"""
import argparse

import db_tools as db


def main():
    p = argparse.ArgumentParser(description="Create or reset the Perpetual Bistro SQLite database")
    p.add_argument("--reset", action="store_true", help="delete restaurant.db and recreate it from the SQL script")
    p.add_argument("--demo", action="store_true", help="add sample floor-plan and manager data")
    args = p.parse_args()

    db.init_db(reset=args.reset)
    with db.db() as c:
        items = c.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0]
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    print(f"Database ready: {db.DB_PATH.name} ({items} menu items; tables: {', '.join(tables)})")

    if args.demo:
        from ui_dashboard import floor_plan, manager      # imported late: the demo data is optional
        floor_plan.ensure_schema()
        manager.ensure_schema()
        floor_plan.seed_demo()
        manager.seed_demo()


if __name__ == "__main__":
    main()
