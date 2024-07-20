import psutil
import shutil
import signal
import subprocess
import tomllib
import usb
import yaml

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    render_template_string,
    request,
    session,
    url_for,
)
from flask_github import GitHub
from sortedcontainers import SortedSet
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker

app = Flask(__name__, static_folder=None)
app.config.from_file("data/config.toml", load=tomllib.load, text=False)

github = GitHub(app)

engine = create_engine(app.config["DATABASE_URI"])
db_session = scoped_session(
    sessionmaker(autocommit=False, autoflush=False, bind=engine)
)
Base = declarative_base()
Base.query = db_session.query_property()


def sigchld_handler(signum, frame):
    # we received a SIGCHLD, but do not get info about which child;
    # therefore, we loop our list of children and dispose of them if they're dead
    for proc in active_redirs:
        if proc.proc.poll() is not None:
            proc.start()
    pass


signal.signal(signal.SIGCHLD, sigchld_handler)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    github_access_token = Column(String(255))
    github_id = Column(Integer)
    github_login = Column(String(255))
    github_avatar = Column(String(255))

    def __init__(self, github_access_token):
        self.github_access_token = github_access_token


def init_db():
    Base.metadata.create_all(bind=engine)
    User.metadata.create_all(engine)


@app.before_request
def before_request():
    g.user = None
    if "user_id" in session:
        g.user = User.query.get(session["user_id"])


@app.after_request
def after_request(response):
    db_session.remove()
    return response


with open("data/config.yml", "r") as f:
    yml = yaml.load(f, yaml.SafeLoader)


def device_ignored(vendor, product):
    for candidate in yml["usb"]["ignored"]:
        matched = None
        if "vendor" in candidate:
            matched = ((matched if matched else True) and int(candidate["vendor"], 16) == vendor)
        if "product" in candidate:
            matched = ((matched if matched else True) and int(candidate["product"], 16) == product)
        if matched:
            return True
    return False


class Navigation:
    def __init__(self, text, url=None):
        self.text_ = text
        self.url_ = url

    @property
    def text(self):
        return self.text_

    @property
    def url(self):
        return self.url_


@app.context_processor
def pass_default_data():
    return dict(user=g.user)


@app.route("/")
def home():
    if g.user:
        devices = []

        for d in usb.core.find(find_all=True):
            if not device_ignored(d.idVendor, d.idProduct):
                dev = DeviceRedirection.find(d)
                d.is_redirected = dev is not None
                if d.is_redirected:
                    d.redirection_port = dev.port
                devices.append(d)

        # make sure we have a stable order of devices
        devices.sort(key=lambda x: (x.idVendor, x.idProduct, x.bus, x.port_number))

        navigation = [Navigation("Home")]

        return render_template("home.html", navigation=navigation, devices=devices)
    else:
        return render_template("login.html", err=request.args.get("err"))


@github.access_token_getter
def token_getter():
    user = g.user
    if user is not None:
        return user.github_access_token


@app.route("/gh-callback")
@github.authorized_handler
def authorized(access_token):
    next_url = request.args.get("next") or url_for("home")
    if access_token is None:
        return redirect(next_url)

    user = User.query.filter_by(github_access_token=access_token).first()
    if user is None:
        user = User(access_token)
        db_session.add(user)

    user.github_access_token = access_token

    g.user = user
    github_user = github.get("/user")
    org_list = [org["login"] for org in github.get(github_user["organizations_url"])]

    if "managarm" not in org_list:
        g.user = None
        db_session.rollback()
        return redirect(url_for("home", err="not_whitelisted"))

    user.github_id = github_user["id"]
    user.github_login = github_user["login"]
    user.github_avatar = github_user["avatar_url"]

    db_session.commit()

    session["user_id"] = user.id
    return redirect(next_url)


@app.route("/login")
def login():
    if session.get("user_id", None) is None:
        return github.authorize()
    else:
        return "Already logged in"


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("home"))


class DeviceRedirection:
    def __init__(self, d):
        self.d = d
        self.port = available_ports.pop(0)
        self.start()

    def start(self):
        self.proc = subprocess.Popen(
            [
                shutil.which("usbredirect"),
                "--device",
                f"{self.d.idVendor:04x}:{self.d.idProduct:04x}",
                "--as",
                f"0.0.0.0:{self.port}",
            ]
        )
        print(f"redirecting device {self.d.idVendor:04x}:{self.d.idProduct:04x} on port {self.port}")

    def stop(self):
        self.proc.terminate()
        print(
            f"stopping device redirection of {self.d.idVendor:04x}:{self.d.idProduct:04x} on port {self.port}"
        )
        self.dispose()

    def dispose(self):
        available_ports.add(self.port)
        active_redirs.remove(self)

    def find(d):
        return next(
            filter(
                lambda m: d.bus == m.d.bus
                and d.port_number == m.d.port_number
                and d.idVendor == m.d.idVendor
                and d.idProduct == m.d.idProduct,
                active_redirs,
            ),
            None,
        )

    def exists(d):
        l = [
            m.d
            for m in filter(
                lambda m: d.bus == m.d.bus
                and d.port_number == m.d.port_number
                and d.idVendor == m.d.idVendor
                and d.idProduct == m.d.idProduct,
                active_redirs,
            )
        ]
        return len(l) != 0


active_redirs = []
available_ports = SortedSet(range(42069, 42069 + 0x100))


@app.route("/device/<bus>/<device>")
def list_device(bus, device):
    try:
        with open(f"/sys/bus/usb/devices/usb{bus}/{bus}-{device}/devnum") as f:
            devnum = f.readline().strip()
    except FileNotFoundError:
        abort(404)

    with open(f"/sys/bus/usb/devices/usb{bus}/{bus}-{device}/idVendor") as f:
        vendor = int(f.readline().strip(), 16)
    with open(f"/sys/bus/usb/devices/usb{bus}/{bus}-{device}/idProduct") as f:
        product = int(f.readline().strip(), 16)
    with open(f"/sys/bus/usb/devices/usb{bus}/{bus}-{device}/manufacturer") as f:
        manufacturer = f.readline().strip()
    with open(f"/sys/bus/usb/devices/usb{bus}/{bus}-{device}/product") as f:
        devname = f.readline().strip()

    if device_ignored(vendor, product):
        abort(404)

    cmd = [
        shutil.which("lsusb"),
        "-D",
        f"/dev/bus/usb/{int(bus):03d}/{int(devnum):03d}",
    ]
    output = subprocess.run(cmd, capture_output=True)
    if output.returncode != 0:
        abort(404)

    navigation = [
        Navigation("Home", url=url_for("home")),
        Navigation("Device Info"),
    ]

    return render_template(
        "lsusb-dump.html",
        navigation=navigation,
        vendor=vendor,
        product=product,
        name=f"{manufacturer} {devname}",
        lsusb_output=output.stdout.decode("utf-8"),
    )


@app.post("/device/<bus>/<device>/redir")
def redir_device(bus, device):
    d = usb.core.find(
        custom_match=lambda d: int(d.bus) == int(bus)
        and int(d.port_number) == int(device)
    )
    if DeviceRedirection.exists(d):
        abort(503)
    redir = DeviceRedirection(d)
    active_redirs.append(redir)
    return str(redir.port)


@app.post("/device/<bus>/<device>/redir-stop")
def redir_device_stop(bus, device):
    d = usb.core.find(
        custom_match=lambda d: int(d.bus) == int(bus)
        and int(d.port_number) == int(device)
    )
    redir = DeviceRedirection.find(d)

    if not redir:
        abort(503)

    redir.stop()

    return ""

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
