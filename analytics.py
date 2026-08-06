import calendar
from datetime import date, datetime, timedelta


SPEND_SQL = """
CASE
    WHEN a.type = 'credit' THEN t.amount
    WHEN t.amount > 0 THEN t.amount
    ELSE 0
END
"""

DEFAULT_OVERVIEW_LOOKBACK_DAYS = 30
MAX_OVERVIEW_LOOKBACK_DAYS = 365


def previous_month(month):
    value = datetime.strptime(month, "%Y-%m")
    year, number = value.year, value.month - 1
    if number == 0:
        year, number = year - 1, 12
    return f"{year:04d}-{number:02d}"


def month_label(month):
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def shift_month(month, offset):
    year, number = map(int, month.split("-"))
    absolute = year * 12 + number - 1 + offset
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def month_range(first, last):
    months = []
    current = first
    while current <= last:
        months.append(current)
        current = shift_month(current, 1)
    return months


def scope_filter(account_id=None, connection_id=None):
    conditions = []
    params = []
    if account_id:
        conditions.append("a.id = ?")
        params.append(account_id)
    if connection_id:
        conditions.append("a.connection_id = ?")
        params.append(connection_id)
    return (
        "".join(f" AND {condition}" for condition in conditions),
        params,
    )


def spending_summary(connection, month, account_id=None, connection_id=None):
    account_sql, account_params = scope_filter(account_id, connection_id)
    prior = previous_month(month)

    today = date.today()
    comparison_day = today.day if month == today.strftime("%Y-%m") else None

    def total_for(target_month, through_day=None):
        day_sql = " AND CAST(substr(t.transacted_at, 9, 2) AS INTEGER) <= ?" if through_day else ""
        row = connection.execute(
            f"""
            SELECT COALESCE(SUM({SPEND_SQL}), 0) AS total
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            WHERE t.pending = 0 AND t.excluded = 0
              AND substr(t.transacted_at, 1, 7) = ?
              {day_sql}
              {account_sql}
            """,
            [target_month, *([through_day] if through_day else []), *account_params],
        ).fetchone()
        return row["total"]

    total = total_for(month)
    prior_total = total_for(prior, comparison_day)
    change = (
        (total - prior_total) / prior_total * 100
        if prior_total > 0
        else None
    )

    month_days = calendar.monthrange(*map(int, month.split("-")))[1]
    elapsed = today.day if month == today.strftime("%Y-%m") else month_days
    daily_average = total / max(elapsed, 1)
    projected_total = (
        round(daily_average * month_days)
        if month == today.strftime("%Y-%m")
        else total
    )

    categories = connection.execute(
        f"""
        SELECT COALESCE(t.category_override, t.category) AS name,
               SUM({SPEND_SQL}) AS amount,
               COUNT(*) AS transaction_count
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.pending = 0 AND t.excluded = 0
          AND substr(t.transacted_at, 1, 7) = ?
          {account_sql}
        GROUP BY COALESCE(t.category_override, t.category)
        HAVING amount > 0
        ORDER BY amount DESC
        """,
        [month, *account_params],
    ).fetchall()

    merchants = connection.execute(
        f"""
        SELECT COALESCE(NULLIF(t.merchant, ''), t.description) AS name,
               SUM({SPEND_SQL}) AS amount,
               COUNT(*) AS transaction_count
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.pending = 0 AND t.excluded = 0
          AND substr(t.transacted_at, 1, 7) = ?
          {account_sql}
        GROUP BY COALESCE(NULLIF(t.merchant, ''), t.description)
        HAVING amount > 0
        ORDER BY amount DESC
        LIMIT 8
        """,
        [month, *account_params],
    ).fetchall()

    card_totals = connection.execute(
        f"""
        SELECT a.id, a.name, a.mask, c.owner_name, SUM({SPEND_SQL}) AS amount
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        JOIN connections c ON c.id = a.connection_id
        WHERE t.pending = 0 AND t.excluded = 0
          AND substr(t.transacted_at, 1, 7) = ?
          {account_sql}
        GROUP BY a.id, a.name, a.mask, c.owner_name
        HAVING amount > 0
        ORDER BY amount DESC
        """,
        [month, *account_params],
    ).fetchall()

    largest = connection.execute(
        f"""
        SELECT COALESCE(NULLIF(t.merchant, ''), t.description) AS name,
               {SPEND_SQL} AS amount,
               t.transacted_at AS date
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.pending = 0 AND t.excluded = 0
          AND substr(t.transacted_at, 1, 7) = ?
          AND {SPEND_SQL} > 0
          {account_sql}
        ORDER BY amount DESC
        LIMIT 1
        """,
        [month, *account_params],
    ).fetchone()

    budget_rows = connection.execute(
        "SELECT category, amount FROM budgets WHERE month = ?", (month,)
    ).fetchall()
    budgets = {row["category"]: row["amount"] for row in budget_rows}
    budget_total = sum(budgets.values())
    budget_spent = sum(
        row["amount"] for row in categories if row["name"] in budgets
    )

    insights = []
    if categories and total:
        top = categories[0]
        share = top["amount"] / total * 100
        insights.append(
            f"{top['name']} is your largest category at {share:.0f}% of spending."
        )
    if change is not None:
        direction = "higher" if change > 0 else "lower"
        comparison = (
            "at the same point last month"
            if comparison_day
            else f"than {month_label(prior)}"
        )
        insights.append(
            f"Spending is {abs(change):.0f}% {direction} {comparison}."
        )
    if largest:
        insights.append(
            f"Your largest purchase was {largest['name']} at ${largest['amount'] / 100:,.2f}."
        )

    return {
        "total": total,
        "prior_total": prior_total,
        "change": change,
        "comparison_label": (
            f"Through day {comparison_day} last month"
            if comparison_day
            else f"In {month_label(prior)}"
        ),
        "daily_average": daily_average,
        "projected_total": projected_total,
        "elapsed_days": elapsed,
        "month_days": month_days,
        "categories": [dict(row) for row in categories],
        "merchants": [dict(row) for row in merchants],
        "card_totals": [dict(row) for row in card_totals],
        "largest": dict(largest) if largest else None,
        "budgets": budgets,
        "budget_total": budget_total,
        "budget_spent": budget_spent,
        "insights": insights,
    }


