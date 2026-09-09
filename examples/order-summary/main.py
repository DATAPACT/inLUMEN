"""Summarize orders using the standard inLUMEN Task workspace contract."""
import csv
import json
import os
from decimal import Decimal
from pathlib import Path


def main():
    input_dir = Path(os.environ["PIPELINE_INPUT_DIR"])
    output_dir = Path(os.environ["PIPELINE_OUTPUT_DIR"])
    with (input_dir / "orders.csv").open(newline="", encoding="utf-8") as handle:
        orders = list(csv.DictReader(handle))
    total = sum((Decimal(order["amount"]) for order in orders), Decimal("0"))
    summary = {"order_count": len(orders), "total_amount": f"{total:.2f}"}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Summarized {len(orders)} orders: {total:.2f}")


if __name__ == "__main__":
    main()
