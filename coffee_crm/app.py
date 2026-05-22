from __future__ import annotations

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import os
from sqlalchemy import text
import json
import secrets
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
import time

app = Flask(__name__)

basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'coffee_shop.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get("COFFEE_CRM_SECRET_KEY", "dev-key-123")
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get("COFFEE_CRM_HTTPS", "0") == "1"

db = SQLAlchemy(app)

ADMIN_PASSWORD = os.environ.get("COFFEE_CRM_ADMIN_PASSWORD", "0000")

# --- Rate limit логина (простая защита для диплома) ---
LOGIN_MAX_ATTEMPTS = int(os.environ.get("COFFEE_CRM_LOGIN_MAX_ATTEMPTS", "8"))
LOGIN_WINDOW_SECONDS = int(os.environ.get("COFFEE_CRM_LOGIN_WINDOW_SECONDS", "300"))  # 5 минут
LOGIN_LOCK_SECONDS = int(os.environ.get("COFFEE_CRM_LOGIN_LOCK_SECONDS", "600"))      # 10 минут
_login_attempts: dict[str, dict[str, float]] = {}

def _login_key() -> str:
    return request.remote_addr or "unknown"

def _login_is_locked(key: str) -> int:
    rec = _login_attempts.get(key)
    if not rec:
        return 0
    locked_until = int(rec.get("locked_until", 0))
    now = int(time.time())
    return max(0, locked_until - now)

def _login_register_failure(key: str) -> None:
    now = int(time.time())
    rec = _login_attempts.get(key)
    if not rec or now - int(rec.get("first_ts", now)) > LOGIN_WINDOW_SECONDS:
        rec = {"count": 0, "first_ts": now, "locked_until": 0}
    rec["count"] = int(rec.get("count", 0)) + 1
    if int(rec["count"]) >= LOGIN_MAX_ATTEMPTS:
        rec["locked_until"] = now + LOGIN_LOCK_SECONDS
    _login_attempts[key] = rec

def _login_clear(key: str) -> None:
    _login_attempts.pop(key, None)

def require_login(role: str | None = None):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("auth"):
                return redirect(url_for("login", next=request.path))
            if role and session.get("role") != role:
                flash("Недостаточно прав для доступа.", "danger")
                return redirect(url_for("index"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator

def _active_shift_for_employee(employee_id: int) -> Shift | None:
    return Shift.query.filter_by(employee_id=employee_id, closed_at=None).order_by(Shift.opened_at.desc()).first()

def _log_shift_issue(shift_id: int | None, message: str) -> None:
    if not shift_id:
        return
    db.session.add(ShiftIssue(shift_id=shift_id, message=message[:255]))

# --- ЛЕГКАЯ МИГРАЦИЯ SQLite (для дипломного прототипа) ---
def _sqlite_column_exists(table_name: str, column_name: str) -> bool:
    rows = db.session.execute(text(f'PRAGMA table_info("{table_name}")')).all()
    return any(r[1] == column_name for r in rows)  # r[1] = name

def _sqlite_table_exists(table_name: str) -> bool:
    row = db.session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table_name},
    ).first()
    return row is not None

# --- МОДЕЛИ БАЗЫ ДАННЫХ ---

class Drink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    price = db.Column(db.Integer, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

class Inventory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Integer, default=0)

class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(100), default='')   # Имя
    last_name = db.Column(db.String(100), default='')    # Фамилия
    phone = db.Column(db.String(20), unique=True)
    bonuses = db.Column(db.Integer, default=0)           # 10% с каждой покупки
    date_registered = db.Column(db.DateTime, default=datetime.now)

class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    date_created = db.Column(db.DateTime, default=datetime.now, nullable=False)

class Shift(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False, index=True)
    opened_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    closed_at = db.Column(db.DateTime, nullable=True)
    cash_counted = db.Column(db.Integer, default=0, nullable=False)  # сумма в кассе (ввод при закрытии)

