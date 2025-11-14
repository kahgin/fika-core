# Test Suite

## Running Tests

All tests should be run from the `fika-core` directory.

### Individual Tests

```bash
# Test MAUT pipeline
pytest tests/maut_test.py -v

# Test CVRPTW
pytest tests/cvrptw_test.py -v

# Test full integration
pytest tests/integration_test.py -v
```

### Run All Tests

```bash
pytest tests/ -v
```

### Run Directly (without pytest)

```bash
# MAUT test
python tests/maut_test.py

# CVRPTW test
python tests/cvrptw_test.py

# Integration test
python tests/integration_test.py
```

## Test Files

### `maut_test.py`
Tests MAUT scoring and POI selection.

**Input**: `maut_test.json` (MAUT request format)
**Output**: `maut_output.json` (scored POIs)

### `cvrptw_test.py`
Tests CVRPTW routing with MAUT output.

**Input**: `maut_test.json` → runs MAUT first
**Output**: `cvrptw_output.json` (daily routes)

### `integration_test.py`
Tests complete pipeline: Frontend → MAUT → CVRPTW.

**Input**: Hardcoded frontend payload
**Output**: 
- `integration_maut_output.json`
- `integration_cvrptw_output.json`

## Test Data

### `maut_test.json`
MAUT request format (internal):
```json
{
  "destination": "Singapore",
  "num_days": 3,
  "budget_tier": "sensible",
  "pacing": "balanced",
  "interest_themes": ["shopping", "food_culinary"],
  "flags": {
    "has_child": true,
    "has_pets": false,
    "wheelchair_accessible": false,
    "is_muslim": false,
    "exclude_nightlife": false
  }
}
```

## Expected Outputs

### MAUT Output
```json
{
  "status": "ok",
  "places": [...],
  "total_distance": 0.0,
  "total_time": 0,
  "route_order": [],
  "meta": {
    "selected_themes": ["shopping", "food_culinary", "nature"],
    "count_in": 50,
    "count_out": 30
  }
}
```

### CVRPTW Output
```json
{
  "days": [
    {
      "date": "2024-06-01",
      "stops": [
        {
          "poi_id": "...",
          "name": "...",
          "role": "attraction",
          "arrival": "09:30",
          "start_service": "09:30",
          "depart": "11:00"
        }
      ],
      "meals": 2
    }
  ]
}
```

## Troubleshooting

### Import Errors
Make sure you're in `fika-core` directory:
```bash
cd /home/kahgin/fika/fika-core
```

### Database Connection
Tests require `.env` with:
```
SUPABASE_URL=...
SUPABASE_KEY=...
```

### Empty Results
Check that database has POIs for the test destination (Singapore).
