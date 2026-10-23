#!/usr/bin/env python3
"""Route a proposed review revision without masking genuine findings."""

from __future__ import annotations

import argparse
import json


CAUSES = {"finding_driven", "infrastructure_only"}


def decide(cause: str, consecutive_infrastructure_rounds: int) -> dict[str, object]:
    if cause not in CAUSES:
        raise ValueError(f"unknown round cause: {cause}")
    if consecutive_infrastructure_rounds < 0:
        raise ValueError("consecutive infrastructure rounds must be non-negative")
    if cause == "finding_driven":
        return {
            "cause": cause,
            "route": "current_candidate",
            "consecutive_infrastructure_rounds": 0,
        }
    count = consecutive_infrastructure_rounds + 1
    return {
        "cause": cause,
        "route": "separate_follow_up" if count >= 2 else "current_candidate",
        "consecutive_infrastructure_rounds": count,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cause", required=True, choices=sorted(CAUSES))
    parser.add_argument(
        "--consecutive-infrastructure-rounds", required=True, type=int
    )
    args = parser.parse_args()
    try:
        result = decide(args.cause, args.consecutive_infrastructure_rounds)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
