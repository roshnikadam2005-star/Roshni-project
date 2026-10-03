"""Application routes and API endpoints."""
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from functools import wraps

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import or_

from models import Cart, Order, OrderItem, Product, User, Wishlist, db

main_bp = Blueprint("main", __name__)
api_bp = Blueprint("api", __name__)
TOKEN_SALT = "crossword-auth"
GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"
LAST_GOOGLE_SYNC_AT = 0.0


def _json_error(message: str, status_code: int = 400):
    return jsonify({"error": message}), status_code


def _token_serializer():
    # Shared serializer for signing auth tokens.
    from flask import current_app

    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"])


def create_access_token(identity: str) -> str:
    return _token_serializer().dumps(identity, salt=TOKEN_SALT)


def get_jwt_identity():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        return _token_serializer().loads(token, salt=TOKEN_SALT, max_age=60 * 60 * 24 * 7)
    except (BadSignature, SignatureExpired):
        return None


def jwt_required():
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user_id = get_jwt_identity()
            if not user_id:
                return _json_error("Unauthorized", 401)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def _current_user():
    user_id = get_jwt_identity()
    if not user_id:
        return None
    return db.session.get(User, int(user_id))


def _cart_payload(user_id: int) -> dict:
    lines = Cart.query.filter_by(user_id=user_id).all()
    items = []
    total = 0.0
    for line in lines:
        if not line.product:
            continue
        line_total = line.quantity * line.product.price
        total += line_total
        items.append(
            {
                "product_id": line.product_id,
                "quantity": line.quantity,
                "line_total": round(line_total, 2),
                "product": line.product.to_dict(),
            }
        )
    return {"items": items, "total": round(total, 2)}


def _admin_required():
    user = _current_user()
    if not user:
        return None, _json_error("Unauthorized", 401)
    if user.role != "admin":
        return None, _json_error("Admin access required", 403)
    return user, None


