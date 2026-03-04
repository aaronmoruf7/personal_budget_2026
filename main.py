import calendar
from collections import Counter
from datetime import date

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import Budget, HiddenCategory, Transaction, get_db, init_db
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

    # Categories the user has hidden
    hidden = {h.name for h in db.query(HiddenCategory).all()}

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

    def _is_income(txn: Transaction) -> bool:
        return txn.is_income or txn.category.lower() == "income"

    for txn in monthly_txns:
        if _is_income(txn):
            monthly_income += txn.amount
        else:
            monthly_totals[txn.category] = monthly_totals.get(txn.category, 0) + txn.amount

    for txn in yearly_txns:
        if _is_income(txn):
            yearly_income += txn.amount
        else:
            yearly_totals[txn.category] = yearly_totals.get(txn.category, 0) + txn.amount

    # All expense categories (from transactions + budgets, minus Income and hidden)
    all_expense_cats = (
        set(monthly_totals)
        | set(yearly_totals)
        | set(monthly_budgets)
        | set(yearly_budgets)
    ) - {"Income"} - hidden

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

    # Last 5 by transaction date (most recent first)
    recent = (
        db.query(Transaction)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
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
    all_categories = sorted((txn_cats | budget_cats) - hidden - {"Income"})

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

    # Count occurrences per (date, name, amount) in this paste
    paste_counts: Counter = Counter(
        (t["date"], t["name"], t["amount"]) for t in parsed
    )
    # Build a lookup so we can grab the full txn dict by key
    txn_by_key = {(t["date"], t["name"], t["amount"]): t for t in parsed}

    for key, paste_count in paste_counts.items():
        txn_date, txn_name, txn_amount = key
        db_count = (
            db.query(Transaction)
            .filter(
                Transaction.date == txn_date,
                Transaction.name == txn_name,
                Transaction.amount == txn_amount,
            )
            .count()
        )
        to_add = max(0, paste_count - db_count)
        skipped += paste_count - to_add
        for _ in range(to_add):
            db.add(Transaction(**txn_by_key[key]))
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


@app.get("/transactions", response_class=HTMLResponse)
def transactions_page(
    request: Request,
    db: Session = Depends(get_db),
    category: str = "",
    year: str = "",
    month: str = "",
):
    # Convert to int, treating empty string as None
    year_int = int(year) if year.strip() else None
    month_int = int(month) if month.strip() else None

    query = db.query(Transaction)

    if category:
        query = query.filter(Transaction.category == category)

    if year_int:
        year_start = date(year_int, 1, 1)
        year_end = date(year_int, 12, 31)
        query = query.filter(Transaction.date >= year_start, Transaction.date <= year_end)

    if month_int and year_int:
        month_start = date(year_int, month_int, 1)
        month_end = date(year_int, month_int, calendar.monthrange(year_int, month_int)[1])
        query = query.filter(Transaction.date >= month_start, Transaction.date <= month_end)

    txns = query.order_by(Transaction.date.desc(), Transaction.id.desc()).all()

    all_categories = sorted({t.category for t in db.query(Transaction).all()})
    today = date.today()

    return templates.TemplateResponse(
        "transactions.html",
        {
            "request": request,
            "txns": txns,
            "all_categories": all_categories,
            "selected_category": category,
            "selected_year": year_int,
            "selected_month": month_int,
            "today": today,
            "months": MONTHS,
            "total": sum(t.amount for t in txns),
        },
    )


@app.post("/transactions/add")
async def add_transaction(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    raw_date = form.get("date", "").strip()        # YYYY-MM-DD from <input type="date">
    name = form.get("name", "").strip()
    category = form.get("category", "").strip()
    new_category = form.get("new_category", "").strip()
    if category == "__new__" and new_category:
        category = new_category
    amount = float(form.get("amount", 0))
    is_income = form.get("is_income") == "on"

    try:
        txn_date = date.fromisoformat(raw_date)
    except ValueError:
        return RedirectResponse(url="/transactions", status_code=303)

    db.add(Transaction(date=txn_date, name=name, category=category,
                       amount=amount, is_income=is_income))
    db.commit()
    return RedirectResponse(url="/transactions", status_code=303)


@app.post("/transactions/delete/{txn_id}")
def delete_transaction(txn_id: int, db: Session = Depends(get_db)):
    txn = db.query(Transaction).filter(Transaction.id == txn_id).first()
    if txn:
        db.delete(txn)
        db.commit()
    return RedirectResponse(url="/transactions", status_code=303)


@app.post("/categories/delete")
async def delete_category(request: Request, db: Session = Depends(get_db)):
    """Hide a category from progress bars and drop its budget entries."""
    form = await request.form()
    category = form.get("category", "").strip()
    if category:
        db.query(Budget).filter(Budget.category == category).delete()
        already = db.query(HiddenCategory).filter(HiddenCategory.name == category).first()
        if not already:
            db.add(HiddenCategory(name=category))
        db.commit()
    return RedirectResponse(url="/", status_code=303)
