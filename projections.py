"""Annual retirement and taxable investment estimates; no persistence."""
import math

END_AGE = 95


class ProjectionError(ValueError):
    pass


FIELDS = {
    "annual_income": (0, 100_000_000, "Annual income"),
    "tax_advantaged_rate": (0, 100, "Tax-advantaged saving percentage"),
    "withdrawal_rate": (0, 100, "Retirement withdrawal percentage"),
    "retirement_age": (18, END_AGE - 1, "Retirement age"),
    "current_age": (18, END_AGE - 1, "Current age"),
    "starting_assets": (0, 1_000_000_000, "Starting investments"),
    "inflation_rate": (0, 15, "Annual inflation"),
    "growth_rate": (-30, 30, "Annual investment growth"),
}


def validate(values):
    if not isinstance(values, dict) or set(values) != set(FIELDS):
        raise ProjectionError("Complete the eight planning inputs before calculating.")
    for key, (low, high, label) in FIELDS.items():
        value = values[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not low <= value <= high):
            raise ProjectionError(f"{label} must be a number between {low:g} and {high:g}.")
        if key.endswith("age") and value != int(value):
            raise ProjectionError(f"{label} must be a whole number.")
    if values["retirement_age"] < values["current_age"]:
        raise ProjectionError("Retirement age must be at least your current age. Use your current age if already retired.")
    return dict(values)


def project(values, annual_spending, retirement_share=1):
    plan = validate(values)
    if (not isinstance(annual_spending, (int, float)) or isinstance(annual_spending, bool)
            or not math.isfinite(annual_spending) or annual_spending < 0):
        raise ProjectionError("A usable 12-month spending estimate is needed before calculating.")
    if not isinstance(retirement_share, (int, float)) or not 0 <= retirement_share <= 1:
        raise ProjectionError("The starting investment split could not be determined.")
    growth = plan["growth_rate"] / 100
    inflation = plan["inflation_rate"] / 100
    retirement_balance = plan["starting_assets"] * retirement_share
    taxable_balance = plan["starting_assets"] - retirement_balance
    annual_tax_advantaged = plan["annual_income"] * plan["tax_advantaged_rate"] / 100
    annual_taxable = plan["annual_income"] - annual_tax_advantaged - annual_spending
    rows = [{"age": int(plan["current_age"]), "factor": 1,
             "assets": round(plan["starting_assets"], 2),
             "retirement_assets": round(retirement_balance, 2), "taxable_assets": round(taxable_balance, 2),
             "income": 0, "tax_advantaged_savings": 0, "taxable_cash_flow": 0,
             "withdrawal": 0, "spending": 0, "shortfall": 0}]
    first_shortfall_age = None
    total_real_shortfall = 0
    for age in range(int(plan["current_age"]), END_AGE):
        elapsed = age - plan["current_age"]
        factor = (1 + inflation) ** elapsed
        end_factor = factor * (1 + inflation)
        retired = age >= plan["retirement_age"]
        income = 0 if retired else plan["annual_income"] * factor
        contribution = income * plan["tax_advantaged_rate"] / 100
        spending = annual_spending * factor
        # Apply the elected rate to this year's opening retirement balance.
        # A severe loss cannot force a withdrawal above the available balance.
        available_retirement = retirement_balance * (1 + growth)
        withdrawal = min(available_retirement, retirement_balance * plan["withdrawal_rate"] / 100) if retired else 0
        retirement_balance = available_retirement + contribution - withdrawal
        taxable_cash_flow = income - contribution + withdrawal - spending
        taxable_closing = taxable_balance * (1 + growth) + taxable_cash_flow
        shortfall = max(0, -taxable_closing)
        if shortfall > 0 and first_shortfall_age is None:
            first_shortfall_age = age
        total_real_shortfall += shortfall / end_factor
        taxable_balance = max(0, taxable_closing)
        rows.append({"age": age + 1, "factor": end_factor,
                     **{key: round(value, 2) for key, value in {
                         "assets": retirement_balance + taxable_balance,
                         "retirement_assets": retirement_balance, "taxable_assets": taxable_balance,
                         "income": income, "tax_advantaged_savings": contribution,
                         "taxable_cash_flow": taxable_cash_flow, "withdrawal": withdrawal,
                         "spending": spending, "shortfall": shortfall}.items()}})
    retirement = next(row for row in rows if row["age"] == plan["retirement_age"])
    return {"rows": rows, "retirement": retirement, "final": rows[-1],
            "annual_spending": annual_spending,
            "annual_tax_advantaged_savings": round(annual_tax_advantaged, 2),
            "annual_taxable_savings": round(annual_taxable, 2),
            "first_shortfall_age": first_shortfall_age,
            "total_real_shortfall": round(total_real_shortfall, 2)}
