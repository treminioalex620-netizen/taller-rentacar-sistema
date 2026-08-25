from __future__ import annotations

import csv
import os
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, Response, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "taller_rentacar.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
EXPORT_DIR = DATA_DIR / "registros"
USING_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cambie-esta-clave-antes-de-produccion")


class Database:
    """Small adapter that keeps the application portable between SQLite and PostgreSQL."""
    def __init__(self):
        if USING_POSTGRES:
            from psycopg import connect
            from psycopg.rows import dict_row
            self.connection = connect(DATABASE_URL, row_factory=dict_row)
        else:
            self.connection = sqlite3.connect(DB_PATH)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA busy_timeout=5000")

    def execute(self, query, params=()):
        if USING_POSTGRES:
            query = query.replace("?", "%s")
        return self.connection.execute(query, params)

    def commit(self):
        self.connection.commit()

    def close(self):
        self.connection.close()


def db():
    if "db" not in g:
        g.db = Database()
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    connection = g.pop("db", None)
    if connection:
        connection.close()


SQLITE_SCHEMA = """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS incomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL CHECK(category IN ('renta', 'taller')),
            description TEXT NOT NULL,
            amount REAL NOT NULL CHECK(amount > 0),
            observations TEXT,
            income_date TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(created_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            amount REAL NOT NULL CHECK(amount > 0),
            paid_date TEXT NOT NULL,
            notes TEXT,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(created_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            amount REAL NOT NULL CHECK(amount > 0),
            observations TEXT,
            expense_date TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(created_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS day_closures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            closure_date TEXT NOT NULL UNIQUE,
            closed_by INTEGER NOT NULL,
            closed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(closed_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            daily_rate REAL NOT NULL CHECK(daily_rate > 0),
            weekly_workdays INTEGER NOT NULL DEFAULT 6 CHECK(weekly_workdays BETWEEN 1 AND 7),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER NOT NULL,
            module TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(actor_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS payroll_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER,
            employee_name TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            daily_rate REAL NOT NULL DEFAULT 0,
            days_worked INTEGER NOT NULL DEFAULT 0,
            days_absent INTEGER NOT NULL DEFAULT 0,
            base_salary REAL NOT NULL DEFAULT 0,
            commission_eligible INTEGER NOT NULL DEFAULT 0,
            commission_amount REAL NOT NULL DEFAULT 0,
            amount REAL NOT NULL CHECK(amount > 0),
            concept TEXT NOT NULL,
            notes TEXT,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(created_by) REFERENCES users(id),
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        );
"""

POSTGRES_SCHEMA = (SQLITE_SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    .replace("REAL", "NUMERIC(12,2)")
    .replace("income_date TEXT", "income_date DATE")
    .replace("expense_date TEXT", "expense_date DATE")
    .replace("paid_date TEXT", "paid_date DATE")
    .replace("closure_date TEXT", "closure_date DATE")
    .replace("period_start TEXT", "period_start DATE")
    .replace("period_end TEXT", "period_end DATE")
    .replace("created_by INTEGER", "created_by BIGINT")
    .replace("closed_by INTEGER", "closed_by BIGINT")
    .replace("actor_id INTEGER", "actor_id BIGINT")
    .replace("employee_id INTEGER", "employee_id BIGINT")
    .replace("created_at TEXT", "created_at TIMESTAMPTZ")
    .replace("closed_at TEXT", "closed_at TIMESTAMPTZ"))


def init_db():
    connection = Database()
    if USING_POSTGRES:
        for statement in POSTGRES_SCHEMA.split(";"):
            if statement.strip():
                connection.execute(statement)
    else:
        connection.connection.executescript(SQLITE_SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")} if not USING_POSTGRES else {"active"}
    if "active" not in columns:
        connection.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    if not USING_POSTGRES:
        payroll_columns = {row[1] for row in connection.execute("PRAGMA table_info(payroll_payments)")}
        for column, definition in (("employee_id", "INTEGER"), ("daily_rate", "REAL NOT NULL DEFAULT 0"), ("days_worked", "INTEGER NOT NULL DEFAULT 0"), ("days_absent", "INTEGER NOT NULL DEFAULT 0"), ("base_salary", "REAL NOT NULL DEFAULT 0"), ("commission_eligible", "INTEGER NOT NULL DEFAULT 0"), ("commission_amount", "REAL NOT NULL DEFAULT 0")):
            if column not in payroll_columns:
                connection.execute(f"ALTER TABLE payroll_payments ADD COLUMN {column} {definition}")
    exists = connection.execute("SELECT 1 FROM users WHERE username = 'admin'").fetchone()
    if not exists:
        connection.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
            ("admin", generate_password_hash("admin123")),
        )
    connection.commit()
    connection.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Inicie sesión para continuar.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("role") != "admin":
            flash("Esta sección está disponible únicamente para administradores.", "danger")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