def rolling_spending_summary(
    connection,
    lookback_days=DEFAULT_OVERVIEW_LOOKBACK_DAYS,
    account_id=None,
    connection_id=None,
    today=None,
):
    today = today or date.today()
    current_start = today - timedelta(days=lookback_days - 1)
    prior_end = current_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=lookback_days - 1)
    account_sql, account_params = scope_filter(account_id, connection_id)

    rows = [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT t.transacted_at AS date,
                   COALESCE(t.category_override, t.category) AS category,
                   COALESCE(NULLIF(t.merchant, ''), t.description) AS merchant,
                   t.description,
                   a.id AS account_id,
                   a.name AS account_name,
                   a.mask,
                   c.owner_name,
                   {SPEND_SQL} AS amount
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            JOIN connections c ON c.id = a.connection_id
            WHERE t.pending = 0 AND t.excluded = 0
              AND t.transacted_at BETWEEN ? AND ?
              {account_sql}
            ORDER BY t.transacted_at
            """,
            [prior_start.isoformat(), today.isoformat(), *account_params],
        ).fetchall()
    ]
    current_rows = [row for row in rows if row["date"] >= current_start.isoformat()]
    prior_rows = [row for row in rows if row["date"] <= prior_end.isoformat()]
    total = sum(row["amount"] for row in current_rows)
    prior_total = sum(row["amount"] for row in prior_rows)
    change = ((total - prior_total) / prior_total * 100) if prior_total else None

    category_groups = {}
    merchant_groups = {}
    card_groups = {}
    for row in current_rows:
        category = category_groups.setdefault(
            row["category"],
            {"name": row["category"], "amount": 0, "transaction_count": 0},
        )
        category["amount"] += row["amount"]
        category["transaction_count"] += 1

        merchant = merchant_groups.setdefault(
            row["merchant"],
            {"name": row["merchant"], "amount": 0, "transaction_count": 0},
        )
        merchant["amount"] += row["amount"]
        merchant["transaction_count"] += 1

        card_key = (
            row["account_id"],
            row["account_name"],
            row["mask"],
            row["owner_name"],
        )
        card = card_groups.setdefault(
            card_key,
            {
                "id": row["account_id"],
                "name": row["account_name"],
                "mask": row["mask"],
                "owner_name": row["owner_name"],
                "amount": 0,
            },
        )
        card["amount"] += row["amount"]

    categories = sorted(
        (row for row in category_groups.values() if row["amount"] > 0),
        key=lambda row: row["amount"],
        reverse=True,
    )
    merchants = sorted(
        (row for row in merchant_groups.values() if row["amount"] > 0),
        key=lambda row: row["amount"],
        reverse=True,
    )[:8]
    card_totals = sorted(
        (row for row in card_groups.values() if row["amount"] > 0),
        key=lambda row: row["amount"],
        reverse=True,
    )
    positive_rows = [row for row in current_rows if row["amount"] > 0]
    largest_row = max(positive_rows, key=lambda row: row["amount"], default=None)
    largest = (
        {
            "name": largest_row["merchant"],
            "amount": largest_row["amount"],
            "date": largest_row["date"],
        }
        if largest_row
        else None
    )

    current_month = today.strftime("%Y-%m")
    current_month_total = connection.execute(
        f"""
        SELECT COALESCE(SUM({SPEND_SQL}), 0) AS total
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.pending = 0 AND t.excluded = 0
          AND substr(t.transacted_at, 1, 7) = ?
          AND t.transacted_at <= ?
          {account_sql}
        """,
        [current_month, today.isoformat(), *account_params],
    ).fetchone()["total"]
    current_month_days = calendar.monthrange(today.year, today.month)[1]
    projected_total = round(current_month_total / today.day * current_month_days)

    insights = []
    if categories and total > 0:
        top = categories[0]
        insights.append(
            f"{top['name']} is your largest category at {top['amount'] / total * 100:.0f}% of spending."
        )
    if change is not None:
        if change == 0:
            insights.append("Spending matches the preceding lookback window.")
        else:
            direction = "higher" if change > 0 else "lower"
            insights.append(
                f"Spending is {abs(change):.0f}% {direction} than the preceding {lookback_days} days."
            )
    if largest:
        insights.append(
            f"Your largest purchase was {largest['name']} at ${largest['amount'] / 100:,.2f}."
        )

    return {
        "lookback_days": lookback_days,
        "date_from": current_start.isoformat(),
        "date_to": today.isoformat(),
        "prior_date_from": prior_start.isoformat(),
        "prior_date_to": prior_end.isoformat(),
        "total": total,
        "prior_total": prior_total,
        "change": change,
        "daily_average": total / lookback_days,
        "projected_total": projected_total,
        "current_month_total": current_month_total,
        "current_month_label": month_label(current_month),
        "current_month_elapsed_days": today.day,
        "categories": categories,
        "merchants": merchants,
        "card_totals": card_totals,
        "largest": largest,
        "insights": insights,
    }


def effective_cash_flow_type(row):
    if row["flow_override"]:
        return row["flow_override"]
    description = f"{row['merchant'] or ''} {row['description']}".lower()
    if "venmo" in description:
        return "other_inflow" if row["amount"] < 0 else "spending"
    if row["category_flow_type"]:
        return row["category_flow_type"]
    category = row["category"].lower()
    if category.startswith("transfer"):
        return "transfer"
    if row["account_type"] == "credit" and category == "loan payments":
        return "transfer"
    if row["amount"] < 0:
        return "earned_income" if category.startswith("income") else "other_inflow"
    return "spending"


def cash_flow_summary(
    connection,
    lookback_days=DEFAULT_OVERVIEW_LOOKBACK_DAYS,
    account_id=None,
    connection_id=None,
    today=None,
):
    today = today or date.today()
    current_start = today - timedelta(days=lookback_days - 1)
    prior_end = current_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=lookback_days - 1)
    account_sql, account_params = scope_filter(account_id, connection_id)
    rows = [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT t.id, t.transacted_at AS date, t.amount, t.description,
                   t.merchant, t.excluded, t.flow_override,
                   COALESCE(t.category_override, t.category) AS category,
                   r.flow_type AS category_flow_type,
                   a.type AS account_type, a.name AS account_name, a.mask,
                   c.owner_name, c.institution
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            JOIN connections c ON c.id = a.connection_id
            LEFT JOIN category_rules r
              ON r.name = COALESCE(t.category_override, t.category) COLLATE NOCASE
            WHERE t.pending = 0 AND t.transacted_at <= ? {account_sql}
            ORDER BY t.transacted_at DESC, ABS(t.amount) DESC
            """,
            [today.isoformat(), *account_params],
        ).fetchall()
    ]

    for row in rows:
        row["flow_type"] = (
            "excluded" if row["excluded"] else effective_cash_flow_type(row)
        )
        row["display_name"] = row["merchant"] or row["description"]
        row["display_amount"] = abs(row["amount"])

    def totals(selected):
        result = {
            "income": 0,
            "other_inflows": 0,
            "spending": 0,
            "transfers_in": 0,
            "transfers_out": 0,
        }
        for row in selected:
            if row["flow_type"] == "earned_income" and row["amount"] < 0:
                result["income"] += -row["amount"]
            elif row["flow_type"] == "other_inflow" and row["amount"] < 0:
                result["other_inflows"] += -row["amount"]
            elif row["flow_type"] == "transfer":
                key = "transfers_in" if row["amount"] < 0 else "transfers_out"
                result[key] += abs(row["amount"])
            elif row["flow_type"] == "spending" and row["amount"] > 0:
                result["spending"] += row["amount"]
        result["total_inflows"] = result["income"] + result["other_inflows"]
        result["net"] = result["total_inflows"] - result["spending"]
        result["savings_rate"] = (
            result["net"] / result["income"] * 100 if result["income"] else None
        )
        return result

    current = [row for row in rows if row["date"] >= current_start.isoformat()]
    prior = [
        row
        for row in rows
        if prior_start.isoformat() <= row["date"] <= prior_end.isoformat()
    ]
    current_totals = totals(current)
    prior_totals = totals(prior)

    monthly = {}
    for row in rows:
        month = row["date"][:7]
        monthly.setdefault(month, []).append(row)
    months = [
        {"month": month, **totals(monthly[month])}
        for month in sorted(monthly)[-24:]
    ]
    activity = [
        row
        for row in current
        if row["flow_type"] in {"earned_income", "other_inflow", "transfer"}
    ][:30]
    return {
        **current_totals,
        "prior": prior_totals,
        "date_from": current_start.isoformat(),
        "date_to": today.isoformat(),
        "prior_date_from": prior_start.isoformat(),
        "prior_date_to": prior_end.isoformat(),
        "months": months,
        "activity": activity,
    }


