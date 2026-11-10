"""virtual_waiter_prototype.py - interactive CLI for the Perpetual Bistro virtual waiter.

Setup:
    pip install ollama
    ollama pull qwen3:4b
    ollama serve            (if not already running)

Run:
    python virtual_waiter_prototype.py                # normal
    python virtual_waiter_prototype.py --reset-db     # restore menu + stock to defaults
"""
import argparse
import sys

import db_tools as db
from fsm_controller import DEFAULT_MODEL, WaiterAgent


def main():
    p = argparse.ArgumentParser(description="Perpetual Bistro virtual waiter (Ollama + SQLite)")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--reset-db", action="store_true", help="recreate restaurant.db from the SQL script")
    p.add_argument("--log", default="sample_conversation_log.json")
    args = p.parse_args()
    sys.stdout.reconfigure(errors="replace")      # emoji from the model must not crash cmd.exe

    db.init_db(reset=args.reset_db)
    agent = WaiterAgent(model=args.model)
    print("=" * 60 + "\n PERPETUAL BISTRO - Virtual Waiter   (type 'quit' to exit)\n" + "=" * 60)
    print("Waiter:", agent.start())

    try:
        while not agent.finished:
            try:
                user = input("\nYou: ").strip()
            except EOFError:
                break
            if not user:
                continue
            if user.lower() in ("quit", "exit"):
                break
            print("\nWaiter:", agent.handle(user))
    except KeyboardInterrupt:
        print("\n(interrupted)")
    finally:
        if agent.log:
            agent.save_log(args.log)
            print(f"\n[conversation log saved to {args.log}]")


if __name__ == "__main__":
    main()
