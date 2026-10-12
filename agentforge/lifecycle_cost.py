"""Observed lifetime costs; absent measurements remain absent, never inferred as savings."""

def summarize_lifecycle(arm):
    builds = arm.get("builds", [])
    cases = arm.get("cases", [])
    initial = [b for b in builds if b["phase"] == "initial"]
    maintenance = [b for b in builds if b["phase"] == "maintenance"]
    def costs(records):
        return {"elapsed_ms": round(sum(r.get("elapsed_ms", 0) for r in records), 3),
                "model_calls": sum(r.get("model_calls", 0) for r in records)}
    total = costs(builds + cases)
    return {"initial_build": costs(initial), "maintenance": costs(maintenance), "execution": costs(cases),
            "total": total, "passed": sum(c.get("passed", False) for c in cases), "total_cases": len(cases),
            "not_executed": sum(c.get("actual") is None for c in cases),
            "amortized_per_requested_case": {k: v / len(cases) for k, v in total.items()} if cases else None,
            "human_minutes": arm.get("human_minutes"), "monetary_cost": None,
            "note": "Requests are not tokens or prices. Missing human effort is unknown. Failed and unexecuted cases stay in the denominator."}