def parse_date(value: str, fallback: date | None = None) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return fallback or date.today()


def selected_range():
    end = parse_date(request.values.get("end"), date.today())
    start = parse_date(request.values.get("start"), end - timedelta(days=14))
    if start > end:
        start, end = end, start
    return start.isoformat(), end.isoformat()


def commission_for_period(start: str, end: str) -> float:
    connection = db()
    workshop = connection.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM incomes WHERE category = 'taller' AND income_date BETWEEN ? AND ?", (start, end)).fetchone()["total"]
    invoices_total = connection.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM invoices WHERE paid_date BETWEEN ? AND ?", (start, end)).fetchone()["total"]
    return float(workshop + invoices_total) * 0.01


def is_closed(entry_date: str) -> bool:
    return bool(db().execute("SELECT 1 FROM day_closures WHERE closure_date = ?", (entry_date,)).fetchone())


def can_manage_income(record) -> bool:
    return session.get("role") == "admin" or record["created_by"] == session.get("user_id")


def audit(module: str, action: str, detail: str):
    """Registers the responsible logged-in user for each business action."""
    actor_id = session.get("user_id")
    if actor_id:
        db().execute("INSERT INTO audit_logs (actor_id, module, action, detail) VALUES (?, ?, ?, ?)", (actor_id, module, action, detail))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        user = db().execute("SELECT * FROM users WHERE username = ?", (request.form.get("username", "").strip(),)).fetchone()
        if user and user["active"] and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session.update(user_id=user["id"], username=user["username"], role=user["role"])
            return redirect(url_for("dashboard"))
        flash("Usuario o contraseña incorrectos.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    today = date.today().isoformat()
    day_is_closed = is_closed(today)
    totals = db().execute("""
        SELECT category, COALESCE(SUM(amount), 0) total FROM incomes
        WHERE income_date = ? GROUP BY category
    """, (today,)).fetchall()
    data = {row["category"]: row["total"] for row in totals}
    expenses_total = db().execute("SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE expense_date = ?", (today,)).fetchone()["total"]
    recent = [dict(row, entry_date=row["income_date"], entry_type="ingreso") for row in db().execute("SELECT * FROM incomes WHERE income_date = ? ORDER BY id DESC", (today,)).fetchall()]
    recent += [dict(row, entry_date=row["expense_date"], entry_type="gasto", category="gasto") for row in db().execute("SELECT * FROM expenses WHERE expense_date = ? ORDER BY id DESC", (today,)).fetchall()]
    recent.sort(key=lambda row: (str(row["entry_date"]), row["id"]), reverse=True)
    if day_is_closed:
        data, expenses_total, recent = {}, 0, []
    return render_template("dashboard.html", rental_total=data.get("renta", 0), workshop_total=data.get("taller", 0), expenses_total=expenses_total, net_total=data.get("renta", 0) + data.get("taller", 0) - expenses_total, recent=recent[:8], today=today, day_is_closed=day_is_closed)


@app.route("/ingresos/nuevo", methods=["GET", "POST"])
@login_required
def new_income():
    if request.method == "POST":
        category = request.form.get("category")
        description = request.form.get("description", "").strip()
        observations = request.form.get("observations", "").strip()
        income_date = parse_date(request.form.get("income_date")).isoformat()
        try:
            amount = float(request.form.get("amount", 0))
            assert category in ("renta", "taller") and description and amount > 0
        except (ValueError, AssertionError):
            flash("Complete categoría, descripción y un monto mayor que cero.", "danger")
            return render_template("income_form.html", record=None)
        if is_closed(income_date):
            flash("No puede agregar movimientos: ese día ya fue cerrado.", "danger")
            return render_template("income_form.html", record=None)
        db().execute("INSERT INTO incomes (category, description, amount, observations, income_date, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                     (category, description, amount, observations, income_date, session["user_id"]))
        audit("Ingresos", "Registró ingreso", f"{category.title()}: {description} — ${amount:.2f} ({income_date})")
        db().commit()
        flash("Ingreso guardado correctamente.", "success")
        return redirect(url_for("dashboard"))
    return render_template("income_form.html", record=None)


@app.route("/ingresos/<int:income_id>/editar", methods=["GET", "POST"])
@login_required
def edit_income(income_id):
    record = db().execute("SELECT * FROM incomes WHERE id = ?", (income_id,)).fetchone()
    if not record:
        flash("No se encontró el ingreso solicitado.", "warning")
        return redirect(url_for("reports"))
    if not can_manage_income(record):
        flash("No tiene permiso para editar este registro.", "danger")
        return redirect(url_for("reports"))
    if is_closed(record["income_date"]):
        flash("No se puede modificar un registro de un día cerrado.", "danger")
        return redirect(url_for("reports"))
    if request.method == "POST":
        category, description = request.form.get("category"), request.form.get("description", "").strip()
        new_date = parse_date(request.form.get("income_date")).isoformat()
        try:
            amount = float(request.form.get("amount", 0))
            assert category in ("renta", "taller") and description and amount > 0
        except (ValueError, AssertionError):
            flash("Complete categoría, descripción y un monto mayor que cero.", "danger")
            return render_template("income_form.html", record=record)
        if is_closed(new_date):
            flash("No puede mover un registro a un día ya cerrado.", "danger")
            return render_template("income_form.html", record=record)
        db().execute("UPDATE incomes SET category=?, description=?, amount=?, observations=?, income_date=? WHERE id=?", (category, description, amount, request.form.get("observations", "").strip(), new_date, income_id))
        audit("Ingresos", "Modificó ingreso", f"Registro #{income_id}: {category.title()} — {description} — ${amount:.2f} ({new_date})")
        db().commit(); flash("Ingreso actualizado correctamente.", "success")
        return redirect(url_for("reports"))
    return render_template("income_form.html", record=record)


@app.route("/ingresos/<int:income_id>/eliminar", methods=["POST"])
@login_required
def delete_income(income_id):
    record = db().execute("SELECT * FROM incomes WHERE id = ?", (income_id,)).fetchone()
    if not record or not can_manage_income(record):
        flash("No tiene permiso para eliminar ese registro.", "danger")
    elif is_closed(record["income_date"]):
        flash("No se puede eliminar un registro de un día cerrado.", "danger")
    else:
        audit("Ingresos", "Eliminó ingreso", f"Registro #{income_id}: {record['category'].title()} — {record['description']} — ${record['amount']:.2f} ({record['income_date']})")
        db().execute("DELETE FROM incomes WHERE id = ?", (income_id,)); db().commit(); flash("Ingreso eliminado.", "success")
    return redirect(url_for("reports"))


@app.route("/ingresos/<int:income_id>/recibo")
@login_required
def receipt(income_id):
    record = db().execute("SELECT incomes.*, users.username FROM incomes JOIN users ON users.id = incomes.created_by WHERE incomes.id = ?", (income_id,)).fetchone()
    if not record:
        flash("No se encontró el recibo.", "warning")
        return redirect(url_for("reports"))
    return render_template("receipt.html", record=record)


@app.route("/gastos/nuevo", methods=["GET", "POST"])
@login_required
def new_expense():
    if request.method == "POST":
        expense_date = parse_date(request.form.get("expense_date")).isoformat()
        description = request.form.get("description", "").strip()
        try:
            amount = float(request.form.get("amount", 0)); assert description and amount > 0
        except (ValueError, AssertionError):
            flash("Complete la descripción y un monto mayor que cero.", "danger")
            return render_template("expense_form.html", record=None)
        if is_closed(expense_date):
            flash("No puede agregar gastos: ese día ya fue cerrado.", "danger")
            return render_template("expense_form.html", record=None)
        db().execute("INSERT INTO expenses (description, amount, observations, expense_date, created_by) VALUES (?, ?, ?, ?, ?)", (description, amount, request.form.get("observations", "").strip(), expense_date, session["user_id"]))
        audit("Gastos", "Registró gasto", f"{description} — ${amount:.2f} ({expense_date})")
        db().commit()
        flash("Gasto registrado correctamente.", "success")
        return redirect(url_for("dashboard"))
    return render_template("expense_form.html", record=None)


@app.route("/gastos/<int:expense_id>/editar", methods=["GET", "POST"])
@login_required
def edit_expense(expense_id):
    record = db().execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if not record or not can_manage_income(record):
        flash("No tiene permiso para editar este gasto.", "danger"); return redirect(url_for("reports"))
    if is_closed(record["expense_date"]):
        flash("No se puede modificar un gasto de un día cerrado.", "danger"); return redirect(url_for("reports"))
    if request.method == "POST":
        expense_date, description = parse_date(request.form.get("expense_date")).isoformat(), request.form.get("description", "").strip()
        try:
            amount = float(request.form.get("amount", 0)); assert description and amount > 0
        except (ValueError, AssertionError):
            flash("Complete la descripción y un monto mayor que cero.", "danger"); return render_template("expense_form.html", record=record)
        if is_closed(expense_date):
            flash("No puede mover un gasto a un día cerrado.", "danger"); return render_template("expense_form.html", record=record)
        db().execute("UPDATE expenses SET description=?, amount=?, observations=?, expense_date=? WHERE id=?", (description, amount, request.form.get("observations", "").strip(), expense_date, expense_id)); audit("Gastos", "Modificó gasto", f"Registro #{expense_id}: {description} — ${amount:.2f} ({expense_date})"); db().commit()
        flash("Gasto actualizado correctamente.", "success"); return redirect(url_for("reports"))
    return render_template("expense_form.html", record=record)


@app.route("/gastos/<int:expense_id>/eliminar", methods=["POST"])
@login_required
def delete_expense(expense_id):
    record = db().execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if not record or not can_manage_income(record): flash("No tiene permiso para eliminar ese gasto.", "danger")
    elif is_closed(record["expense_date"]): flash("No se puede eliminar un gasto de un día cerrado.", "danger")
    else:
        audit("Gastos", "Eliminó gasto", f"Registro #{expense_id}: {record['description']} — ${record['amount']:.2f} ({record['expense_date']})")
        db().execute("DELETE FROM expenses WHERE id = ?", (expense_id,)); db().commit(); flash("Gasto eliminado.", "success")
    return redirect(url_for("reports"))


@app.route("/facturas", methods=["GET", "POST"])
@login_required
@admin_required
def invoices():
    if request.method == "POST":
        try:
            amount = float(request.form.get("amount", 0))
            description = request.form.get("description", "").strip()
            assert description and amount > 0
        except (ValueError, AssertionError):
            flash("Indique una descripción y monto válido.", "danger")
        else:
            db().execute("INSERT INTO invoices (description, amount, paid_date, notes, created_by) VALUES (?, ?, ?, ?, ?)",
                         (description, amount, parse_date(request.form.get("paid_date")).isoformat(), request.form.get("notes", "").strip(), session["user_id"]))
            audit("Facturas", "Registró factura pagada", f"{description} — ${amount:.2f} ({parse_date(request.form.get('paid_date')).isoformat()})")
            db().commit()
            flash("Factura pagada registrada. Cuenta para las comisiones.", "success")
            return redirect(url_for("invoices"))
    records = db().execute("SELECT * FROM invoices ORDER BY paid_date DESC, id DESC LIMIT 30").fetchall()
    return render_template("invoices.html", records=records)


@app.route("/comisiones")
@login_required
@admin_required
def commissions():
    start, end = selected_range()
    connection = db()
    workshop = connection.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM incomes WHERE category = 'taller' AND income_date BETWEEN ? AND ?", (start, end)).fetchone()["total"]
    invoices_total = connection.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM invoices WHERE paid_date BETWEEN ? AND ?", (start, end)).fetchone()["total"]
    return render_template("commissions.html", start=start, end=end, workshop=workshop, invoices=invoices_total, base=workshop + invoices_total, commission=(workshop + invoices_total) * .01)


@app.route("/reportes")
@login_required
def reports():
    start, end = selected_range()
    category = request.args.get("category", "todos")
    clauses, parameters = ["income_date BETWEEN ? AND ?"], [start, end]
    if category in ("renta", "taller"):
        clauses.append("category = ?")
        parameters.append(category)
    income_records = db().execute(f"SELECT * FROM incomes WHERE {' AND '.join(clauses)}", parameters).fetchall() if category != "gasto" else []
    expense_records = db().execute("SELECT * FROM expenses WHERE expense_date BETWEEN ? AND ?", (start, end)).fetchall() if category in ("todos", "gasto") else []
    records = [dict(row, entry_date=row["income_date"], entry_type="ingreso") for row in income_records]
    records += [dict(row, entry_date=row["expense_date"], entry_type="gasto", category="gasto") for row in expense_records]
    records.sort(key=lambda row: (str(row["entry_date"]), row["id"]), reverse=True)
    totals = {"renta": 0, "taller": 0, "gasto": 0}
    for row in records: totals[row["category"]] += row["amount"]
    return render_template("reports.html", records=records, start=start, end=end, category=category, totals=totals)


@app.route("/reportes/csv")
@login_required
def report_csv():
    start, end = selected_range()
    category = request.args.get("category", "todos")
    query, params = "SELECT * FROM incomes WHERE income_date BETWEEN ? AND ?", [start, end]
    if category in ("renta", "taller"):
        query += " AND category = ?"; params.append(category)
    income_rows = db().execute(query + " ORDER BY income_date, id", params).fetchall() if category != "gasto" else []
    expense_rows = db().execute("SELECT * FROM expenses WHERE expense_date BETWEEN ? AND ? ORDER BY expense_date, id", (start, end)).fetchall() if category in ("todos", "gasto") else []
    from io import StringIO
    output = StringIO(); writer = csv.writer(output)
    writer.writerow(["Fecha", "Tipo", "Área", "Descripción", "Monto", "Observaciones"])
    writer.writerows([[r["income_date"], "Ingreso", r["category"], r["description"], f"{r['amount']:.2f}", r["observations"]] for r in income_rows])
    writer.writerows([[r["expense_date"], "Gasto", "Gasto", r["description"], f"{r['amount']:.2f}", r["observations"]] for r in expense_rows])
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=reporte_{start}_{end}.csv"})


@app.route("/cerrar-dia", methods=["POST"])
@login_required
@admin_required
def close_day():
    closure_date = parse_date(request.form.get("closure_date")).isoformat()
    connection = db()
    if connection.execute("SELECT 1 FROM day_closures WHERE closure_date = ?", (closure_date,)).fetchone():
        flash("Ese día ya fue cerrado; no se generaron archivos duplicados.", "warning")
        return redirect(url_for("dashboard"))
    rows = connection.execute("SELECT * FROM incomes WHERE income_date = ? ORDER BY id", (closure_date,)).fetchall()
    expenses = connection.execute("SELECT * FROM expenses WHERE expense_date = ? ORDER BY id", (closure_date,)).fetchall()
    for category, folder in (("taller", "taller"), ("renta", "rentas")):
        path = EXPORT_DIR / folder; path.mkdir(parents=True, exist_ok=True)
        with (path / f"{closure_date}.csv").open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file); writer.writerow(["Fecha", "Descripción", "Monto", "Observaciones"])
            for r in rows:
                if r["category"] == category:
                    writer.writerow([r["income_date"], r["description"], f"{r['amount']:.2f}", r["observations"]])
    path = EXPORT_DIR / "gastos"; path.mkdir(parents=True, exist_ok=True)
    with (path / f"{closure_date}.csv").open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file); writer.writerow(["Fecha", "Descripción", "Monto", "Observaciones"])
        for expense in expenses:
            writer.writerow([expense["expense_date"], expense["description"], f"{expense['amount']:.2f}", expense["observations"]])
    connection.execute("INSERT INTO day_closures (closure_date, closed_by) VALUES (?, ?)", (closure_date, session["user_id"]))
    audit("Cierres", "Cerró día", f"Cierre de caja del {closure_date}. Archivos generados: taller, rentas y gastos.")
    connection.commit()
    flash("Día cerrado. Se crearon los archivos de taller, rentas y gastos.", "success")
    return redirect(url_for("dashboard"))


