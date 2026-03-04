import calendar
from datetime import date

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import Budget, Transaction, get_db, init_db
from parser import parse_transactions

app = FastAPI()
templates = Jinja2Templates(directory="templates")

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
MONTHS_SHORT = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def fmt_currency(value: float) -> str:
    return f"${value:,.2f}"


templates.env.filters["currency"] = fmt_currency


@app.on_event("startup")
def startup():
    init_db()


def _get_color(pct: float, has_budget: bool) -> str:
    if not has_budget:
        return "gray"
    if pct >= 100:
        return "red"
    if pct >= 75:
        return "orange"
    return "green"


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    year: int = None,
    month: int = None,
):
    today = date.today()
    if year is None:
        year = today.year
    if month is None:
        month = today.month

    # Date ranges
    month_start = date(year, month, 1)
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)

    monthly_txns = (
        db.query(Transaction)
        .filter(Transaction.date >= month_start, Transaction.date <= month_end)
        .all()
    )
    yearly_txns = (
        db.query(Transaction)
        .filter(Transaction.date >= year_start, Transaction.date <= year_end)
        .all()
    )

    # Budgets for selected year
    budgets = db.query(Budget).filter(Budget.year == year).all()
    monthly_budgets = {b.category: b.amount for b in budgets if b.month == month}
    yearly_budgets = {b.category: b.amount for b in budgets if b.month is None}

    # Tally totals (skip Income category — shown separately)
    monthly_totals: dict[str, float] = {}
    yearly_totals: dict[str, float] = {}
    monthly_income = 0.0
    yearly_income = 0.0

    for txn in monthly_txns:
        if txn.is_income:
            monthly_income += txn.amount
        else:
            monthly_totals[txn.category] = monthly_totals.get(txn.category, 0) + txn.amount

    for txn in yearly_txns:
        if txn.is_income:
            yearly_income += txn.amount
        else:
            yearly_totals[txn.category] = yearly_totals.get(txn.category, 0) + txn.amount

    # All expense categories (from transactions + budgets, minus Income)
    all_expense_cats = (
        set(monthly_totals)
        | set(yearly_totals)
        | set(monthly_budgets)
        | set(yearly_budgets)
    ) - {"Income"}

    categories = []
    for cat in sorted(all_expense_cats):
        m_spent = monthly_totals.get(cat, 0)
        y_spent = yearly_totals.get(cat, 0)
        m_budget = monthly_budgets.get(cat)
        y_budget = yearly_budgets.get(cat)
        m_pct = min(100, (m_spent / m_budget * 100) if m_budget else 0)
        y_pct = min(100, (y_spent / y_budget * 100) if y_budget else 0)

        categories.append(
            {
                "name": cat,
                "monthly_spent": m_spent,
                "yearly_spent": y_spent,
                "monthly_budget": m_budget,
                "yearly_budget": y_budget,
                "monthly_pct": round(m_pct, 1),
                "yearly_pct": round(y_pct, 1),
                "monthly_color": _get_color(m_pct, m_budget is not None),
                "yearly_color": _get_color(y_pct, y_budget is not None),
            }
        )

    # Last 5 ingested (by insert order)
    recent = (
        db.query(Transaction)
        .order_by(Transaction.created_at.desc())
        .limit(5)
        .all()
    )

    # All budgets for the settings table (newest first)
    all_budgets = (
        db.query(Budget)
        .order_by(Budget.year.desc(), Budget.month.desc())
        .all()
    )

    # All unique categories for the budget form dropdown
    txn_cats = {t.category for t in db.query(Transaction).all() if not t.is_income}
    budget_cats = {b.category for b in db.query(Budget).all()}
    all_categories = sorted(txn_cats | budget_cats)

    added = request.query_params.get("added")
    skipped = request.query_params.get("skipped")

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "categories": categories,
            "recent": recent,
            "all_budgets": all_budgets,
            "all_categories": all_categories,
            "monthly_income": monthly_income,
            "yearly_income": yearly_income,
            "year": year,
            "month": month,
            "today": today,
            "months": MONTHS,
            "months_short": MONTHS_SHORT,
            "added": added,
            "skipped": skipped,
        },
    )


@app.post("/ingest")
async def ingest(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    raw_text = form.get("raw_text", "")

    parsed = parse_transactions(raw_text)
    added = 0
    skipped = 0

    for txn in parsed:
        exists = (
            db.query(Transaction)
            .filter(
                Transaction.date == txn["date"],
                Transaction.name == txn["name"],
                Transaction.amount == txn["amount"],
            )
            .first()
        )
        if exists:
            skipped += 1
            continue

        db.add(Transaction(**txn))
        added += 1

    db.commit()
    return RedirectResponse(url=f"/?added={added}&skipped={skipped}", status_code=303)


@app.post("/budgets")
async def set_budget(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    category = form.get("category", "").strip()
    if category == "__new__":
        category = form.get("new_category", "").strip()
    if not category:
        return RedirectResponse(url="/", status_code=303)

    year = int(form.get("year", date.today().year))
    period_type = form.get("period_type", "monthly")
    month = int(form.get("month")) if period_type == "monthly" else None
    amount = float(form.get("amount", 0))

    existing = (
        db.query(Budget)
        .filter(
            Budget.category == category,
            Budget.year == year,
            Budget.month == month,
        )
        .first()
    )
    if existing:
        existing.amount = amount
    else:
        db.add(Budget(category=category, year=year, month=month, amount=amount))

    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/budgets/delete/{budget_id}")
def delete_budget(budget_id: int, db: Session = Depends(get_db)):
    budget = db.query(Budget).filter(Budget.id == budget_id).first()
    if budget:
        db.delete(budget)
        db.commit()
    return RedirectResponse(url="/", status_code=303)
