import json
from pathlib import Path

ROOT = Path(__file__).parent
SOURCE = ROOT / "tests" / "generated_complex_customers_50.jsonl"
TARGET = ROOT / "tests" / "generated_complex_customers_25.jsonl"

rows = [json.loads(x) for x in SOURCE.read_text(encoding="utf-8").splitlines() if x.strip()][:25]
TARGET.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
print(f"Generated {len(rows)} complex tests: {TARGET}")
