#!/usr/bin/env python3
"""Regenerate the SYNTHETIC cron-drift regression fixture.

    python3 OPS/tests/fixtures/build_cron_drift_fixture.py

WHAT THIS IS NOT
    The output is NOT a recovered snapshot of any live Hermes scheduler state and
    must never be cited as evidence of one. It is machine-generated from
    cron/ge16_jobs.json with deliberate field-level drift injected, and exists only
    to exercise drift detection in compare_live_jobs().

WHY IT LIVES IN-REPO
    The fixture previously lived at /private/tmp/ge16_v2_live_cron_snapshot.json.
    The OS cleared that scratch path and the regression broke with a
    FileNotFoundError. Keeping it in-repo makes the suite hermetic.

WHAT IT MODELS
    The five RETIRED legacy scheduler jobs as they would have looked before the
    Option B identity rotation: each carries its retired legacy ID, is enabled=true
    (retired jobs were live at the time), and differs from its active target in the
    exact fields the regression asserts.
"""
import json
import pathlib

FIXTURES = pathlib.Path(__file__).resolve().parent
OPS = FIXTURES.parents[1]

PROVENANCE = "SYNTHETIC REGRESSION FIXTURE - NOT HISTORICAL EVIDENCE"
DESCRIPTION = (
    "Machine-generated pre-rotation legacy cron shape used solely to exercise "
    "drift detection in compare_live_jobs(). It is derived from cron/ge16_jobs.json "
    "with deliberate field-level drift injected. It is NOT a recovered snapshot of "
    "any live Hermes scheduler state and must never be cited as evidence of one."
)

# Emission order matches the order the regression asserts.
MAPPING = [
    ("2f817443c8e9", "441fedd48bc8"),
    ("4c3dee85457f", "2b0a9111c836"),
    ("22f17e2baabd", "c4cebfce9fb0"),
    ("b539a7a77c39", "9194211d627e"),
    ("a62815d7de8a", "152172eb38fd"),
]

# Field-level drift the regression requires, per legacy job.
DRIFT = {
    "2f817443c8e9": {"name", "workdir"},
    "4c3dee85457f": {"dependencies", "model"},
    "b539a7a77c39": {"prompt"},
}


def build_jobs():
    canonical = json.loads((OPS / "cron" / "ge16_jobs.json").read_text(encoding="utf-8"))
    by_id = {job["id"]: job for job in canonical["jobs"]}
    jobs = []
    for legacy_id, target_id in MAPPING:
        target = by_id[target_id]
        drift = DRIFT.get(legacy_id, set())
        job = {
            "id": legacy_id,
            "name": target["name"],
            "schedule": target["schedule"],
            "timezone": target["timezone"],
            "dependencies": list(target["dependencies"]),
            "workdir": target["workdir"],
            # Retired jobs were live before rotation; this is asserted drift.
            "enabled": True,
            "state": target["state"],
            "delivery": target["delivery"],
            "enabled_toolsets": list(target["enabled_toolsets"]),
            "model": target["model"],
            "provider": target["provider"],
            "model_snapshot": target["model_snapshot"],
            "provider_snapshot": target["provider_snapshot"],
            "prompt": target["prompt"],
        }
        if "name" in drift:
            job["name"] = target["name"].replace("P1.4", "P1.3").replace("P1.5", "P1.3")
        if "workdir" in drift:
            # The pre-rename legacy workdir: exactly the stale-path signal.
            job["workdir"] = target["workdir"].replace(
                "Malaysia General Election v2", "Malaysia General Election"
            )
        if "dependencies" in drift:
            job["dependencies"] = ["d48fef13c681"]
        if "model" in drift:
            job["model"] = "poolside/laguna-s-2.1:free"
        if "prompt" in drift:
            job["prompt"] = target["prompt"].replace(
                "Execute only through", "Run directly without the runner instead of"
            )
        jobs.append(job)
    return jobs


def main():
    jobs = build_jobs()
    payload = {
        "_provenance": PROVENANCE,
        "_description": DESCRIPTION,
        "_generator": "OPS/tests/fixtures/build_cron_drift_fixture.py",
        "_injected_drift": {
            "2f817443c8e9": ["name", "workdir"],
            "4c3dee85457f": ["dependencies", "model"],
            "b539a7a77c39": ["prompt"],
            "_all": ["enabled=true (retired jobs were live pre-rotation)"],
        },
        "jobs": jobs,
    }
    destination = FIXTURES / "ge16_v2_live_cron_snapshot.json"
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print("wrote %s (%d synthetic jobs)" % (destination, len(jobs)))


if __name__ == "__main__":
    main()
