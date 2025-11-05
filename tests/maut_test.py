import json
import os
from app.services.maut import run_pipeline

TEST_PATH = os.path.join(os.path.dirname(__file__), "maut_test.json")

if __name__ == "__main__":
    # Ensure test file exists or write a default one
    if not os.path.exists(TEST_PATH):
        print(f"Test file {TEST_PATH} not found. Please create it before running the test.")
        exit(1)

    res = run_pipeline()
    with open("tests/maut_output.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)


