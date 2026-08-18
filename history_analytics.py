"""Deterministische Langzeit-Aggregate fuer die Garmin-Tageshistorie."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from numbers import Real


AGGREGATE_METRICS = (
    "score",
    "body_battery",
    "hrv",
    "ruhepuls",
    "schlafdauer_h",
    "schlaf_score",
    "tief_min",
    "rem_min",
    "leicht_min",
    "wach_min",
    "stress_avg",
    "schritte",
    "spo2",
    "atemfrequenz",
    "tr_score",
    "vo2max",
    "kalorien_gesamt",
    "kalorien_aktiv",
    "distanz_km",
    "puls_min",
    "puls_max",
    "body_battery_min",
    "body_battery_max",
    "body_battery_geladen",
    "body_battery_verbraucht",
    "aktiv_min",
    "hochaktiv_min",
    "sitzend_min",
    "intensitaet_mod_min",
    "intensitaet_vig_min",
    "stockwerke_auf",
    "stockwerke_ab",
    "fluessigkeit_ml",
    "gewicht_kg",
    "bmi",
    "koerperfett_pct",
    "fitnessalter",
    "ausdauer_score",
)

SUM_METRICS = {
    "schritte",
    "kalorien_gesamt",
    "kalorien_aktiv",
    "distanz_km",
    "aktiv_min",
    "hochaktiv_min",
    "sitzend_min",
    "intensitaet_mod_min",
    "intensitaet_vig_min",
    "stockwerke_auf",
    "stockwerke_ab",
    "fluessigkeit_ml",
}

AGGREGATE_LEVELS = ("week", "month", "quarter", "year")
CATEGORY_METRICS = ("tr_level", "trainingsstatus")


def _number(value):
    return isinstance(value, Real) and not isinstance(value, bool)


def period_key(day: date, level: str) -> str:
    """Builds stable ISO keys for week, month, quarter and year."""
    if level == "week":
        iso_year, iso_week, _ = day.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if level == "month":
        return day.strftime("%Y-%m")
    if level == "quarter":
        return f"{day.year}-Q{((day.month - 1) // 3) + 1}"
    if level == "year":
        return str(day.year)
    raise ValueError(f"Unbekannte Aggregationsebene: {level}")


def _activity_totals(days):
    totals = {
        "aktivitaeten_anzahl": 0,
        "aktivitaeten_dauer_min": 0,
        "aktivitaeten_distanz_km": 0,
        "aktivitaeten_kalorien": 0,
    }
    types = Counter()
    available = Counter()
    for day in days:
        activities = day.get("aktivitaeten_gestern")
        if not isinstance(activities, list):
            continue
        for activity in activities:
            if not isinstance(activity, dict):
                continue
            totals["aktivitaeten_anzahl"] += 1
            activity_type = activity.get("typ") or "unbekannt"
            types[str(activity_type)] += 1
            for source, target in (
                ("dauer_min", "aktivitaeten_dauer_min"),
                ("distanz_km", "aktivitaeten_distanz_km"),
                ("kalorien", "aktivitaeten_kalorien"),
            ):
                value = activity.get(source)
                if _number(value):
                    totals[target] += value
                    available[target] += 1
    totals["aktivitaeten_dauer_min"] = round(totals["aktivitaeten_dauer_min"], 1)
    totals["aktivitaeten_distanz_km"] = round(totals["aktivitaeten_distanz_km"], 2)
    totals["aktivitaeten_kalorien"] = round(totals["aktivitaeten_kalorien"], 1)
    return totals, dict(sorted(types.items())), dict(available)


def aggregate_period(days, level: str, key: str) -> dict:
    """Aggregates one period while preserving per-metric availability counts."""
    ordered = sorted(days, key=lambda item: item["datum"])
    values = defaultdict(list)
    for item in ordered:
        for metric in AGGREGATE_METRICS:
            value = item.get(metric)
            if _number(value):
                values[metric].append(value)

    averages = {
        metric: round(sum(metric_values) / len(metric_values), 2)
        for metric, metric_values in values.items()
    }
    minima = {metric: min(metric_values) for metric, metric_values in values.items()}
    maxima = {metric: max(metric_values) for metric, metric_values in values.items()}
    sums = {
        metric: round(sum(values[metric]), 2)
        for metric in SUM_METRICS
        if metric in values
    }
    activity_sums, activity_types, activity_available = _activity_totals(ordered)
    sums.update(activity_sums)
    availability = {metric: len(metric_values) for metric, metric_values in values.items()}
    availability.update(activity_available)
    categories = {}
    for metric in CATEGORY_METRICS:
        counts = Counter(
            str(item[metric]) for item in ordered
            if item.get(metric) is not None and str(item[metric]).strip()
        )
        if counts:
            categories[metric] = dict(sorted(counts.items()))

    return {
        "periode": key,
        "ebene": level,
        "von": ordered[0]["datum"],
        "bis": ordered[-1]["datum"],
        "tage_gefunden": len(ordered),
        "mittelwerte": averages,
        "summen": sums,
        "minima": minima,
        "maxima": maxima,
        "werte_verfuegbar": availability,
        "aktivitaetstypen": activity_types,
        "kategorien": categories,
    }


def aggregate_history(days, levels=AGGREGATE_LEVELS) -> dict[str, list[dict]]:
    """Creates sorted period series from normalized private daily records."""
    valid_days = []
    for item in days:
        if not isinstance(item, dict) or not isinstance(item.get("datum"), str):
            continue
        try:
            parsed = date.fromisoformat(item["datum"])
        except ValueError:
            continue
        valid_days.append((parsed, item))

    result = {}
    for level in levels:
        groups = defaultdict(list)
        for parsed, item in valid_days:
            groups[period_key(parsed, level)].append(item)
        result[level] = [
            aggregate_period(groups[key], level, key)
            for key in sorted(groups)
        ]
    return result