class ShiftIssue(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shift_id = db.Column(db.Integer, db.ForeignKey('shift.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    message = db.Column(db.String(255), nullable=False)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    drink_id = db.Column(db.Integer, db.ForeignKey('drink.id'), nullable=False)
    drink_name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=True)
    date_created = db.Column(db.DateTime, default=datetime.now)

class OrderHeader(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(100), default='', nullable=False)
    customer_phone = db.Column(db.String(20), default='', nullable=False)
    shift_id = db.Column(db.Integer, db.ForeignKey('shift.id'), nullable=True, index=True)
    is_admin_order = db.Column(db.Boolean, default=False, nullable=False)
    status = db.Column(db.String(20), default='new', nullable=False)  # new -> preparing -> ready
    total_sum = db.Column(db.Integer, default=0, nullable=False)      # сумма до списания бонусов
    redeemed_bonus = db.Column(db.Integer, default=0, nullable=False) # сколько бонусов списали
    final_sum = db.Column(db.Integer, default=0, nullable=False)      # сумма к оплате после списания
    date_created = db.Column(db.DateTime, default=datetime.now, nullable=False)

class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order_header.id'), nullable=False, index=True)
    drink_id = db.Column(db.Integer, db.ForeignKey('drink.id'), nullable=False)
    drink_name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    quantity = db.Column(db.Integer, default=1, nullable=False)

# Создание БД
with app.app_context():
    db.create_all()

    # Если база старая: добавляем недостающие поля в order и создаем drinks.
    # (SQLAlchemy create_all не делает ALTER TABLE.)
    if _sqlite_table_exists("order") and not _sqlite_column_exists("order", "drink_id"):
        db.session.execute(text('ALTER TABLE "order" ADD COLUMN drink_id INTEGER'))
        db.session.commit()

    # Новые таблицы для "корзины" (заказ + позиции)
    # create_all создаст их автоматически, но для уже существующей базы таблиц может не быть.
    # Проверяем и создаем, если нужно.
    if not _sqlite_table_exists("order_header"):
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS order_header (
                id INTEGER PRIMARY KEY,
                customer_name VARCHAR(100) NOT NULL DEFAULT '',
                customer_phone VARCHAR(20) NOT NULL DEFAULT '',
                shift_id INTEGER,
                is_admin_order BOOLEAN NOT NULL DEFAULT 0,
                status VARCHAR(20) NOT NULL DEFAULT 'new',
                total_sum INTEGER NOT NULL DEFAULT 0,
                redeemed_bonus INTEGER NOT NULL DEFAULT 0,
                final_sum INTEGER NOT NULL DEFAULT 0,
                date_created DATETIME NOT NULL
            )
        """))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_order_header_shift_id ON order_header(shift_id)"))
        db.session.commit()
    if not _sqlite_table_exists("order_item"):
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS order_item (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL,
                drink_id INTEGER NOT NULL,
                drink_name VARCHAR(100) NOT NULL,
                price INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1
            )
        """))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_order_item_order_id ON order_item(order_id)"))
        db.session.commit()

    # Таблица сотрудников
    if not _sqlite_table_exists("employee"):
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS employee (
                id INTEGER PRIMARY KEY,
                full_name VARCHAR(150) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                date_created DATETIME NOT NULL
            )
        """))
        db.session.commit()

    # Таблица смен
    if not _sqlite_table_exists("shift"):
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS shift (
                id INTEGER PRIMARY KEY,
                employee_id INTEGER NOT NULL,
                opened_at DATETIME NOT NULL,
                closed_at DATETIME,
                cash_counted INTEGER NOT NULL DEFAULT 0
            )
        """))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_shift_employee_id ON shift(employee_id)"))
        db.session.commit()

    # Таблица ошибок смены
    if not _sqlite_table_exists("shift_issue"):
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS shift_issue (
                id INTEGER PRIMARY KEY,
                shift_id INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                message VARCHAR(255) NOT NULL
            )
        """))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_shift_issue_shift_id ON shift_issue(shift_id)"))
        db.session.commit()

    # Миграции для order_header (добавление колонок)
    if _sqlite_table_exists("order_header"):
        if not _sqlite_column_exists("order_header", "customer_phone"):
            db.session.execute(text('ALTER TABLE order_header ADD COLUMN customer_phone VARCHAR(20) NOT NULL DEFAULT ""'))
            db.session.commit()
        if not _sqlite_column_exists("order_header", "total_sum"):
            db.session.execute(text('ALTER TABLE order_header ADD COLUMN total_sum INTEGER NOT NULL DEFAULT 0'))
            db.session.commit()
        if not _sqlite_column_exists("order_header", "redeemed_bonus"):
            db.session.execute(text('ALTER TABLE order_header ADD COLUMN redeemed_bonus INTEGER NOT NULL DEFAULT 0'))
            db.session.commit()
        if not _sqlite_column_exists("order_header", "final_sum"):
            db.session.execute(text('ALTER TABLE order_header ADD COLUMN final_sum INTEGER NOT NULL DEFAULT 0'))
            db.session.commit()
        if not _sqlite_column_exists("order_header", "shift_id"):
            db.session.execute(text('ALTER TABLE order_header ADD COLUMN shift_id INTEGER'))
            db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_order_header_shift_id ON order_header(shift_id)"))
            db.session.commit()
        if not _sqlite_column_exists("order_header", "is_admin_order"):
            db.session.execute(text('ALTER TABLE order_header ADD COLUMN is_admin_order BOOLEAN NOT NULL DEFAULT 0'))
            db.session.commit()

    # Технический "сотрудник" для админа, чтобы вести смены/продажи
    admin_emp = Employee.query.filter_by(full_name="Администратор").first()
    if not admin_emp:
        db.session.add(Employee(full_name="Администратор", password_hash=generate_password_hash("!admin-local!"), is_active=True))
        db.session.commit()

    if not Inventory.query.filter_by(name='Стаканчики').first():
        db.session.add(Inventory(name='Стаканчики', quantity=100))
        db.session.add(Inventory(name='Кофе (порции)', quantity=500))

    # Напитки (для терминала бариста)
    if not Drink.query.first():
        db.session.add_all([
            Drink(name='Капучино', price=180),
            Drink(name='Латте', price=200),
            Drink(name='Американо', price=150),
            Drink(name='Эспрессо', price=120),
        ])

    db.session.commit()

    # Заполняем drink_id у старых заказов по drink_name
    if _sqlite_table_exists("order") and _sqlite_column_exists("order", "drink_id"):
        drinks = {d.name: d.id for d in Drink.query.all()}
        for name, did in drinks.items():
            db.session.execute(
                text('UPDATE "order" SET drink_id = :did WHERE drink_id IS NULL AND drink_name = :name'),
                {"did": did, "name": name},
            )
        db.session.commit()

# --- МАРШРУТЫ ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if session.get("auth"):
            return redirect(url_for("index"))
        employees = Employee.query.filter_by(is_active=True).order_by(Employee.full_name.asc()).all()
        return render_template('login.html', next=request.args.get("next", "/"), employees=employees)

    key = _login_key()
    remaining = _login_is_locked(key)
    if remaining > 0:
        flash(f"Слишком много неверных попыток. Попробуйте через {remaining} сек.", "danger")
        return redirect(url_for("login", next=request.form.get("next", "/") or "/"))

    role = request.form.get('role', '').strip()
    next_url = request.form.get('next', '/').strip() or '/'

    if role == 'admin':
        password = request.form.get('password', '')
        if secrets.compare_digest(password, ADMIN_PASSWORD):
            session.clear()
            session["auth"] = True
            session["role"] = "admin"
            admin_emp = Employee.query.filter_by(full_name="Администратор").first()
            if admin_emp:
                session["employee_id"] = admin_emp.id
                session["employee_name"] = admin_emp.full_name
            _login_clear(key)
            flash("Вход выполнен.", "success")
            return redirect(next_url)
        _login_register_failure(key)
        flash("Неверный пароль администратора.", "danger")
        return redirect(url_for("login", next=next_url))

    if role == 'personal':
        employee_id_raw = request.form.get('employee_id', '').strip()
        password = request.form.get('password', '')
        if not employee_id_raw.isdigit():
            _login_register_failure(key)
            flash("Выберите сотрудника.", "danger")
            return redirect(url_for("login", next=next_url))
        employee = Employee.query.get(int(employee_id_raw))
        if not employee or not employee.is_active:
            _login_register_failure(key)
            flash("Сотрудник не найден или отключён.", "danger")
            return redirect(url_for("login", next=next_url))
        if not check_password_hash(employee.password_hash, password):
            _login_register_failure(key)
            flash("Неверный пароль сотрудника.", "danger")
            return redirect(url_for("login", next=next_url))
        session.clear()
        session["auth"] = True
        session["role"] = "personal"
        session["employee_id"] = employee.id
        session["employee_name"] = employee.full_name
        _login_clear(key)
        flash("Вход выполнен.", "success")
        return redirect(next_url)

    _login_register_failure(key)
    flash("Выберите, кто входит в систему.", "danger")
    return redirect(url_for("login", next=next_url))

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash("Вы вышли из системы.", "info")
    return redirect(url_for("login"))

@app.route('/')
@require_login()
def index():
    # Старые записи Order оставляем для совместимости, но основной поток идёт через OrderHeader/OrderItem
    old_orders = Order.query.order_by(Order.date_created.desc()).limit(50).all()
    recent_orders = db.session.execute(text("""
        SELECT oh.id, oh.customer_name, oh.customer_phone, oh.status, oh.date_created,
               oh.total_sum, oh.redeemed_bonus, oh.final_sum,
               COALESCE(SUM(oi.quantity), 0) AS total_qty
        FROM order_header oh
        LEFT JOIN order_item oi ON oi.order_id = oh.id
        GROUP BY oh.id
        ORDER BY oh.date_created DESC
        LIMIT 50
    """)).mappings().all()
    active_orders = db.session.execute(text("""
        SELECT oh.id, oh.customer_name, oh.status, oh.date_created, oh.final_sum,
               COALESCE(SUM(oi.quantity), 0) AS total_qty
        FROM order_header oh
        LEFT JOIN order_item oi ON oi.order_id = oh.id
        WHERE oh.status IN ('new', 'preparing', 'ready')
        GROUP BY oh.id
        ORDER BY oh.date_created DESC
        LIMIT 24
    """)).mappings().all()
    stock = Inventory.query.all()
    customers = Customer.query.order_by(Customer.date_registered.desc()).all()
    # Последний заказ по телефону клиента
    last_orders_rows = db.session.execute(text("""
        SELECT
          c.phone AS phone,
          (SELECT strftime('%d.%m.%Y %H:%M:%S', oh.date_created)
             FROM order_header oh
            WHERE oh.customer_phone = c.phone
            ORDER BY oh.date_created DESC
            LIMIT 1) AS last_dt,
          (SELECT oh.final_sum
             FROM order_header oh
            WHERE oh.customer_phone = c.phone
            ORDER BY oh.date_created DESC
            LIMIT 1) AS last_sum
        FROM customer c
    """)).mappings().all()
    last_orders = {}
    for r in last_orders_rows:
        if r["last_dt"] is None:
            continue
        dt = r["last_dt"]
        last_orders[str(r["phone"])] = {
            "last_order_at": dt,
            "last_order_sum": int(r["last_sum"] or 0),
        }
    drinks = Drink.query.filter_by(is_active=True).order_by(Drink.name.asc()).all()
    active_shift = None
    if session.get("employee_id"):
        active_shift = _active_shift_for_employee(int(session["employee_id"]))
    return render_template(
        'index.html',
        orders=recent_orders,
        active_orders=active_orders,
        old_orders=old_orders,
        stock=stock,
        drinks=drinks,
        customers=customers,
        last_orders=last_orders,
        active_shift=active_shift,
    )

@app.route('/api/customer_summary')
@require_login()
def api_customer_summary():
    phone = (request.args.get("phone", "") or "").strip()
    phone_digits = "".join(ch for ch in phone if ch.isdigit())
    if len(phone_digits) != 11:
        return jsonify({"found": False}), 200
    customer = Customer.query.filter_by(phone=phone_digits).first()
    if not customer:
        return jsonify({"found": False}), 200
    last = OrderHeader.query.filter_by(customer_phone=phone_digits).order_by(OrderHeader.date_created.desc()).first()
    last_at = None
    if last and last.date_created:
        last_at = last.date_created.strftime('%d.%m.%Y %H:%M:%S')
    return jsonify({
        "found": True,
        "bonuses": int(customer.bonuses or 0),
        "last_order_at": last_at,
    }), 200

@app.route('/place_order', methods=['POST'])
@require_login()
def place_order():
    customer_name = request.form.get('customer_name', '').strip()
    customer_phone = request.form.get('customer_phone', '').strip()
    redeem_bonuses = request.form.get('redeem_bonuses', '').strip() in ('1', 'true', 'on', 'yes')
    cart_json = request.form.get('cart_json', '').strip()

    if not customer_name:
        flash('Введите имя клиента.', 'danger')
        return redirect(url_for('index'))

    try:
        cart = json.loads(cart_json) if cart_json else []
    except Exception:
        cart = []

    if not isinstance(cart, list) or len(cart) == 0:
        flash('Добавьте хотя бы один напиток в заказ.', 'danger')
        return redirect(url_for('index'))

    # Валидация корзины + сбор позиций
    items = []
    for row in cart:
        if not isinstance(row, dict):
            continue
        drink_id = row.get('drink_id')
        qty = row.get('quantity', 1)
        if not isinstance(drink_id, int) or not isinstance(qty, int):
            continue
        if qty <= 0 or qty > 20:
            continue
        drink = Drink.query.get(drink_id)
        if not drink or not drink.is_active:
            continue
        items.append((drink, qty))

    if not items:
        flash('Корзина некорректна: выберите напитки заново.', 'danger')
        return redirect(url_for('index'))

    # Привязка к смене:
    # - для персонала смена обязательна
    # - для админа смена опциональна (если открыта — привяжем)
    shift_id = None
    if session.get("role") in ("personal", "admin"):
        emp_id = session.get("employee_id")
        if not emp_id:
            flash("Не найден сотрудник в сессии. Перезайдите.", "danger")
            return redirect(url_for("index"))
        shift = _active_shift_for_employee(int(emp_id))
        if not shift and session.get("role") == "personal":
            flash("Смена не открыта. Нажмите «Открыть смену».", "danger")
            return redirect(url_for("index"))
        shift_id = shift.id if shift else None

    # Начисление бонусов, если клиент уже есть в базе
    phone_digits = ''.join(ch for ch in customer_phone if ch.isdigit())
    customer = None
    cashback = 0
    redeemed = 0
    if phone_digits:
        if len(phone_digits) != 11:
            flash('Некорректный номер телефона. Введите 11 цифр (например: 89991234567).', 'warning')
            _log_shift_issue(shift_id, "Некорректный телефон при оформлении заказа")
        else:
            customer = Customer.query.filter_by(phone=phone_digits).first()
            if not customer:
                flash('Клиент с таким телефоном не найден. Добавьте его во вкладке «Клиенты» — бонусы не начислены.', 'info')
                _log_shift_issue(shift_id, "Клиент не найден по телефону (бонусы не начислены)")

    header = OrderHeader(
        customer_name=customer_name,
        customer_phone=phone_digits if len(phone_digits) == 11 else '',
        shift_id=shift_id,
        is_admin_order=(session.get("role") == "admin"),
        status='new'
    )
    db.session.add(header)
    db.session.flush()

    total_qty = 0
    total_sum = 0
    for drink, qty in items:
        total_sum += int(drink.price) * qty
        db.session.add(OrderItem(
            order_id=header.id,
            drink_id=drink.id,
            drink_name=drink.name,
            price=int(drink.price),
            quantity=qty,
        ))
        total_qty += qty

    # Списание бонусов (если клиент найден и включили опцию)
    final_sum = total_sum
    if redeem_bonuses:
        if not customer:
            flash('Чтобы списать бонусы, укажите телефон клиента из базы.', 'warning')
            _log_shift_issue(shift_id, "Попытка списать бонусы без найденного клиента")
        else:
            available = int(customer.bonuses or 0)
            max_redeem = total_sum // 2  # не более 50% от суммы заказа
            redeemed = min(available, max_redeem)
            if redeemed > 0:
                customer.bonuses = available - redeemed
                final_sum = total_sum - redeemed

    # Начисление бонусов (10% от суммы к оплате)
    if customer and final_sum > 0:
        cashback = final_sum // 10
        if cashback > 0:
            customer.bonuses = int(customer.bonuses or 0) + cashback

    header.total_sum = int(total_sum)
    header.redeemed_bonus = int(redeemed)
    header.final_sum = int(final_sum)

    # Списание расходников: 1 стакан + 1 порция кофе на каждую чашку
    cups = Inventory.query.filter_by(name='Стаканчики').first()
    coffee = Inventory.query.filter_by(name='Кофе (порции)').first()
    if cups:
        cups.quantity = max(0, int(cups.quantity or 0) - total_qty)
    if coffee:
        coffee.quantity = max(0, int(coffee.quantity or 0) - total_qty)

    db.session.commit()
    if redeemed > 0 and cashback > 0:
        flash(f'Заказ №{header.id} оформлен. Списано бонусов: {redeemed}. Начислено бонусов: {cashback}.', 'success')
    elif redeemed > 0:
        flash(f'Заказ №{header.id} оформлен. Списано бонусов: {redeemed}.', 'success')
    elif customer and cashback > 0:
        flash(f'Заказ №{header.id} оформлен. Начислено бонусов: {cashback}.', 'success')
    else:
        flash(f'Заказ №{header.id} оформлен.', 'success')
    return redirect(url_for('index'))

@app.route('/shift/open', methods=['POST'])
@require_login()
def shift_open():
    emp_id = int(session.get("employee_id") or 0)
    if not emp_id:
        flash("Не найден сотрудник. Перезайдите.", "danger")
        return redirect(url_for("index"))
    existing = _active_shift_for_employee(emp_id)
    if existing:
        flash("Смена уже открыта.", "info")
        return redirect(url_for("index"))
    s = Shift(employee_id=emp_id, opened_at=datetime.now(), cash_counted=0)
    db.session.add(s)
    db.session.commit()
    flash(f"Смена открыта (#{s.id}).", "success")
    return redirect(url_for("index"))

@app.route('/shift/close', methods=['POST'])
@require_login()
def shift_close():
    emp_id = int(session.get("employee_id") or 0)
    if not emp_id:
        flash("Не найден сотрудник. Перезайдите.", "danger")
        return redirect(url_for("index"))
    shift = _active_shift_for_employee(emp_id)
    if not shift:
        flash("Нет открытой смены.", "danger")
        return redirect(url_for("index"))
    cash_raw = (request.form.get("cash_counted") or "").strip()
    if not cash_raw.isdigit():
        flash("Введите корректную сумму в кассе (целое число).", "danger")
        return redirect(url_for("index"))
    shift.cash_counted = int(cash_raw)
    shift.closed_at = datetime.now()
    db.session.commit()
    flash("Смена закрыта.", "success")
    return redirect(url_for("index"))

@app.route('/supply')
@require_login(role="admin")
def supply():
    stock = Inventory.query.all()
    return render_template('supply.html', stock=stock)

@app.route('/supply_update', methods=['POST'])
@require_login(role="admin")
def supply_update():
    item = request.form.get('item', '').strip()
    qty_raw = request.form.get('quantity', '').strip()
    if not qty_raw.isdigit():
        flash('Введите корректное количество (целое число).', 'danger')
        return redirect(url_for('supply'))
    qty = int(qty_raw)
    if qty <= 0 or qty > 100000:
        flash('Количество должно быть больше 0.', 'danger')
        return redirect(url_for('supply'))

    inv = Inventory.query.filter_by(name=item).first()
    if not inv:
        flash('Номенклатура не найдена.', 'danger')
        return redirect(url_for('supply'))

    inv.quantity = int(inv.quantity or 0) + qty
    db.session.commit()
    flash(f'Поставка принята: {item} +{qty}.', 'success')
    return redirect(url_for('supply'))

@app.route('/add_customer', methods=['POST'])
@require_login()
def add_customer():
    """Регистрация нового клиента с именем и фамилией."""
    first_name = request.form.get('first_name', '').strip()
    last_name = request.form.get('last_name', '').strip()
    phone = request.form.get('phone', '').strip()

    if not phone or len(phone) != 11 or not phone.isdigit():
        flash('Некорректный номер телефона. Введите 11 цифр (например: 89991234567)', 'danger')
        return redirect(url_for('index') + '#customers-tab')

    existing = Customer.query.filter_by(phone=phone).first()
    if existing:
        # Обновляем данные, если клиент уже есть
        existing.first_name = first_name
        existing.last_name = last_name
        db.session.commit()
        flash(f'Данные клиента {phone} обновлены.', 'info')
    else:
        new_customer = Customer(first_name=first_name, last_name=last_name, phone=phone)
        db.session.add(new_customer)
        db.session.commit()
        flash(f'Клиент {first_name} {last_name} добавлен!', 'success')

    return redirect(url_for('index') + '#customers-tab')

@app.route('/delete_customer/<int:customer_id>', methods=['POST'])
@require_login(role="admin")
def delete_customer(customer_id):
    """Удаление клиента (отвязываем его заказы, потом удаляем)."""
    customer = Customer.query.get_or_404(customer_id)
    # Обнуляем ссылку в заказах, чтобы не нарушить целостность
    Order.query.filter_by(customer_id=customer_id).update({'customer_id': None})
    db.session.delete(customer)
    db.session.commit()
    return redirect(url_for('index') + '#customers-tab')

@app.route('/admin')
@require_login(role="admin")
def admin_panel():
    stock = Inventory.query.all()
    total_sales_row = db.session.execute(text("SELECT COALESCE(SUM(final_sum), 0) AS total_sales FROM order_header")).mappings().first()
    total_sales = int(total_sales_row["total_sales"] or 0)
    total_customers = Customer.query.count()
    employees = Employee.query.order_by(Employee.date_created.desc()).all()
    closed_shifts = db.session.execute(text("""
        SELECT s.id, s.employee_id, e.full_name,
               strftime('%d.%m.%Y', s.opened_at) AS day,
               COALESCE(SUM(oh.final_sum), 0) AS revenue
        FROM shift s
        JOIN employee e ON e.id = s.employee_id
        LEFT JOIN order_header oh ON oh.shift_id = s.id
        WHERE s.closed_at IS NOT NULL
        GROUP BY s.id
        ORDER BY s.closed_at DESC
        LIMIT 200
    """)).mappings().all()
    return render_template(
        "admin.html",
        stock=stock,
        total_sales=total_sales,
        total_customers=total_customers,
        employees=employees,
        closed_shifts=closed_shifts,
    )

@app.route('/admin/add_employee', methods=['POST'])
@require_login(role="admin")
def admin_add_employee():
    full_name = request.form.get("full_name", "").strip()
    password = request.form.get("password", "")
    if not full_name or len(full_name) < 3:
        flash("Введите ФИО сотрудника.", "danger")
        return redirect(url_for("admin_panel"))
    if not password or len(password) < 4:
        flash("Пароль должен быть минимум 4 символа.", "danger")
        return redirect(url_for("admin_panel"))
    existing = Employee.query.filter_by(full_name=full_name).first()
    if existing:
        flash("Сотрудник с таким ФИО уже существует.", "danger")
        return redirect(url_for("admin_panel"))
    emp = Employee(
        full_name=full_name,
        password_hash=generate_password_hash(password),
        is_active=True,
    )
    db.session.add(emp)
    db.session.commit()
    flash("Сотрудник добавлен.", "success")
    return redirect(url_for("admin_panel"))

@app.route('/admin/employees/<int:employee_id>/toggle', methods=['POST'])
@require_login(role="admin")
def admin_toggle_employee(employee_id: int):
    employee = Employee.query.get_or_404(employee_id)
    if employee.full_name == "Администратор":
        flash("Нельзя отключить технического администратора.", "danger")
        return redirect(url_for("admin_panel"))
    employee.is_active = not bool(employee.is_active)
    db.session.commit()
    flash("Статус сотрудника обновлён.", "success")
    return redirect(url_for("admin_panel"))

@app.route('/admin/employees/<int:employee_id>/delete', methods=['POST'])
@require_login(role="admin")
def admin_delete_employee(employee_id: int):
    employee = Employee.query.get_or_404(employee_id)
    if employee.full_name == "Администратор":
        flash("Нельзя удалить технического администратора.", "danger")
        return redirect(url_for("admin_panel"))
    # Не удаляем сотрудника, если есть смены — чтобы не ломать историю
    has_shifts = Shift.query.filter_by(employee_id=employee_id).first() is not None
    if has_shifts:
        flash("Нельзя удалить сотрудника: у него есть смены/история. Выключите его вместо удаления.", "warning")
        return redirect(url_for("admin_panel"))
    db.session.delete(employee)
    db.session.commit()
    flash("Сотрудник удалён.", "success")
    return redirect(url_for("admin_panel"))

@app.route('/admin/employees/<int:employee_id>')
@require_login(role="admin")
def admin_employee(employee_id: int):
    employee = Employee.query.get_or_404(employee_id)
    day = (request.args.get("day") or "").strip()
    if not day:
        day = datetime.now().strftime('%Y-%m-%d')
    revenue_row = db.session.execute(text("""
        SELECT COALESCE(SUM(oh.final_sum), 0) AS revenue
        FROM shift s
        LEFT JOIN order_header oh ON oh.shift_id = s.id
        WHERE s.employee_id = :eid
          AND s.closed_at IS NOT NULL
          AND date(s.opened_at) = :day
    """), {"eid": employee_id, "day": day}).mappings().first()
    revenue = int(revenue_row["revenue"] or 0)
    shifts = db.session.execute(text("""
        SELECT s.id,
               strftime('%H:%M:%S', s.opened_at) AS opened_at,
               strftime('%H:%M:%S', s.closed_at) AS closed_at,
               s.cash_counted,
               COALESCE(SUM(oh.final_sum), 0) AS revenue
        FROM shift s
        LEFT JOIN order_header oh ON oh.shift_id = s.id
        WHERE s.employee_id = :eid
          AND s.closed_at IS NOT NULL
          AND date(s.opened_at) = :day
        GROUP BY s.id
        ORDER BY s.closed_at DESC
    """), {"eid": employee_id, "day": day}).mappings().all()
    return render_template("employee.html", employee=employee, day=day, revenue=revenue, shifts=shifts)

@app.route('/admin/shifts/<int:shift_id>')
@require_login(role="admin")
def admin_shift_detail(shift_id: int):
    shift = db.session.execute(text("""
        SELECT s.id, s.employee_id, e.full_name,
               strftime('%d.%m.%Y %H:%M:%S', s.opened_at) AS opened_at,
               strftime('%d.%m.%Y %H:%M:%S', s.closed_at) AS closed_at,
               s.cash_counted
        FROM shift s
        JOIN employee e ON e.id = s.employee_id
        WHERE s.id = :sid
    """), {"sid": shift_id}).mappings().first()
    if not shift:
        flash("Смена не найдена.", "danger")
        return redirect(url_for("admin_panel"))
    drinks = db.session.execute(text("""
        SELECT oi.drink_name, SUM(oi.quantity) AS qty, SUM(oi.price * oi.quantity) AS sum
        FROM order_item oi
        JOIN order_header oh ON oh.id = oi.order_id
        WHERE oh.shift_id = :sid
        GROUP BY oi.drink_name
        ORDER BY qty DESC
    """), {"sid": shift_id}).mappings().all()
    issues = ShiftIssue.query.filter_by(shift_id=shift_id).order_by(ShiftIssue.created_at.desc()).limit(200).all()
    revenue_row = db.session.execute(text("""
        SELECT COALESCE(SUM(final_sum), 0) AS revenue FROM order_header WHERE shift_id = :sid
    """), {"sid": shift_id}).mappings().first()
    revenue = int(revenue_row["revenue"] or 0)
    cups_row = db.session.execute(text("""
        SELECT COALESCE(SUM(oi.quantity), 0) AS cups
        FROM order_item oi
        JOIN order_header oh ON oh.id = oi.order_id
        WHERE oh.shift_id = :sid
    """), {"sid": shift_id}).mappings().first()
    cups = int(cups_row["cups"] or 0)
    return render_template("shift_detail.html", shift=shift, drinks=drinks, issues=issues, revenue=revenue, cups=cups)

@app.route('/orders/<int:order_id>/ready', methods=['POST'])
@require_login()
def order_ready(order_id: int):
    order = OrderHeader.query.get_or_404(order_id)
    if order.status in ('new', 'preparing'):
        order.status = 'ready'
        db.session.commit()
    return redirect(url_for('index') + '#terminal')

@app.route('/orders/<int:order_id>/complete', methods=['POST'])
@require_login()
def order_complete(order_id: int):
    order = OrderHeader.query.get_or_404(order_id)
    if order.status == 'ready':
        order.status = 'done'
        db.session.commit()
    return redirect(url_for('index') + '#terminal')

@app.route('/monitor')
def monitor():
    return render_template('monitor.html')

@app.route('/api/monitor')
def api_monitor():
    active_orders = db.session.execute(text("""
        SELECT oh.id, oh.customer_name, oh.status, oh.date_created, oh.final_sum,
               COALESCE(SUM(oi.quantity), 0) AS total_qty
        FROM order_header oh
        LEFT JOIN order_item oi ON oi.order_id = oh.id
        WHERE oh.status IN ('new', 'preparing', 'ready')
        GROUP BY oh.id
        ORDER BY oh.date_created ASC
        LIMIT 60
    """)).mappings().all()
    return jsonify({
        "orders": [
            {
                "id": int(o["id"]),
                "customer_name": o["customer_name"] or "",
                "status": o["status"] or "new",
                "total_qty": int(o["total_qty"] or 0),
                "final_sum": int(o["final_sum"] or 0),
                "created_at": (o["date_created"].strftime('%H:%M:%S') if hasattr(o["date_created"], "strftime") else str(o["date_created"])),
            } for o in active_orders
        ],
        "generated_at": datetime.now().strftime('%H:%M:%S')
    })

@app.route('/api/stats')
@require_login(role="admin")
def api_stats():
    stats_sales = db.session.execute(text("""
        SELECT COALESCE(SUM(final_sum), 0) AS total_sales FROM order_header
    """)).mappings().first()
    total_sales = int(stats_sales["total_sales"] or 0)
    total_customers = Customer.query.count()
    stock = Inventory.query.all()
    return jsonify({
        "total_sales": total_sales,
        "total_customers": total_customers,
        "stock": [{"name": i.name, "quantity": i.quantity} for i in stock],
        "generated_at": datetime.now().isoformat(),
    })

if __name__ == "__main__":
    app.run(debug=True)
