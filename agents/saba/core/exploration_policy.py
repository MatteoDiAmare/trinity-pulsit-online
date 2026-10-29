"""Bounded exploration scoring for research tasks."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Opportunity:
    name: str
    expected_value: float
    uncertainty: float
    cost: float


def rank(opportunities, curiosity_weight=0.2):
    if curiosity_weight < 0:
        raise ValueError("curiosity_weight must be nonnegative")
    return sorted(opportunities, key=lambda o: o.expected_value + curiosity_weight * o.uncertainty - o.cost, reverse=True)
