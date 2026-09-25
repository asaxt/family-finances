"""Deterministic annual planning estimates; no data access or persistence."""
import math


class ProjectionError(ValueError):
    pass


FIELDS = {
    "current_age": (18, 109, "Current age"),
    "end_age": (19, 110, "Plan through age"),
    "retirement_age": (18, 109, "Retirement age"),
    "starting_assets": (0, 1_000_000_000, "Starting investments"),
    "annual_savings": (0, 100_000_000, "Annual savings"),
    "annual_spending": (0, 100_000_000, "Annual retirement spending"),
    "annual_income": (0, 100_000_000, "Annual retirement income"),
    "income_age": (18, 110, "Retirement income start age"),
    "growth_rate": (-30, 30, "Annual investment growth"),
    "inflation_rate": (0, 15, "Annual inflation"),
    "expense_amount": (0, 100_000_000, "One-time expense"),
    "expense_age": (18, 109, "One-time expense age"),
}


def validate(values):
    if not isinstance(values, dict) or set(values) != set(FIELDS):
        raise ProjectionError("Complete all planning assumptions before calculating.")
    result = {}
    for key, (low, high, label) in FIELDS.items():
        value = values[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not low <= value <= high):
            raise ProjectionError(f"{label} must be a number between {low:g} and {high:g}.")
        if key.endswith("age") and value != int(value):
            raise ProjectionError(f"{label} must be a whole number.")
        result[key] = value
    if not result["current_age"] <= result["retirement_age"] < result["end_age"]:
        raise ProjectionError("Retirement age must be at least your current age and before the plan ends.")
    if result["expense_amount"] and not result["current_age"] <= result["expense_age"] < result["end_age"]:
        raise ProjectionError("The one-time expense age must fall within the plan.")
    return result


def project(values):
    plan = validate(values)
    growth = plan["growth_rate"] / 100
    inflation = plan["inflation_rate"] / 100
    balance = plan["starting_assets"]
    rows = [{"age": int(plan["current_age"]), "elapsed": 0, "factor": 1,
             "assets": round(balance, 2), "savings": 0, "income": 0,
             "spending": 0, "expense": 0, "growth": 0, "shortfall": 0}]
    first_shortfall_age = None
    total_real_shortfall = 0
    for age in range(int(plan["current_age"]), int(plan["end_age"])):
        elapsed = age - plan["current_age"]
        factor = (1 + inflation) ** elapsed
        retired = age >= plan["retirement_age"]
        savings = 0 if retired else plan["annual_savings"] * factor
        income = plan["annual_income"] * factor if retired and age >= plan["income_age"] else 0
        spending = plan["annual_spending"] * factor if retired else 0
        expense = plan["expense_amount"] * factor if age == plan["expense_age"] else 0
        investment_growth = balance * growth
        closing = balance + investment_growth + savings + income - spending - expense
        shortfall = max(0, -closing)
        if shortfall > 0 and first_shortfall_age is None:
            first_shortfall_age = age
        end_factor = (1 + inflation) ** (elapsed + 1)
        total_real_shortfall += shortfall / end_factor
        balance = max(0, closing)
        rows.append({"age": age + 1, "elapsed": int(elapsed + 1), "factor": end_factor,
                     **{key: round(value, 2) for key, value in {
                         "assets": balance, "savings": savings, "income": income,
                         "spending": spending, "expense": expense,
                         "growth": investment_growth, "shortfall": shortfall}.items()}})
    retirement = next(row for row in rows if row["age"] == plan["retirement_age"])
    return {"rows": rows, "retirement": retirement, "final": rows[-1],
            "first_shortfall_age": first_shortfall_age,
            "total_real_shortfall": round(total_real_shortfall, 2)}
