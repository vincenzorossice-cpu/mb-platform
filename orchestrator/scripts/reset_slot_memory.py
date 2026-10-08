"""Riparte da zero per UNA slot: sposta (non cancella) la sua memoria in slot_memory/_backup/<data-ora>/.

Uso:
    python -m orchestrator.scripts.reset_slot_memory --slot "Wrappin' More Gold"
"""
import argparse
import sys

from orchestrator.modules.slot_autopilot.slot_map import SlotMemory


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sposta in backup la memoria di una slot.")
    parser.add_argument("--slot", required=True, help="Nome della slot.")
    parser.add_argument("--memory-dir", default="slot_memory")
    args = parser.parse_args(argv)
    result = SlotMemory(args.memory_dir).reset(args.slot)
    if not result.success:
        print(f"ERRORE: {result.error}")
        return 1
    if not result.data:
        print(f"Nessun file di memoria per '{args.slot}': niente da spostare.")
        return 0
    print(f"Spostati {len(result.data)} file in backup:")
    for path in result.data:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
