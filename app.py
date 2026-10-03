"""Crossword — Flask bookstore application."""
import json
import os
import socket
import sqlite3

from flask import Flask
from flask_cors import CORS

from models import Product, User, db
from routes import api_bp, main_bp


def create_app():
    app = Flask(__name__)
    project_root = os.path.dirname(os.path.abspath(__file__))
    db_file_path = os.path.join(project_root, "database.db")
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-chapterone-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", f"sqlite:///{db_file_path}"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    with app.app_context():
        db.create_all()
        ensure_checkout_orders_table(db_file_path)
        seed_if_empty()

    return app


def seed_if_empty():
    books_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "books.json")
    books = []
    if os.path.exists(books_path):
        with open(books_path, "r", encoding="utf-8") as f:
            books = json.load(f)
    existing_titles = {title for (title,) in Product.query.with_entities(Product.title).all()}
    for b in books:
        if b.get("title") not in existing_titles:
            db.session.add(Product(**b))

    admin_email = "admin@crossword.in"
    if not User.query.filter_by(email=admin_email).first():
        admin = User(name="Store Admin", email=admin_email, role="admin")
        admin.set_password("admin123")
        db.session.add(admin)

    db.session.commit()


def ensure_checkout_orders_table(db_file_path: str):
    """Create simple checkout orders table if missing."""
    conn = sqlite3.connect(db_file_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders_checkout (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT NOT NULL,
                items TEXT NOT NULL,
                total_price REAL NOT NULL,
                payment_method TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


app = create_app()

def _can_bind(host: str, port: int) -> bool:
    """Return True if Werkzeug can bind to host:port (same rules as the dev server)."""
    from werkzeug.serving import get_sockaddr, select_address_family

    family = select_address_family(host, port)
    try:
        sockaddr = get_sockaddr(host, int(port), family)
    except OSError:
        return False
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(sockaddr)
    except OSError:
        return False
    return True


def get_run_port(
    host: str,
    preferred: int = 5000,
    fallback_start: int = 5001,
    fallback_end: int = 5010,
) -> int:
    """Use preferred port, otherwise first available fallback for this host."""
    if _can_bind(host, preferred):
        return preferred
    for port in range(fallback_start, fallback_end + 1):
        if _can_bind(host, port):
            return port
    return preferred


if __name__ == "__main__":
    run_host = os.environ.get("HOST", "127.0.0.1")
    run_port = 5000
    display_host = run_host if ":" not in run_host else f"[{run_host}]"
    print(f"Starting Flask server at http://{display_host}:{run_port}")
    app.run(debug=True, host=run_host, port=run_port)