@app.route("/cierres")
@login_required
@admin_required
def closures():
    records = db().execute("SELECT day_closures.*, users.username FROM day_closures JOIN users ON users.id = day_closures.closed_by ORDER BY closure_date DESC").fetchall()
    return render_template("closures.html", records=records)


@app.route("/planilla", methods=["GET", "POST"])
@login_required
@admin_required
def payroll():
    if request.method == "POST":
        employee_id = request.form.get("employee_id", type=int)
        employee = db().execute("SELECT * FROM employees WHERE id = ? AND active = 1", (employee_id,)).fetchone()
        concept = request.form.get("concept", "Pago semanal").strip() or "Pago semanal"
        start, end = parse_date(request.form.get("period_start")).isoformat(), parse_date(request.form.get("period_end")).isoformat()
        try:
            days_worked, days_absent = int(request.form.get("days_worked", 0)), int(request.form.get("days_absent", 0))
            assert employee and days_worked >= 0 and days_absent >= 0 and days_worked + days_absent <= employee["weekly_workdays"] and start <= end
        except (TypeError, ValueError, AssertionError):
            flash("Seleccione un empleado e indique días válidos dentro de su semana laboral.", "danger")
        else:
            base_salary = float(employee["daily_rate"]) * days_worked
            receives_commission = 1 if request.form.get("commission_eligible") == "1" else 0
            commission_amount = commission_for_period(start, end) if receives_commission else 0
            amount = base_salary + commission_amount
            if amount <= 0:
                flash("El pago calculado es $0.00; indique días trabajados o marque comisión.", "warning")
                return redirect(url_for("payroll"))
            db().execute("INSERT INTO payroll_payments (employee_id, employee_name, period_start, period_end, daily_rate, days_worked, days_absent, base_salary, commission_eligible, commission_amount, amount, concept, notes, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (employee["id"], employee["name"], start, end, employee["daily_rate"], days_worked, days_absent, base_salary, receives_commission, commission_amount, amount, concept, request.form.get("notes", "").strip(), session["user_id"]))
            audit("Planilla", "Registró pago", f"{employee['name']}: base ${base_salary:.2f}, comisión ${commission_amount:.2f}, total ${amount:.2f} ({start} al {end})")
            db().commit()
            flash("Pago agregado a la planilla semanal.", "success")
            return redirect(url_for("payroll"))
    records = db().execute("SELECT * FROM payroll_payments ORDER BY period_end DESC, id DESC").fetchall()
    employees = db().execute("SELECT * FROM employees WHERE active = 1 ORDER BY name").fetchall()
    return render_template("payroll.html", records=records, employees=employees)


@app.route("/api/comision-periodo")
@login_required
@admin_required
def commission_period_api():
    start, end = selected_range()
    return jsonify(commission=round(commission_for_period(start, end), 2), start=start, end=end)


@app.route("/planilla/<int:payment_id>/eliminar", methods=["POST"])
@login_required
@admin_required
def delete_payroll(payment_id):
    record = db().execute("SELECT * FROM payroll_payments WHERE id = ?", (payment_id,)).fetchone()
    if record:
        audit("Planilla", "Eliminó pago", f"Pago #{payment_id} de {record['employee_name']} — ${record['amount']:.2f}")
        db().execute("DELETE FROM payroll_payments WHERE id = ?", (payment_id,)); db().commit(); flash("Pago eliminado de la planilla.", "success")
    return redirect(url_for("payroll"))


@app.route("/personal", methods=["GET", "POST"])
@login_required
@admin_required
def employees():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        try:
            daily_rate, weekly_workdays = float(request.form.get("daily_rate", 0)), int(request.form.get("weekly_workdays", 6))
            assert name and daily_rate > 0 and 1 <= weekly_workdays <= 7
        except (ValueError, AssertionError):
            flash("Complete nombre, pago diario y días laborables válidos.", "danger")
        else:
            try:
                db().execute("INSERT INTO employees (name, daily_rate, weekly_workdays) VALUES (?, ?, ?)", (name, daily_rate, weekly_workdays)); audit("Personal", "Agregó trabajador", f"{name}: ${daily_rate:.2f}/día, {weekly_workdays} días laborables"); db().commit(); flash("Trabajador agregado.", "success")
            except Exception:
                flash("Ya existe un trabajador con ese nombre.", "danger")
    records = db().execute("SELECT * FROM employees ORDER BY active DESC, name").fetchall()
    return render_template("employees.html", records=records)


@app.route("/personal/<int:employee_id>/estado", methods=["POST"])
@login_required
@admin_required
def toggle_employee(employee_id):
    employee = db().execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    db().execute("UPDATE employees SET active = CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id = ?", (employee_id,)); audit("Personal", "Cambió estado", f"{employee['name'] if employee else f'Trabajador #{employee_id}'}: {'desactivado' if employee and employee['active'] else 'activado'}"); db().commit()
    flash("Estado del trabajador actualizado.", "success")
    return redirect(url_for("employees"))


@app.route("/cambiar-contrasena", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current, new, confirm = request.form.get("current_password", ""), request.form.get("new_password", ""), request.form.get("confirm_password", "")
        user = db().execute("SELECT password_hash FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if not check_password_hash(user["password_hash"], current): flash("La contraseña actual no es correcta.", "danger")
        elif len(new) < 6 or new != confirm: flash("La nueva contraseña debe tener al menos 6 caracteres y coincidir.", "danger")
        else:
            db().execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new), session["user_id"])); audit("Seguridad", "Cambió contraseña", "Actualizó su contraseña."); db().commit(); flash("Contraseña actualizada.", "success")
            return redirect(url_for("dashboard"))
    return render_template("change_password.html")