def daily_trends(connection, account_id=None, connection_id=None):
    account_sql, account_params = scope_filter(account_id, connection_id)
    rows = connection.execute(
        f"""
        SELECT t.transacted_at AS date,
               a.id AS account_id,
               c.owner_name || ' · ' || a.name || ' ····' || COALESCE(a.mask, '') AS account_name,
               SUM({SPEND_SQL}) AS amount
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        JOIN connections c ON c.id = a.connection_id
        WHERE t.pending = 0 AND t.excluded = 0
          {account_sql}
        GROUP BY t.transacted_at, a.id, a.name, a.mask, c.owner_name
        HAVING amount != 0
        ORDER BY t.transacted_at
        """,
        account_params,
    ).fetchall()
    return [dict(row) for row in rows]


def long_term_trends(connection, account_id=None, connection_id=None):
    scope_sql, scope_params = scope_filter(account_id, connection_id)
    monthly_rows = connection.execute(
        f"""
        SELECT substr(t.transacted_at, 1, 7) AS month,
               SUM({SPEND_SQL}) AS amount
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.pending = 0 AND t.excluded = 0
          {scope_sql}
        GROUP BY substr(t.transacted_at, 1, 7)
        ORDER BY month
        """,
        scope_params,
    ).fetchall()
    coverage = connection.execute(
        f"""
        SELECT MIN(t.transacted_at) AS first_date,
               MAX(t.transacted_at) AS last_date
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.pending = 0 AND t.excluded = 0
          {scope_sql}
        """,
        scope_params,
    ).fetchone()
    if not monthly_rows or not coverage["first_date"]:
        return {
            "months": [],
            "metrics": {},
            "category_series": [],
            "coverage_label": "No spending history",
        }

    first_date = datetime.strptime(coverage["first_date"], "%Y-%m-%d").date()
    last_date = datetime.strptime(coverage["last_date"], "%Y-%m-%d").date()
    months = month_range(monthly_rows[0]["month"], monthly_rows[-1]["month"])
    amounts = {row["month"]: row["amount"] for row in monthly_rows}
    today = date.today()
    current_month = today.strftime("%Y-%m")

    points = []
    for month in months:
        amount = amounts.get(month, 0)
        year, number = map(int, month.split("-"))
        days_in_month = calendar.monthrange(year, number)[1]
        projected = month == current_month and today.day < days_in_month
        display_amount = (
            round(amount / max(today.day, 1) * days_in_month)
            if projected
            else amount
        )
        incomplete_start = (
            month == first_date.strftime("%Y-%m") and first_date.day > 1
        )
        points.append(
            {
                "month": month,
                "label": month_label(month),
                "amount": amount,
                "display_amount": display_amount,
                "projected": projected,
                "partial_start": incomplete_start,
                "usable": not incomplete_start,
            }
        )

    point_by_month = {point["month"]: point for point in points}
    for index, point in enumerate(points):
        for window in (3, 6, 12, 24):
            sample = points[index - window + 1 : index + 1]
            point[f"ma_{window}"] = (
                round(sum(row["display_amount"] for row in sample) / window)
                if len(sample) == window and all(row["usable"] for row in sample)
                else None
            )
        prior = point_by_month.get(shift_month(point["month"], -12))
        if prior and prior["usable"] and prior["display_amount"]:
            point["yoy_amount"] = point["display_amount"] - prior["display_amount"]
            point["yoy_pct"] = (
                point["yoy_amount"] / prior["display_amount"] * 100
            )
        else:
            point["yoy_amount"] = None
            point["yoy_pct"] = None

    usable_points = [point for point in points if point["usable"]]
    latest = usable_points[-1]
    recent = usable_points[-3:]
    preceding = usable_points[-6:-3]
    recent_average = (
        round(sum(point["display_amount"] for point in recent) / len(recent))
        if recent
        else 0
    )
    preceding_average = (
        round(sum(point["display_amount"] for point in preceding) / len(preceding))
        if preceding
        else 0
    )
    momentum = (
        (recent_average - preceding_average) / preceding_average * 100
        if preceding_average
        else None
    )

    category_rows = connection.execute(
        f"""
        SELECT substr(t.transacted_at, 1, 7) AS month,
               COALESCE(t.category_override, t.category) AS category,
               SUM({SPEND_SQL}) AS amount
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.pending = 0 AND t.excluded = 0
          {scope_sql}
        GROUP BY substr(t.transacted_at, 1, 7),
                 COALESCE(t.category_override, t.category)
        HAVING amount > 0
        ORDER BY month, amount DESC
        """,
        scope_params,
    ).fetchall()
    category_amounts = {}
    for row in category_rows:
        category_amounts.setdefault(row["category"], {})[row["month"]] = row["amount"]

    category_series = []
    projection_factor = (
        latest["display_amount"] / latest["amount"]
        if latest["projected"] and latest["amount"]
        else 1
    )
    for category, values_by_month in category_amounts.items():
        values = []
        for point in points:
            value = values_by_month.get(point["month"], 0)
            if point["projected"]:
                value = round(value * projection_factor)
            values.append(value)
        latest_amount = values[-1]
        prior_amount = values[-13] if len(values) >= 13 else 0
        trailing_values = values[-12:]
        three_month_values = values[-3:]
        category_series.append(
            {
                "name": category,
                "values": values,
                "latest_amount": latest_amount,
                "three_month_average": round(
                    sum(three_month_values) / len(three_month_values)
                ),
                "trailing_12": sum(trailing_values),
                "yoy_pct": (
                    (latest_amount - prior_amount) / prior_amount * 100
                    if prior_amount
                    else None
                ),
            }
        )
    category_series.sort(key=lambda row: row["trailing_12"], reverse=True)

    return {
        "months": points,
        "metrics": {
            "latest_amount": latest["display_amount"],
            "latest_projected": latest["projected"],
            "latest_label": latest["label"],
            "yoy_pct": latest["yoy_pct"],
            "yoy_amount": latest["yoy_amount"],
            "three_month_average": recent_average,
            "three_month_momentum": momentum,
            "twelve_month_average": latest["ma_12"],
            "twenty_four_month_average": latest["ma_24"],
        },
        "category_series": category_series,
        "coverage_label": (
            f"{month_label(usable_points[0]['month'])}–{month_label(usable_points[-1]['month'])}"
        ),
    }


