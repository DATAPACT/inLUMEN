import os
import csv
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

def main():
    # Resolve input and output directories
    input_dir = Path(os.getenv('PIPELINE_INPUT_DIR', '.'))
    output_dir = Path(os.getenv('PIPELINE_OUTPUT_DIR', '.'))

    # Input file name (as defined in the pipeline)
    input_file = input_dir / 'orders.csv'

    order_count = 0
    total_amount = Decimal('0')

    # Read CSV using the standard library
    with input_file.open(newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Increment count
            order_count += 1
            # Sum amounts with Decimal for exact precision
            amount = Decimal(row['amount'])
            total_amount += amount

    # Format total_amount with exactly two decimal places
    total_amount_str = str(total_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

    # Prepare output JSON structure
    summary = {
        "order_count": order_count,
        "total_amount": total_amount_str
    }

    # Write JSON output
    output_file = output_dir / 'summary.json'
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open('w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False)

if __name__ == "__main__":
    main()
