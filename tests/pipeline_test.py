import os
import json
from app.services.transformers import transform_frontend_payload
from app.services.maut import run_pipeline
from app.services.pipeline import run_full_pipeline
from app.services.ant_colony_opt import ACOConfig
from app.utils.validators import assert_itinerary_valid


def test_full_pipeline():
    """Test complete pipeline."""

    print("\n" + "=" * 80)
    print("FULL PIPELINE TEST: MAUT → CVRPTW → ACO")
    print("=" * 80)

    # 1. Frontend payload
    frontend_payload = {
        "title": "Singapore Test Itinerary",
        "destination": "Singapore",
        "dates": {
            "type": "specific",
            "startDate": "2025-06-01",
            "endDate": "2025-06-10",
        },
        "num_days": 10,
        "travelers": {"adults": 2, "children": 1, "pets": 1},
        "preferences": {
            "budget": "luxury",
            "pacing": "packed",
            "interests": ["art_museums", "food_culinary", "cultural_history"],
        },
    }

    print("\n[STEP 1] Transform Frontend Payload")
    print("-" * 80)
    maut_request = transform_frontend_payload(frontend_payload)
    print(f"✓ Destination: {maut_request['destination']}")
    print(f"✓ Duration: {maut_request['num_days']} days")
    print(f"✓ Pacing: {maut_request['pacing']}")
    print(f"✓ Budget: {maut_request['budget_tier']}")
    print(f"✓ Interests: {', '.join(maut_request['interest_themes'])}")

    # 2. Run MAUT
    print("\n[STEP 2] Run MAUT Pipeline")
    print("-" * 80)
    maut_output = run_pipeline(maut_request)
    places = maut_output.get("places", [])
    meta = maut_output.get("meta", {})

    print(f"✓ Status: {maut_output['status']}")
    print(f"✓ POIs selected: {len(places)}")
    print(f"✓ Themes: {', '.join(meta.get('selected_themes', []))}")
    print(f"✓ Candidates in: {meta.get('count_in', 0)}")
    print(f"✓ Candidates out: {meta.get('count_out', 0)}")

    # Save MAUT output
    output_dir = os.path.dirname(__file__)
    maut_path = os.path.join(output_dir, "pipeline_maut_output.json")
    with open(maut_path, "w", encoding="utf-8") as f:
        json.dump(maut_output, f, indent=2)
    print(f"✓ Saved: {maut_path}")

    # 3. Enrich MAUT output
    maut_output["meta"]["dates"] = frontend_payload["dates"]
    maut_output["meta"]["num_days"] = maut_request["num_days"]

    # 4. Extract hotel - use MAUT's selected hotel
    print("\n[STEP 3] Extract Hotel Information")
    print("-" * 80)

    selected_hotel = meta.get("selected_hotel")
    if selected_hotel:
        coords = selected_hotel.get("coordinates") or {}
        hotel = {
            "id": selected_hotel["id"],
            "name": selected_hotel["name"],
            "lat": coords.get("lat") or selected_hotel.get("latitude"),
            "lon": coords.get("lng") or selected_hotel.get("longitude"),
        }
        print(f"✓ Using MAUT-selected hotel: {hotel['name']}")
    else:
        # Fallback: extract from places list
        accommodations = [
            p for p in places if "accommodation" in p.get("poi_roles", [])
        ]

        if accommodations:
            hotel_poi = accommodations[0]
            coords = hotel_poi.get("coordinates") or {}
            hotel = {
                "id": hotel_poi["id"],
                "name": hotel_poi["name"],
                "lat": coords.get("lat") or hotel_poi.get("latitude"),
                "lon": coords.get("lng") or hotel_poi.get("longitude"),
            }
            print(f"✓ Using accommodation from places: {hotel['name']}")
        else:
            hotel = {
                "id": "default_hotel",
                "name": "Marina Bay Hotel",
                "lat": 1.290270,
                "lon": 103.851959,
            }
            print(f"✓ Using default hotel: {hotel['name']}")

    print(f"  Location: ({hotel['lat']:.6f}, {hotel['lon']:.6f})")

    # 5. Run full pipeline WITHOUT ACO
    print("\n[STEP 4] Run Pipeline WITHOUT ACO (CVRPTW only)")
    print("-" * 80)

    pipeline_no_aco = run_full_pipeline(
        maut_output=maut_output,
        hotel=hotel,
        pacing=maut_request["pacing"],
        mandatory=None,
        time_limit_sec=20,
        use_aco=False,
    )

    if pipeline_no_aco.get("status") == "success":
        print(f"✓ Status: {pipeline_no_aco['status']}")
        print(f"✓ Days: {len(pipeline_no_aco['days'])}")

        for i, day in enumerate(pipeline_no_aco["days"]):
            print(f"\n  Day {i+1} ({day['date']}):")
            print(f"    - Stops: {len(day['stops'])}")
            print(f"    - Meals: {day['meals']}")
            print(f"    - Distance: {day.get('total_distance', 0):.2f} km")
            print(f"    - Method: {day.get('optimization_method', 'N/A')}")

            # Show first 3 stops
            for j, stop in enumerate(day["stops"][:3]):
                print(
                    f"      {j+1}. {stop['arrival']} - {stop['name']} ({stop['role']})"
                )
            if len(day["stops"]) > 3:
                print(f"      ... and {len(day['stops']) - 3} more stops")

        meta = pipeline_no_aco.get("meta", {})
        print(f"\n  Total distance: {meta.get('total_distance', 0):.2f} km")
        print(f"  Total stops: {meta.get('total_stops', 0)}")
    else:
        print(f"✗ Pipeline failed: {pipeline_no_aco.get('error')}")

    # Save output
    no_aco_path = os.path.join(output_dir, "pipeline_no_aco_output.json")
    with open(no_aco_path, "w", encoding="utf-8") as f:
        json.dump(pipeline_no_aco, f, indent=2)
    print(f"\n✓ Saved: {no_aco_path}")

    # 6. Run full pipeline WITH ACO
    print("\n[STEP 5] Run Pipeline WITH ACO (CVRPTW + ACO)")
    print("-" * 80)

    aco_config = ACOConfig(
        n_ants=20,
        n_iterations=50,
        alpha=1.0,
        beta=2.0,
        evaporation_rate=0.5,
        n_best=5,
    )

    pipeline_with_aco = run_full_pipeline(
        maut_output=maut_output,
        hotel=hotel,
        pacing=maut_request["pacing"],
        mandatory=None,
        time_limit_sec=20,
        use_aco=True,
        aco_config=aco_config,
    )

    if pipeline_with_aco.get("status") == "success":
        print(f"✓ Status: {pipeline_with_aco['status']}")
        print(f"✓ Days: {len(pipeline_with_aco['days'])}")

        for i, day in enumerate(pipeline_with_aco["days"]):
            print(f"\n  Day {i+1} ({day['date']}):")
            print(f"    - Stops: {len(day['stops'])}")
            print(f"    - Meals: {day['meals']}")
            print(f"    - Distance: {day.get('total_distance', 0):.2f} km")
            print(f"    - Method: {day.get('optimization_method', 'N/A')}")

            # Show first 3 stops
            for j, stop in enumerate(day["stops"][:3]):
                print(
                    f"      {j+1}. {stop['arrival']} - {stop['name']} ({stop['role']})"
                )
            if len(day["stops"]) > 3:
                print(f"      ... and {len(day['stops']) - 3} more stops")

        meta = pipeline_with_aco.get("meta", {})
        print(f"\n  Total distance: {meta.get('total_distance', 0):.2f} km")
        print(f"  Total stops: {meta.get('total_stops', 0)}")
        print(f"  ACO applied: {meta.get('optimization_applied', False)}")
    else:
        print(f"✗ Pipeline failed: {pipeline_with_aco.get('error')}")

    # Save output
    with_aco_path = os.path.join(output_dir, "pipeline_with_aco_output.json")
    with open(with_aco_path, "w", encoding="utf-8") as f:
        json.dump(pipeline_with_aco, f, indent=2)
    print(f"\n✓ Saved: {with_aco_path}")

    # 7. Compare results
    print("\n[STEP 6] Compare Results")
    print("-" * 80)

    if (
        pipeline_no_aco.get("status") == "success"
        and pipeline_with_aco.get("status") == "success"
    ):

        no_aco_dist = pipeline_no_aco["meta"]["total_distance"]
        with_aco_dist = pipeline_with_aco["meta"]["total_distance"]
        improvement = (
            ((no_aco_dist - with_aco_dist) / no_aco_dist * 100)
            if no_aco_dist > 0
            else 0
        )

        print(f"CVRPTW only:     {no_aco_dist:.2f} km")
        print(f"CVRPTW + ACO:    {with_aco_dist:.2f} km")
        print(f"Improvement:     {improvement:+.2f}%")

        if improvement > 0:
            print(f"\n✓ ACO optimization reduced distance by {improvement:.2f}%")
        elif improvement < 0:
            print(
                f"\n⚠ ACO increased distance by {abs(improvement):.2f}% (may prioritize other factors)"
            )
        else:
            print("\n= No distance change (routes may be similar)")

    # 8. Validation
    print("\n[STEP 7] Validation")
    print("-" * 80)

    # MAUT validation
    assert maut_output is not None, "MAUT output is None"
    assert maut_output["status"] == "ok", f"MAUT failed: {maut_output.get('status')}"
    assert len(places) > 0, "No POIs selected"

    # Pipeline without ACO validation
    assert pipeline_no_aco is not None, "Pipeline (no ACO) output is None"
    assert (
        pipeline_no_aco.get("status") == "success"
    ), f"Pipeline (no ACO) failed: {pipeline_no_aco.get('error')}"
    days_no_aco = pipeline_no_aco.get("days", [])
    assert (
        len(days_no_aco) == frontend_payload["num_days"]
    ), f"Expected {frontend_payload['num_days']} days (no ACO), got {len(days_no_aco)}"

    assert_itinerary_valid(
        cvrptw_output=pipeline_no_aco,
        maut_output=maut_output,
        pacing=maut_request["pacing"],
        allow_warnings=False,
    )

    # Pipeline with ACO validation
    assert pipeline_with_aco is not None, "Pipeline (with ACO) output is None"
    assert (
        pipeline_with_aco.get("status") == "success"
    ), f"Pipeline (with ACO) failed: {pipeline_with_aco.get('error')}"
    days_aco = pipeline_with_aco.get("days", [])
    assert (
        len(days_aco) == frontend_payload["num_days"]
    ), f"Expected {frontend_payload['num_days']} days (with ACO), got {len(days_aco)}"

    assert_itinerary_valid(
        cvrptw_output=pipeline_with_aco,
        maut_output=maut_output,
        pacing=maut_request["pacing"],
        allow_warnings=False,
    )

    # Validate ACO preserves POI sets (only reorders)
    for day_no_aco, day_aco in zip(days_no_aco, days_aco):
        ids_no_aco = sorted([s["poi_id"] for s in day_no_aco["stops"]])
        ids_aco = sorted([s["poi_id"] for s in day_aco["stops"]])
        assert ids_no_aco == ids_aco, "ACO must not add/remove POIs, only reorder"

    # Validate distance comparison (ACO should not make it significantly worse)
    dist_no_aco = pipeline_no_aco["meta"]["total_distance"]
    dist_aco = pipeline_with_aco["meta"]["total_distance"]
    assert (
        dist_aco <= dist_no_aco * 1.2
    ), f"ACO distance {dist_aco:.2f}km is worse than CVRPTW {dist_no_aco:.2f}km"

    print("✓ All assertions passed")

    print("\n" + "=" * 80)
    print("✓ PIPELINE TEST COMPLETED SUCCESSFULLY")
    print("=" * 80)
    print("\nGenerated files:")
    print(f"  - {maut_path}")
    print(f"  - {no_aco_path}")
    print(f"  - {with_aco_path}")
    print()


if __name__ == "__main__":
    test_full_pipeline()