def _load_books_from_local_json():
    books_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "books.json")
    if not os.path.exists(books_path):
        return []
    with open(books_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _fetch_google_books(limit: int = 20):
    """Fetch real books from Google Books API."""
    query = urllib.parse.urlencode({"q": "bestseller books", "maxResults": min(limit, 40), "langRestrict": "en"})
    url = f"{GOOGLE_BOOKS_URL}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))

    books = []
    for index, item in enumerate(payload.get("items", []), start=1):
        info = item.get("volumeInfo", {})
        sale = item.get("saleInfo", {})
        title = (info.get("title") or "").strip()
        authors = info.get("authors") or ["Unknown Author"]
        image_links = info.get("imageLinks") or {}
        thumbnail = image_links.get("thumbnail") or image_links.get("smallThumbnail")
        if not title or not thumbnail:
            continue
        thumbnail = thumbnail.replace("http://", "https://")

        amount = None
        list_price = sale.get("listPrice") or {}
        if isinstance(list_price, dict):
            amount = list_price.get("amount")
        if amount is None:
            amount = 249 + (index * 37) % 451

        categories = info.get("categories") or ["General"]
        description = (info.get("description") or "No description available yet.").strip()
        rating = float(info.get("averageRating") or 4.2)

        books.append(
            {
                "title": title[:255],
                "author": ", ".join(authors)[:180],
                "price": float(amount),
                "category": str(categories[0])[:80],
                "image": thumbnail[:512],
                "rating": max(1.0, min(5.0, rating)),
                "description": description[:2000],
            }
        )
        if len(books) >= limit:
            break
    return books


def _sync_books_if_needed():
    global LAST_GOOGLE_SYNC_AT

    books = []
    should_try_google = (time.time() - LAST_GOOGLE_SYNC_AT) > (60 * 60 * 12)
    if should_try_google:
        try:
            books = _fetch_google_books(limit=25)
            LAST_GOOGLE_SYNC_AT = time.time()
        except Exception:
            books = []

    if not books and Product.query.count() < 20:
        books = _load_books_from_local_json()

    existing_titles = {title for (title,) in Product.query.with_entities(Product.title).all()}
    for book in books:
        if book.get("title") and book["title"] not in existing_titles:
            db.session.add(Product(**book))
    db.session.commit()


def _session_cart_items():
    raw = session.get("book_cart", {})
    if not isinstance(raw, dict):
        return {}
    return {int(k): int(v) for k, v in raw.items() if str(k).isdigit() and int(v) > 0}


def _save_session_cart(cart_dict):
    session["book_cart"] = {str(k): int(v) for k, v in cart_dict.items() if int(v) > 0}
    session.modified = True


def _build_cart_lines_and_total():
    cart = _session_cart_items()
    lines = []
    total = 0.0
    for product_id, quantity in cart.items():
        product = db.session.get(Product, product_id)
        if not product:
            continue
        line_total = product.price * quantity
        total += line_total
        lines.append({"book": product, "quantity": quantity, "line_total": round(line_total, 2)})
    return lines, round(total, 2), sum(cart.values())


def _orders_db_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")


@main_bp.route("/")
def index():
    _sync_books_if_needed()
    query = Product.query
    search = (request.args.get("search") or "").strip()
    min_price = request.args.get("min_price")
    min_rating = request.args.get("min_rating")

    if search:
        like = f"%{search}%"
        query = query.filter(or_(Product.title.ilike(like), Product.author.ilike(like)))
    if min_price not in (None, ""):
        try:
            query = query.filter(Product.price >= float(min_price))
        except ValueError:
            pass
    if min_rating not in (None, ""):
        try:
            query = query.filter(Product.rating >= float(min_rating))
        except ValueError:
            pass

    books = query.order_by(Product.rating.desc(), Product.id.asc()).all()
    # Show only half of the matched books in the "All books" section.
    half_count = max(1, len(books) // 2) if books else 0
    books = books[:half_count]
    trending_books = Product.query.order_by(Product.rating.desc(), Product.id.asc()).limit(12).all()
    featured_books = Product.query.order_by(Product.id.desc()).limit(12).all()
    category_rows = db.session.query(Product.category).distinct().order_by(Product.category.asc()).all()
    categories = []
    for (category_name,) in category_rows:
        cover_book = (
            Product.query.filter_by(category=category_name)
            .order_by(Product.rating.desc(), Product.id.asc())
            .first()
        )
        categories.append(
            {
                "name": category_name,
                "image": cover_book.image if cover_book else "",
            }
        )
    cart_count = sum(_session_cart_items().values())
    return render_template(
        "index.html",
        books=books,
        trending_books=trending_books,
        featured_books=featured_books,
        categories=categories,
        search=search,
        min_price=min_price or "",
        min_rating=min_rating or "",
        cart_count=cart_count,
    )


@main_bp.route("/products")
def products_page():
    # Legacy route kept for backward compatibility with old links.
    return redirect(url_for("main.index", **request.args))


@main_bp.route("/product/<int:product_id>")
def product_page(product_id: int):
    # Legacy route kept for backward compatibility with old links.
    return redirect(url_for("main.book_page", book_id=product_id))


@main_bp.route("/book/<int:book_id>")
def book_page(book_id: int):
    _sync_books_if_needed()
    book = db.session.get(Product, book_id)
    if not book:
        return redirect(url_for("main.index"))
    return render_template("book.html", book=book, cart_count=sum(_session_cart_items().values()))


@main_bp.route("/login")
def login_page():
    return render_template("login.html")


@main_bp.route("/signup")
def signup_page():
    return render_template("signup.html")


@main_bp.route("/cart")
def cart_page():
    _sync_books_if_needed()
    lines, total, cart_count = _build_cart_lines_and_total()
    return render_template("cart.html", lines=lines, total=total, cart_count=cart_count)


@main_bp.post("/cart/add/<int:book_id>")
def add_book_to_cart(book_id: int):
    qty = request.form.get("quantity", "1")
    try:
        qty = max(1, int(qty))
    except ValueError:
        qty = 1
    cart = _session_cart_items()
    cart[book_id] = cart.get(book_id, 0) + qty
    _save_session_cart(cart)
    next_url = request.form.get("next") or request.referrer or url_for("main.index")
    return redirect(next_url)


@main_bp.post("/cart/remove/<int:book_id>")
def remove_book_from_cart(book_id: int):
    cart = _session_cart_items()
    if book_id in cart:
        cart.pop(book_id)
        _save_session_cart(cart)
    return redirect(url_for("main.cart_page"))


@main_bp.route("/checkout")
def checkout_page():
    _sync_books_if_needed()
    lines, total, cart_count = _build_cart_lines_and_total()
    if not lines:
        return redirect(url_for("main.cart_page"))
    return render_template("checkout.html", lines=lines, total=total, cart_count=cart_count)


@main_bp.post("/process_payment")
def process_payment():
    _sync_books_if_needed()
    lines, total, _ = _build_cart_lines_and_total()
    if not lines:
        return redirect(url_for("main.cart_page"))

    payment_method = (request.form.get("payment_method") or "").strip().lower()
    upi_id = (request.form.get("upi_id") or "").strip()
    card_number = (request.form.get("card_number") or "").strip().replace(" ", "")
    card_holder = (request.form.get("card_holder") or "").strip()
    expiry_date = (request.form.get("expiry_date") or "").strip()
    cvv = (request.form.get("cvv") or "").strip()

    if payment_method == "upi":
        if not upi_id or "@" not in upi_id:
            return redirect(url_for("main.checkout_page"))
    elif payment_method == "card":
        if not (card_number.isdigit() and len(card_number) == 16):
            return redirect(url_for("main.checkout_page"))
        if not card_holder or len(expiry_date) != 5 or not (cvv.isdigit() and len(cvv) in (3, 4)):
            return redirect(url_for("main.checkout_page"))
    else:
        return redirect(url_for("main.checkout_page"))

    items_json = json.dumps(
        [
            {
                "book_id": line["book"].id,
                "title": line["book"].title,
                "quantity": line["quantity"],
                "price": line["book"].price,
            }
            for line in lines
        ]
    )
    user_label = "guest"
    if session.get("user_name"):
        user_label = str(session.get("user_name"))
    elif request.form.get("user"):
        user_label = request.form.get("user")

    conn = sqlite3.connect(_orders_db_path())
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO orders_checkout (user, items, total_price, payment_method, status) VALUES (?, ?, ?, ?, ?)",
            (user_label, items_json, total, payment_method, "success"),
        )
        order_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    _save_session_cart({})
    session["last_order"] = {
        "id": order_id,
        "items": [
            {
                "title": line["book"].title,
                "quantity": line["quantity"],
                "line_total": line["line_total"],
            }
            for line in lines
        ],
        "total": total,
        "payment_method": "Google Pay (UPI)" if payment_method == "upi" else "Debit Card",
        "status": "Payment Successful",
    }
    return redirect(url_for("main.success_page"))


@main_bp.route("/success")
def success_page():
    order = session.get("last_order")
    if not order:
        return redirect(url_for("main.index"))
    return render_template("success.html", order=order, cart_count=0)


@main_bp.route("/wishlist")
def wishlist_page():
    return render_template("wishlist.html")


@main_bp.route("/admin")
def admin_page():
    return render_template("admin.html")


@api_bp.post("/register")
def register():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not name or not email or not password:
        return _json_error("Name, email, and password are required.")
    if len(password) < 6:
        return _json_error("Password must be at least 6 characters.")
    if User.query.filter_by(email=email).first():
        return _json_error("Email is already registered.")

    user = User(name=name, email=email, role="user")
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    token = create_access_token(identity=str(user.id))
    return jsonify({"access_token": token, "user": user.to_dict()}), 201


@api_bp.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return _json_error("Email and password are required.")

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return _json_error("Invalid email or password.", 401)

    token = create_access_token(identity=str(user.id))
    return jsonify({"access_token": token, "user": user.to_dict()})


@api_bp.get("/products")
def list_products():
    query = Product.query
    category = (request.args.get("category") or "").strip()
    min_price = request.args.get("min_price")
    max_price = request.args.get("max_price")
    search = (request.args.get("search") or "").strip()

    if category:
        query = query.filter(Product.category == category)
    if min_price not in (None, ""):
        try:
            query = query.filter(Product.price >= float(min_price))
        except ValueError:
            return _json_error("min_price must be a number.")
    if max_price not in (None, ""):
        try:
            query = query.filter(Product.price <= float(max_price))
        except ValueError:
            return _json_error("max_price must be a number.")
    if search:
        like = f"%{search}%"
        query = query.filter(or_(Product.title.ilike(like), Product.author.ilike(like)))

    products = query.order_by(Product.id.asc()).all()
    return jsonify([p.to_dict() for p in products])


@api_bp.get("/products/<int:product_id>")
def get_product(product_id: int):
    product = db.session.get(Product, product_id)
    if not product:
        return _json_error("Product not found.", 404)
    return jsonify(product.to_dict())


@api_bp.get("/cart")
@jwt_required()
def get_cart():
    user = _current_user()
    if not user:
        return _json_error("Unauthorized", 401)
    return jsonify(_cart_payload(user.id))


@api_bp.post("/cart/add")
@jwt_required()
def add_to_cart():
    user = _current_user()
    if not user:
        return _json_error("Unauthorized", 401)

    data = request.get_json(silent=True) or {}
    product_id = data.get("product_id")
    quantity = data.get("quantity", 1)
    try:
        product_id = int(product_id)
        quantity = max(1, int(quantity))
    except (TypeError, ValueError):
        return _json_error("product_id and quantity must be valid numbers.")

    product = db.session.get(Product, product_id)
    if not product:
        return _json_error("Product not found.", 404)

    line = Cart.query.filter_by(user_id=user.id, product_id=product_id).first()
    if line:
        line.quantity += quantity
    else:
        db.session.add(Cart(user_id=user.id, product_id=product_id, quantity=quantity))
    db.session.commit()
    return jsonify(_cart_payload(user.id))


@api_bp.post("/cart/remove")
@jwt_required()
def remove_from_cart():
    user = _current_user()
    if not user:
        return _json_error("Unauthorized", 401)

    data = request.get_json(silent=True) or {}
    product_id = data.get("product_id")
    try:
        product_id = int(product_id)
    except (TypeError, ValueError):
        return _json_error("product_id must be a valid number.")

    line = Cart.query.filter_by(user_id=user.id, product_id=product_id).first()
    if line:
        db.session.delete(line)
        db.session.commit()
    return jsonify(_cart_payload(user.id))


@api_bp.post("/wishlist/add")
@jwt_required()
def wishlist_add():
    user = _current_user()
    if not user:
        return _json_error("Unauthorized", 401)

    data = request.get_json(silent=True) or {}
    product_id = data.get("product_id")
    try:
        product_id = int(product_id)
    except (TypeError, ValueError):
        return _json_error("product_id must be a valid number.")

    if not db.session.get(Product, product_id):
        return _json_error("Product not found.", 404)
    exists = Wishlist.query.filter_by(user_id=user.id, product_id=product_id).first()
    if not exists:
        db.session.add(Wishlist(user_id=user.id, product_id=product_id))
        db.session.commit()
    return wishlist_list()


@api_bp.post("/wishlist/remove")
@jwt_required()
def wishlist_remove():
    user = _current_user()
    if not user:
        return _json_error("Unauthorized", 401)

    data = request.get_json(silent=True) or {}
    product_id = data.get("product_id")
    try:
        product_id = int(product_id)
    except (TypeError, ValueError):
        return _json_error("product_id must be a valid number.")

    entry = Wishlist.query.filter_by(user_id=user.id, product_id=product_id).first()
    if entry:
        db.session.delete(entry)
        db.session.commit()
    return wishlist_list()


@api_bp.get("/wishlist")
@jwt_required()
def wishlist_list():
    user = _current_user()
    if not user:
        return _json_error("Unauthorized", 401)
    entries = Wishlist.query.filter_by(user_id=user.id).all()
    products = [entry.product.to_dict() for entry in entries if entry.product]
    return jsonify(products)


@api_bp.post("/order")
@jwt_required()
def place_order():
    user = _current_user()
    if not user:
        return _json_error("Unauthorized", 401)

    lines = Cart.query.filter_by(user_id=user.id).all()
    if not lines:
        return _json_error("Your cart is empty.")

    total = 0.0
    order = Order(user_id=user.id, total_price=0.0, status="paid (UPI mock)")
    db.session.add(order)
    db.session.flush()

    for line in lines:
        if not line.product:
            continue
        unit_price = line.product.price
        total += unit_price * line.quantity
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=line.product_id,
                quantity=line.quantity,
                unit_price=unit_price,
            )
        )
        db.session.delete(line)

    order.total_price = round(total, 2)
    db.session.commit()
    return jsonify(
        {
            "message": "Payment successful via UPI mock.",
            "order_id": order.id,
            "total": order.total_price,
        }
    )


