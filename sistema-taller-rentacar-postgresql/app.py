from __future__ import annotations

import csv
import io
import os
import sqlite3
import zipfile
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, Response, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "taller_rentacar.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
EXPORT_DIR = DATA_DIR / "registros"
INVOICE_DIR = DATA_DIR / "facturas_archivos"
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
            category TEXT NOT NULL CHECK(category IN ('renta', 'taller', 'lavado')),
            description TEXT NOT NULL,
            amount REAL NOT NULL CHECK(amount > 0),
            observations TEXT,
            income_date TEXT NOT NULL,
            rental_start TEXT,
            rental_end TEXT,
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
            folder_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pagada',
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(created_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            amount REAL NOT NULL CHECK(amount > 0),
            observations TEXT,
            category TEXT NOT NULL DEFAULT 'Otros',
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
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            document_id TEXT,
            address TEXT,
            notes TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate TEXT NOT NULL UNIQUE,
            brand TEXT NOT NULL,
            model TEXT NOT NULL,
            year INTEGER,
            color TEXT,
            daily_rate REAL,
            status TEXT NOT NULL DEFAULT 'disponible',
            notes TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS vehicle_reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reservation_date TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            client_id INTEGER,
            client_name TEXT NOT NULL,
            phone TEXT,
            vehicle_id INTEGER,
            concept TEXT NOT NULL,
            total_amount REAL NOT NULL CHECK(total_amount >= 0),
            deposit REAL NOT NULL CHECK(deposit >= 0),
            days_total INTEGER NOT NULL DEFAULT 1,
            remaining REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'activa',
            notes TEXT,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(client_id) REFERENCES clients(id),
            FOREIGN KEY(vehicle_id) REFERENCES vehicles(id),
            FOREIGN KEY(created_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS invoice_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            month_key TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(created_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS invoice_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(invoice_id) REFERENCES invoices(id)
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
    .replace("rental_start TEXT", "rental_start DATE")
    .replace("rental_end TEXT", "rental_end DATE")
    .replace("reservation_date TEXT", "reservation_date DATE")
    .replace("start_date TEXT", "start_date DATE")
    .replace("end_date TEXT", "end_date DATE")
    .replace("expense_date TEXT", "expense_date DATE")
    .replace("paid_date TEXT", "paid_date DATE")
    .replace("closure_date TEXT", "closure_date DATE")
    .replace("period_start TEXT", "period_start DATE")
    .replace("period_end TEXT", "period_end DATE")
    .replace("created_by INTEGER", "created_by BIGINT")
    .replace("closed_by INTEGER", "closed_by BIGINT")
    .replace("actor_id INTEGER", "actor_id BIGINT")
    .replace("employee_id INTEGER", "employee_id BIGINT")
    .replace("client_id INTEGER", "client_id BIGINT")
    .replace("vehicle_id INTEGER", "vehicle_id BIGINT")
    .replace("folder_id INTEGER", "folder_id BIGINT")
    .replace("invoice_id INTEGER", "invoice_id BIGINT")
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
        invoice_columns = {row[1] for row in connection.execute("PRAGMA table_info(invoices)")}
        for column, definition in (("folder_id", "INTEGER"), ("status", "TEXT NOT NULL DEFAULT 'pagada'")):
            if column not in invoice_columns:
                connection.execute(f"ALTER TABLE invoices ADD COLUMN {column} {definition}")
        expense_columns = {row[1] for row in connection.execute("PRAGMA table_info(expenses)")}
        if "category" not in expense_columns:
            connection.execute("ALTER TABLE expenses ADD COLUMN category TEXT NOT NULL DEFAULT 'Otros'")
        income_sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='incomes'").fetchone()[0]
        if "lavado" not in income_sql:
            connection.connection.executescript("""
                CREATE TABLE incomes_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL CHECK(category IN ('renta','taller','lavado')),
                    description TEXT NOT NULL, amount REAL NOT NULL CHECK(amount > 0), observations TEXT, income_date TEXT NOT NULL,
                    created_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(created_by) REFERENCES users(id));
                INSERT INTO incomes_new (id,category,description,amount,observations,income_date,created_by,created_at)
                    SELECT id,category,description,amount,observations,income_date,created_by,created_at FROM incomes;
                DROP TABLE incomes; ALTER TABLE incomes_new RENAME TO incomes;
            """)
        income_columns = {row[1] for row in connection.execute("PRAGMA table_info(incomes)")}
        for column in ("rental_start", "rental_end"):
            if column not in income_columns:
                connection.execute(f"ALTER TABLE incomes ADD COLUMN {column} TEXT")
        reservation_columns = {row[1] for row in connection.execute("PRAGMA table_info(vehicle_reservations)")}
        for column in ("start_date", "end_date"):
            if column not in reservation_columns:
                connection.execute(f"ALTER TABLE vehicle_reservations ADD COLUMN {column} TEXT")
    else:
        for statement in (
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS folder_id BIGINT",
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pagada'",
            "ALTER TABLE expenses ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'Otros'",
            "ALTER TABLE incomes ADD COLUMN IF NOT EXISTS rental_start DATE",
            "ALTER TABLE incomes ADD COLUMN IF NOT EXISTS rental_end DATE",
            "ALTER TABLE vehicle_reservations ADD COLUMN IF NOT EXISTS start_date DATE",
            "ALTER TABLE vehicle_reservations ADD COLUMN IF NOT EXISTS end_date DATE",
            "ALTER TABLE incomes DROP CONSTRAINT IF EXISTS incomes_category_check",
            "ALTER TABLE incomes ADD CONSTRAINT incomes_category_check CHECK (category IN ('renta', 'taller', 'lavado'))",
        ):
            connection.execute(statement)
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
    invoices_total = connection.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM invoices WHERE status = 'pagada' AND paid_date BETWEEN ? AND ?", (start, end)).fetchone()["total"]
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
    reservations_total = db().execute("SELECT COALESCE(SUM(deposit), 0) AS total FROM vehicle_reservations WHERE reservation_date = ? AND status = 'activa'", (today,)).fetchone()["total"]
    expenses_total = db().execute("SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE expense_date = ?", (today,)).fetchone()["total"]
    recent = [dict(row, entry_date=row["income_date"], entry_type="ingreso") for row in db().execute("SELECT * FROM incomes WHERE income_date = ? ORDER BY id DESC", (today,)).fetchall()]
    recent += [dict(row, entry_date=row["expense_date"], entry_type="gasto", category="gasto") for row in db().execute("SELECT * FROM expenses WHERE expense_date = ? ORDER BY id DESC", (today,)).fetchall()]
    recent += [dict(row, entry_date=row["reservation_date"], entry_type="reserva", category="reserva", description=f"Reserva: {row['concept']}", amount=row["deposit"], observations=row["client_name"]) for row in db().execute("SELECT * FROM vehicle_reservations WHERE reservation_date = ? AND status = 'activa' ORDER BY id DESC", (today,)).fetchall()]
    recent.sort(key=lambda row: (str(row["entry_date"]), row["id"]), reverse=True)
    if day_is_closed:
        data, expenses_total, reservations_total, recent = {}, 0, 0, []
    income_total = data.get("renta", 0) + data.get("taller", 0) + data.get("lavado", 0) + reservations_total
    return render_template("dashboard.html", rental_total=data.get("renta", 0), workshop_total=data.get("taller", 0), wash_total=data.get("lavado", 0), reservations_total=reservations_total, expenses_total=expenses_total, income_total=income_total, net_total=income_total - expenses_total, recent=recent[:8], today=today, day_is_closed=day_is_closed)


@app.route("/ingresos/nuevo", methods=["GET", "POST"])
@login_required
def new_income():
    if request.method == "POST":
        category = request.form.get("category")
        description = request.form.get("description", "").strip()
        observations = request.form.get("observations", "").strip()
        income_date = parse_date(request.form.get("income_date")).isoformat()
        rental_start = request.form.get("rental_start", "").strip() or None
        rental_end = request.form.get("rental_end", "").strip() or None
        try:
            amount = float(request.form.get("amount", 0))
            assert category in ("renta", "taller", "lavado") and description and amount > 0
            if category == "renta":
                assert rental_start and rental_end and parse_date(rental_start) <= parse_date(rental_end)
        except (ValueError, AssertionError):
            flash("Complete categoría, descripción y un monto mayor que cero.", "danger")
            return render_template("income_form.html", record=None)
        if is_closed(income_date):
            flash("No puede agregar movimientos: ese día ya fue cerrado.", "danger")
            return render_template("income_form.html", record=None)
        db().execute("INSERT INTO incomes (category, description, amount, observations, income_date, rental_start, rental_end, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (category, description, amount, observations, income_date, rental_start if category == "renta" else None, rental_end if category == "renta" else None, session["user_id"]))
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
        rental_start = request.form.get("rental_start", "").strip() or None
        rental_end = request.form.get("rental_end", "").strip() or None
        try:
            amount = float(request.form.get("amount", 0))
            assert category in ("renta", "taller", "lavado") and description and amount > 0
            if category == "renta":
                assert rental_start and rental_end and parse_date(rental_start) <= parse_date(rental_end)
        except (ValueError, AssertionError):
            flash("Complete categoría, descripción y un monto mayor que cero.", "danger")
            return render_template("income_form.html", record=record)
        if is_closed(new_date):
            flash("No puede mover un registro a un día ya cerrado.", "danger")
            return render_template("income_form.html", record=record)
        db().execute("UPDATE incomes SET category=?, description=?, amount=?, observations=?, income_date=?, rental_start=?, rental_end=? WHERE id=?", (category, description, amount, request.form.get("observations", "").strip(), new_date, rental_start if category == "renta" else None, rental_end if category == "renta" else None, income_id))
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


@app.route("/clientes", methods=["GET", "POST"])
@login_required
@admin_required
def clients():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("El nombre del cliente es obligatorio.", "danger")
        else:
            db().execute("INSERT INTO clients (name, phone, document_id, address, notes) VALUES (?, ?, ?, ?, ?)", (name, request.form.get("phone", "").strip(), request.form.get("document_id", "").strip(), request.form.get("address", "").strip(), request.form.get("notes", "").strip()))
            audit("Clientes", "Registró cliente", name); db().commit(); flash("Cliente agregado.", "success")
            return redirect(url_for("clients"))
    return render_template("clients.html", records=db().execute("SELECT * FROM clients WHERE active = 1 ORDER BY name").fetchall())


@app.route("/vehiculos", methods=["GET", "POST"])
@login_required
@admin_required
def vehicles():
    if request.method == "POST":
        plate, brand, model = request.form.get("plate", "").strip().upper(), request.form.get("brand", "").strip(), request.form.get("model", "").strip()
        try:
            daily_rate = float(request.form.get("daily_rate", 0) or 0)
            assert plate and brand and model
        except (ValueError, AssertionError):
            flash("Placa, marca y modelo son obligatorios.", "danger")
        else:
            try:
                db().execute("INSERT INTO vehicles (plate, brand, model, year, color, daily_rate, status, notes) VALUES (?, ?, ?, ?, ?, ?, 'disponible', ?)", (plate, brand, model, request.form.get("year") or None, request.form.get("color", "").strip(), daily_rate or None, request.form.get("notes", "").strip()))
                audit("Vehículos", "Registró vehículo", f"{plate} — {brand} {model}"); db().commit(); flash("Vehículo agregado.", "success")
            except Exception:
                flash("Ya existe un vehículo con esa placa.", "danger")
    return render_template("vehicles.html", records=db().execute("SELECT * FROM vehicles WHERE active = 1 ORDER BY plate").fetchall())


@app.route("/reservas", methods=["GET", "POST"])
@login_required
def reservations():
    connection = db()
    if request.method == "POST":
        reservation_date = parse_date(request.form.get("reservation_date")).isoformat()
        start_date = request.form.get("start_date", "").strip()
        end_date = request.form.get("end_date", "").strip()
        client_id = request.form.get("client_id", type=int)
        vehicle_id = request.form.get("vehicle_id", type=int)
        client = connection.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone() if client_id else None
        valid = True
        try:
            total, deposit, days_total = float(request.form.get("total_amount", 0)), float(request.form.get("deposit", 0)), int(request.form.get("days_total", 1))
            assert client and request.form.get("concept", "").strip() and total >= 0 and 0 <= deposit <= total and days_total > 0
            assert start_date and end_date and parse_date(start_date) <= parse_date(end_date)
        except (ValueError, AssertionError):
            flash("Complete cliente, concepto, montos y días válidos.", "danger")
            valid = False
        if valid and is_closed(reservation_date):
            flash("No puede registrar una reserva en un día cerrado.", "danger")
        elif valid:
            remaining = total - deposit
            connection.execute("INSERT INTO vehicle_reservations (reservation_date, start_date, end_date, client_id, client_name, phone, vehicle_id, concept, total_amount, deposit, days_total, remaining, notes, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (reservation_date, start_date, end_date, client_id, client["name"], request.form.get("phone", client["phone"] or "").strip(), vehicle_id, request.form.get("concept").strip(), total, deposit, days_total, remaining, request.form.get("notes", "").strip(), session["user_id"]))
            if vehicle_id: connection.execute("UPDATE vehicles SET status = 'reservado' WHERE id = ?", (vehicle_id,))
            audit("Reservas", "Registró reserva", f"{client['name']}: abono ${deposit:.2f}, saldo ${remaining:.2f} ({reservation_date})"); connection.commit()
            flash("Reserva y abono registrados.", "success"); return redirect(url_for("reservations"))
    records = connection.execute("SELECT vehicle_reservations.*, vehicles.plate FROM vehicle_reservations LEFT JOIN vehicles ON vehicles.id = vehicle_reservations.vehicle_id ORDER BY reservation_date DESC, id DESC LIMIT 100").fetchall()
    return render_template("reservations.html", records=records, clients=connection.execute("SELECT * FROM clients WHERE active = 1 ORDER BY name").fetchall(), vehicles=connection.execute("SELECT * FROM vehicles WHERE active = 1 AND status IN ('disponible','reservado') ORDER BY plate").fetchall())


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
        category = request.form.get("category", "Otros").strip() or "Otros"
        db().execute("INSERT INTO expenses (description, amount, observations, category, expense_date, created_by) VALUES (?, ?, ?, ?, ?, ?)", (description, amount, request.form.get("observations", "").strip(), category, expense_date, session["user_id"]))
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
        category = request.form.get("category", "Otros").strip() or "Otros"
        db().execute("UPDATE expenses SET description=?, amount=?, observations=?, category=?, expense_date=? WHERE id=?", (description, amount, request.form.get("observations", "").strip(), category, expense_date, expense_id)); audit("Gastos", "Modificó gasto", f"Registro #{expense_id}: {description} — ${amount:.2f} ({expense_date})"); db().commit()
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
            folder_id = request.form.get("folder_id", type=int)
            db().execute("INSERT INTO invoices (description, amount, paid_date, notes, folder_id, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                         (description, amount, parse_date(request.form.get("paid_date")).isoformat(), request.form.get("notes", "").strip(), folder_id, session["user_id"]))
            audit("Facturas", "Registró factura pagada", f"{description} — ${amount:.2f} ({parse_date(request.form.get('paid_date')).isoformat()})")
            db().commit()
            flash("Factura pagada registrada. Cuenta para las comisiones.", "success")
            return redirect(url_for("invoices"))
    records = db().execute("SELECT invoices.*, invoice_folders.name AS folder_name FROM invoices LEFT JOIN invoice_folders ON invoice_folders.id=invoices.folder_id ORDER BY paid_date DESC, invoices.id DESC LIMIT 100").fetchall()
    folders = db().execute("SELECT * FROM invoice_folders ORDER BY month_key DESC, name").fetchall()
    return render_template("invoices.html", records=records, folders=folders)


@app.route("/facturas/carpetas", methods=["POST"])
@login_required
@admin_required
def create_invoice_folder():
    month_key = request.form.get("month_key", "").strip()
    name = request.form.get("name", "").strip() or month_key
    try:
        datetime.strptime(month_key, "%Y-%m")
        assert name
        db().execute("INSERT INTO invoice_folders (name, month_key, created_by) VALUES (?, ?, ?)", (name, month_key, session["user_id"]))
        audit("Facturas", "Creó carpeta", f"{name} ({month_key})")
        db().commit(); flash("Carpeta mensual creada.", "success")
    except Exception:
        flash("No se pudo crear la carpeta; use un mes válido y un nombre que no exista.", "danger")
    return redirect(url_for("invoices"))


@app.route("/facturas/<int:invoice_id>/editar", methods=["POST"])
@login_required
@admin_required
def edit_invoice(invoice_id):
    record = db().execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not record:
        flash("Factura no encontrada.", "warning"); return redirect(url_for("invoices"))
    try:
        amount = float(request.form.get("amount", 0)); description = request.form.get("description", "").strip()
        assert description and amount > 0
        paid_date = parse_date(request.form.get("paid_date")).isoformat()
    except (ValueError, AssertionError):
        flash("La factura necesita descripción, fecha y monto válido.", "danger"); return redirect(url_for("invoices"))
    db().execute("UPDATE invoices SET description=?, amount=?, paid_date=?, notes=?, folder_id=? WHERE id=?", (description, amount, paid_date, request.form.get("notes", "").strip(), request.form.get("folder_id", type=int), invoice_id))
    audit("Facturas", "Editó factura", f"Factura #{invoice_id}: {description} — ${amount:.2f}")
    db().commit(); flash("Factura actualizada.", "success")
    return redirect(url_for("invoices"))


@app.route("/facturas/<int:invoice_id>/anular", methods=["POST"])
@login_required
@admin_required
def cancel_invoice(invoice_id):
    record = db().execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if record and record["status"] != "anulada":
        db().execute("UPDATE invoices SET status='anulada' WHERE id=?", (invoice_id,))
        audit("Facturas", "Anuló factura", f"Factura #{invoice_id}: {record['description']} — ${record['amount']:.2f}")
        db().commit(); flash("Factura anulada. Ya no cuenta para comisiones.", "success")
    return redirect(url_for("invoices"))


@app.route("/facturas/carpetas/<int:folder_id>/zip")
@login_required
@admin_required
def download_invoice_folder(folder_id):
    folder = db().execute("SELECT * FROM invoice_folders WHERE id=?", (folder_id,)).fetchone()
    if not folder:
        flash("Carpeta no encontrada.", "warning"); return redirect(url_for("invoices"))
    records = db().execute("SELECT * FROM invoices WHERE folder_id=? ORDER BY paid_date, id", (folder_id,)).fetchall()
    text = io.StringIO(); writer = csv.writer(text)
    writer.writerow(["Fecha", "Descripción", "Monto", "Estado", "Notas"])
    for row in records: writer.writerow([row["paid_date"], row["description"], f"{row['amount']:.2f}", row["status"], row["notes"]])
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{secure_filename(folder['name']) or 'facturas'}.csv", "\ufeff" + text.getvalue())
    payload.seek(0)
    audit("Facturas", "Descargó carpeta", folder["name"]); db().commit()
    return send_file(payload, mimetype="application/zip", as_attachment=True, download_name=f"facturas_{secure_filename(folder['name'])}.zip")


@app.route("/comisiones")
@login_required
@admin_required
def commissions():
    start, end = selected_range()
    connection = db()
    workshop = connection.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM incomes WHERE category = 'taller' AND income_date BETWEEN ? AND ?", (start, end)).fetchone()["total"]
    invoices_total = connection.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM invoices WHERE status='pagada' AND paid_date BETWEEN ? AND ?", (start, end)).fetchone()["total"]
    return render_template("commissions.html", start=start, end=end, workshop=workshop, invoices=invoices_total, base=workshop + invoices_total, commission=(workshop + invoices_total) * .01)


@app.route("/reportes")
@login_required
def reports():
    start, end = selected_range()
    category = request.args.get("category", "todos")
    clauses, parameters = ["income_date BETWEEN ? AND ?"], [start, end]
    if category in ("renta", "taller", "lavado"):
        clauses.append("category = ?")
        parameters.append(category)
    income_records = db().execute(f"SELECT * FROM incomes WHERE {' AND '.join(clauses)}", parameters).fetchall() if category not in ("gasto", "reserva") else []
    expense_records = db().execute("SELECT * FROM expenses WHERE expense_date BETWEEN ? AND ?", (start, end)).fetchall() if category in ("todos", "gasto") else []
    records = [dict(row, entry_date=row["income_date"], entry_type="ingreso") for row in income_records]
    records += [dict(row, entry_date=row["expense_date"], entry_type="gasto", category="gasto") for row in expense_records]
    if category in ("todos", "reserva"):
        records += [dict(row, entry_date=row["reservation_date"], entry_type="reserva", category="reserva", description=f"Reserva: {row['concept']}", amount=row["deposit"], observations=f"Cliente: {row['client_name']}") for row in db().execute("SELECT * FROM vehicle_reservations WHERE status='activa' AND reservation_date BETWEEN ? AND ?", (start, end)).fetchall()]
    records.sort(key=lambda row: (str(row["entry_date"]), row["id"]), reverse=True)
    totals = {"renta": 0, "taller": 0, "lavado": 0, "reserva": 0, "gasto": 0}
    for row in records: totals[row["category"]] += row["amount"]
    return render_template("reports.html", records=records, start=start, end=end, category=category, totals=totals)


@app.route("/reportes/csv")
@login_required
def report_csv():
    start, end = selected_range()
    category = request.args.get("category", "todos")
    query, params = "SELECT * FROM incomes WHERE income_date BETWEEN ? AND ?", [start, end]
    if category in ("renta", "taller", "lavado"):
        query += " AND category = ?"; params.append(category)
    income_rows = db().execute(query + " ORDER BY income_date, id", params).fetchall() if category not in ("gasto", "reserva") else []
    expense_rows = db().execute("SELECT * FROM expenses WHERE expense_date BETWEEN ? AND ? ORDER BY expense_date, id", (start, end)).fetchall() if category in ("todos", "gasto") else []
    from io import StringIO
    output = StringIO(); writer = csv.writer(output)
    writer.writerow(["Fecha", "Tipo", "Área", "Descripción", "Monto", "Observaciones"])
    writer.writerows([[r["income_date"], "Ingreso", r["category"], r["description"], f"{r['amount']:.2f}", r["observations"]] for r in income_rows])
    writer.writerows([[r["expense_date"], "Gasto", "Gasto", r["description"], f"{r['amount']:.2f}", r["observations"]] for r in expense_rows])
    if category in ("todos", "reserva"):
        for r in db().execute("SELECT * FROM vehicle_reservations WHERE status='activa' AND reservation_date BETWEEN ? AND ? ORDER BY reservation_date, id", (start, end)).fetchall(): writer.writerow([r["reservation_date"], "Reserva", "Reserva", r["concept"], f"{r['deposit']:.2f}", r["client_name"]])
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
    for category, folder in (("taller", "taller"), ("renta", "rentas"), ("lavado", "lavados")):
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
    reservations = connection.execute("SELECT * FROM vehicle_reservations WHERE reservation_date=? AND status='activa' ORDER BY id", (closure_date,)).fetchall()
    path = EXPORT_DIR / "reservas"; path.mkdir(parents=True, exist_ok=True)
    with (path / f"{closure_date}.csv").open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file); writer.writerow(["Fecha", "Cliente", "Concepto", "Abono", "Saldo"])
        for row in reservations: writer.writerow([row["reservation_date"], row["client_name"], row["concept"], f"{row['deposit']:.2f}", f"{row['remaining']:.2f}"])
    connection.execute("INSERT INTO day_closures (closure_date, closed_by) VALUES (?, ?)", (closure_date, session["user_id"]))
    audit("Cierres", "Cerró día", f"Cierre de caja del {closure_date}. Archivos generados: taller, rentas, lavados, reservas y gastos.")
    connection.commit()
    flash("Día cerrado. Se crearon los archivos de taller, rentas, lavados, reservas y gastos.", "success")
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
        active_admins = db().execute("SELECT COUNT(*) AS total FROM users WHERE role='admin' AND active=1").fetchone()["total"]
        if user and user["role"] == "admin" and user["active"] and active_admins <= 1:
            flash("No puede desactivar al último administrador activo.", "danger")
        elif user:
            db().execute("UPDATE users SET active = CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id = ?", (user_id,)); audit("Usuarios", "Cambió estado", f"Usuario {user['username']}: {'desactivado' if user['active'] else 'activado'}"); db().commit()
            flash("Estado del usuario actualizado.", "success")
        else:
            flash("No se encontró el usuario.", "warning")
    return redirect(url_for("users"))


@app.route("/usuarios/<int:user_id>/editar", methods=["GET", "POST"])
@login_required
@admin_required
def edit_user(user_id):
    user = db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        flash("No se encontró el usuario.", "warning")
        return redirect(url_for("users"))
    if request.method == "POST":
        username, role, password = request.form.get("username", "").strip(), request.form.get("role"), request.form.get("password", "")
        active_admins = db().execute("SELECT COUNT(*) AS total FROM users WHERE role='admin' AND active=1").fetchone()["total"]
        if not username or role not in ("admin", "user"):
            flash("Ingrese un usuario y un rol válidos.", "danger")
        elif password and len(password) < 6:
            flash("La nueva contraseña debe tener al menos 6 caracteres.", "danger")
        elif user["role"] == "admin" and user["active"] and role != "admin" and active_admins <= 1:
            flash("No puede quitar el rol al último administrador activo.", "danger")
        else:
            try:
                if password:
                    db().execute("UPDATE users SET username=?, role=?, password_hash=? WHERE id=?", (username, role, generate_password_hash(password), user_id))
                else:
                    db().execute("UPDATE users SET username=?, role=? WHERE id=?", (username, role, user_id))
                audit("Usuarios", "Editó usuario", f"Usuario #{user_id}: {user['username']} → {username}, rol {role}")
                db().commit(); flash("Usuario actualizado.", "success")
                return redirect(url_for("users"))
            except Exception:
                flash("Ese nombre de usuario ya existe.", "danger")
    return render_template("user_form.html", record=user)


@app.route("/usuarios/<int:user_id>/eliminar", methods=["POST"])
@login_required
@admin_required
def delete_user(user_id):
    user = db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        flash("No se encontró el usuario.", "warning")
    elif user_id == session["user_id"]:
        flash("No puede eliminar su propia cuenta mientras está conectado.", "warning")
    elif user["role"] == "admin" and user["active"] and db().execute("SELECT COUNT(*) AS total FROM users WHERE role='admin' AND active=1").fetchone()["total"] <= 1:
        flash("No puede eliminar al último administrador activo.", "danger")
    else:
        references = ("incomes", "expenses", "invoices", "day_closures", "payroll_payments", "audit_logs")
        used_by = [table for table in references if db().execute(f"SELECT 1 FROM {table} WHERE {'closed_by' if table == 'day_closures' else 'actor_id' if table == 'audit_logs' else 'created_by'} = ? LIMIT 1", (user_id,)).fetchone()]
        if used_by:
            flash("No se puede eliminar porque tiene movimientos o bitácora asociados. Desactívelo para conservar el historial.", "warning")
        else:
            db().execute("DELETE FROM users WHERE id = ?", (user_id,)); audit("Usuarios", "Eliminó usuario", f"Usuario {user['username']}"); db().commit()
            flash("Usuario eliminado.", "success")
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