def category_details(connection, month, account_id=None, connection_id=None):
    summary = spending_summary(connection, month, account_id, connection_id)
    account_sql, account_params = scope_filter(account_id, connection_id)
    details = []
    for category in summary["categories"]:
        merchants = connection.execute(
            f"""
            SELECT COALESCE(NULLIF(t.merchant, ''), t.description) AS name,
                   SUM({SPEND_SQL}) AS amount,
                   COUNT(*) AS transaction_count
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            WHERE t.pending = 0 AND t.excluded = 0
              AND substr(t.transacted_at, 1, 7) = ?
              AND COALESCE(t.category_override, t.category) = ?
              {account_sql}
            GROUP BY COALESCE(NULLIF(t.merchant, ''), t.description)
            HAVING amount > 0
            ORDER BY amount DESC
            LIMIT 5
            """,
            [month, category["name"], *account_params],
        ).fetchall()
        category["merchants"] = [dict(row) for row in merchants]
        category["budget"] = summary["budgets"].get(category["name"], 0)
        details.append(category)
    return details


def transaction_list(
    connection,
    month=None,
    account_id=None,
    connection_id=None,
    category=None,
    query=None,
    include_excluded=False,
    excluded_only=False,
    date_from=None,
    date_to=None,
    limit=None,
):
    conditions = []
    params = []
    if month:
        conditions.append("substr(t.transacted_at, 1, 7) = ?")
        params.append(month)
    if date_from:
        conditions.append("t.transacted_at >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("t.transacted_at <= ?")
        params.append(date_to)
    if account_id:
        conditions.append("a.id = ?")
        params.append(account_id)
    if connection_id:
        conditions.append("a.connection_id = ?")
        params.append(connection_id)
    if category:
        conditions.append("COALESCE(t.category_override, t.category) = ?")
        params.append(category)
    if query:
        conditions.append(
            "(LOWER(t.description) LIKE ? OR LOWER(COALESCE(t.merchant, '')) LIKE ?)"
        )
        term = f"%{query.lower()}%"
        params.extend([term, term])
    if excluded_only:
        conditions.append("t.excluded = 1")
    elif not include_excluded:
        conditions.append("t.excluded = 0")
    where_sql = " AND ".join(conditions) if conditions else "1 = 1"
    limit_sql = "LIMIT ?" if limit else ""
    if limit:
        params.append(limit)

    rows = connection.execute(
        f"""
        SELECT t.*, a.name AS account_name, a.mask, a.type AS account_type,
               c.owner_name,
               COALESCE(t.category_override, t.category) AS effective_category,
               r.flow_type AS category_flow_type
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        JOIN connections c ON c.id = a.connection_id
        LEFT JOIN category_rules r
          ON r.name = COALESCE(t.category_override, t.category) COLLATE NOCASE
        WHERE {where_sql}
        ORDER BY t.transacted_at DESC, ABS(t.amount) DESC
        {limit_sql}
        """,
        params,
    ).fetchall()
    results = [dict(row) for row in rows]
    for row in results:
        row["flow_type"] = effective_cash_flow_type(
            {**row, "category": row["effective_category"]}
        )
    return results