@api_bp.get("/admin/products")
@jwt_required()
def admin_products():
    _, error = _admin_required()
    if error:
        return error
    rows = Product.query.order_by(Product.id.desc()).all()
    return jsonify([p.to_dict() for p in rows])


@api_bp.post("/admin/products")
@jwt_required()
def admin_add_product():
    _, error = _admin_required()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    required = ["title", "author", "price", "category", "image"]
    if any(not str(data.get(field, "")).strip() for field in required):
        return _json_error("title, author, price, category, and image are required.")

    try:
        product = Product(
            title=str(data["title"]).strip(),
            author=str(data["author"]).strip(),
            price=float(data["price"]),
            category=str(data["category"]).strip(),
            image=str(data["image"]).strip(),
            rating=float(data.get("rating", 4.5)),
            description=str(data.get("description", "")).strip(),
        )
    except (TypeError, ValueError):
        return _json_error("Invalid product payload.")

    db.session.add(product)
    db.session.commit()
    return jsonify(product.to_dict()), 201


@api_bp.delete("/admin/products/<int:product_id>")
@jwt_required()
def admin_delete_product(product_id: int):
    _, error = _admin_required()
    if error:
        return error

    product = db.session.get(Product, product_id)
    if not product:
        return _json_error("Product not found.", 404)

    Cart.query.filter_by(product_id=product_id).delete()
    Wishlist.query.filter_by(product_id=product_id).delete()
    OrderItem.query.filter_by(product_id=product_id).delete()
    db.session.delete(product)
    db.session.commit()
    return jsonify({"message": "Product deleted."})
