from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user
from . import login_manager
from .models import check_password, load_user_by_id

auth_bp = Blueprint("auth", __name__)


@login_manager.user_loader
def load_user(user_id):
    try:
        return load_user_by_id(int(user_id))
    except Exception:
        return None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.menu"))

    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = check_password(email, password)
        if user:
            login_user(user, remember=True)
            next_page = request.args.get("next")
            return redirect(next_page or url_for("main.menu"))
        error = "Email o password non validi."

    return render_template("login.html", error=error)


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
