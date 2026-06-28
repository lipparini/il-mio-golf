import os
from flask import Flask
from flask_login import LoginManager

login_manager = LoginManager()


def create_app():
    app = Flask(__name__, template_folder="../templates")
    app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Effettua il login per accedere."
    login_manager.login_message_category = "info"

    from .models import init_db
    init_db()

    from .auth import auth_bp
    from .routes import main_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    from .scheduler import start_scheduler
    start_scheduler(app)

    return app
