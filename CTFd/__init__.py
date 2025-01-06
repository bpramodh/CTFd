import datetime
import os
import sys
import time
import weakref
from distutils.version import StrictVersion

import jinja2
from flask import Flask, Request, render_template
from flask_babel import Babel
from flask_migrate import upgrade
from jinja2.sandbox import SandboxedEnvironment
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import safe_join

from CTFd import utils
from CTFd.constants.themes import ADMIN_THEME, DEFAULT_THEME
from CTFd.plugins import init_plugins
from CTFd.utils.crypto import sha256
from CTFd.utils.initialization import (
    init_cli,
    init_events,
    init_logs,
    init_request_processors,
    init_template_filters,
    init_template_globals,
)
from CTFd.utils.migrations import create_database, migrations, stamp_latest_revision
from CTFd.utils.sessions import CachingSessionInterface
from CTFd.utils.updates import update_check
from CTFd.utils.user import get_locale

__version__ = "3.7.5"
__channel__ = "oss"


class CTFdRequest(Request):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.path = self.script_root + self.path


class CTFdFlask(Flask):
    def __init__(self, *args, **kwargs):
        self.jinja_environment = SandboxedBaseEnvironment
        self.session_interface = CachingSessionInterface(key_prefix="session")
        self.request_class = CTFdRequest
        self.start_time = datetime.datetime.utcnow()
        self.run_id = sha256(str(self.start_time))[0:8]
        Flask.__init__(self, *args, **kwargs)


class SandboxedBaseEnvironment(SandboxedEnvironment):
    def __init__(self, app, **options):
        if "loader" not in options:
            options["loader"] = app.create_global_jinja_loader()
        if "finalize" not in options:
            options["finalize"] = lambda x: x if x is not None else ""
        SandboxedEnvironment.__init__(self, **options)
        self.app = app

    def _load_template(self, name, globals):
        if self.loader is None:
            raise TypeError("no loader for this environment specified")

        cache_name = name
        if name.startswith("admin/") is False:
            theme = str(utils.get_config("ctf_theme"))
            cache_name = theme + "/" + name

        cache_key = (weakref.ref(self.loader), cache_name)
        if self.cache is not None:
            template = self.cache.get(cache_key)
            if template is not None and (
                not self.auto_reload or template.is_up_to_date
            ):
                if globals:
                    template.globals.update(globals)
                return template

        template = self.loader.load(self, name, self.make_globals(globals))

        if self.cache is not None:
            self.cache[cache_key] = template
        return template


def create_app(config="CTFd.config.Config"):
    app = CTFdFlask(__name__)
    with app.app_context():
        app.config.from_object(config)

        from CTFd.cache import cache
        from CTFd.utils import import_in_progress

        cache.init_app(app)
        app.cache = cache

        while import_in_progress():
            print("Import in progress, pausing startup...")
            time.sleep(5)

        loaders = [
            jinja2.DictLoader(app.overridden_templates),
            ThemeLoader(),
            jinja2.PrefixLoader({"plugins": jinja2.FileSystemLoader(os.path.join(app.root_path, "plugins"))}),
        ]
        app.jinja_loader = jinja2.ChoiceLoader(loaders)

        from CTFd.models import db
        url = create_database()
        app.config["SQLALCHEMY_DATABASE_URI"] = str(url)
        db.init_app(app)
        migrations.init_app(app, db)

        babel = Babel()
        babel.locale_selector_func = get_locale
        babel.init_app(app)

        if url.drivername.startswith("sqlite"):
            db.create_all()
            stamp_latest_revision()
        else:
            upgrade()

        from CTFd.models import ma
        ma.init_app(app)

        app.db = db
        app.VERSION = __version__
        app.CHANNEL = __channel__

        reverse_proxy = app.config.get("REVERSE_PROXY")
        if reverse_proxy:
            proxyfix_args = [int(i) for i in reverse_proxy.split(",")] if isinstance(reverse_proxy, str) else None
            app.wsgi_app = ProxyFix(app.wsgi_app, *proxyfix_args)

        version = utils.get_config("ctf_version")
        if version and StrictVersion(version) < StrictVersion(__version__):
            run_upgrade()
        elif not version:
            utils.set_config("ctf_version", __version__)

        if not utils.get_config("ctf_theme"):
            utils.set_config("ctf_theme", "core-beta")

        update_check(force=True)

        init_request_processors(app)
        init_template_filters(app)
        init_template_globals(app)
        init_logs(app)
        init_events(app)
        init_plugins(app)
        init_cli(app)

        # Custom route
        @app.route('/')
        def index():
            return render_template('odu_landing.html')

    return app