@app.route("/usuarios", methods=["GET", "POST"])
@login_required
@admin_required
def users():
    if request.method == "POST":
        username, password, role = request.form.get("username", "").strip(), request.form.get("password", ""), request.form.get("role")
        if not username or len(password) < 6 or role not in ("admin", "user"):
            flash("Usuario, rol y contraseña de al menos 6 caracteres son obligatorios.", "danger")
        else:
            try:
                db().execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)", (username, generate_password_hash(password), role)); audit("Usuarios", "Creó usuario", f"Usuario {username} con rol {role}"); db().commit()
                flash("Usuario creado.", "success")
            except Exception:
                flash("Ese nombre de usuario ya existe.", "danger")
    return render_template("users.html", users=db().execute("SELECT id, username, role, active, created_at FROM users ORDER BY username").fetchall())


@app.route("/usuarios/<int:user_id>/estado", methods=["POST"])
@login_required
@admin_required
def toggle_user(user_id):
    if user_id == session["user_id"]:
        flash("No puede desactivar su propia cuenta mientras está conectado.", "warning")
    else:
        user = db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        db().execute("UPDATE users SET active = CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id = ?", (user_id,)); audit("Usuarios", "Cambió estado", f"Usuario {user['username'] if user else user_id}: {'desactivado' if user and user['active'] else 'activado'}"); db().commit()
        flash("Estado del usuario actualizado.", "success")
    return redirect(url_for("users"))


@app.route("/bitacora")
@login_required
@admin_required
def audit_log():
    module = request.args.get("module", "todos")
    params = []
    query = "SELECT audit_logs.*, users.username FROM audit_logs JOIN users ON users.id = audit_logs.actor_id"
    if module != "todos":
        query += " WHERE audit_logs.module = ?"
        params.append(module)
    query += " ORDER BY audit_logs.id DESC LIMIT 300"
    records = db().execute(query, params).fetchall()
    modules = db().execute("SELECT DISTINCT module FROM audit_logs ORDER BY module").fetchall()
    return render_template("audit_log.html", records=records, modules=modules, module=module)


init_db()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
