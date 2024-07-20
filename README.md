# What is this?

`redir-server` provides a web interface to inspect and redirect the server's attached USB devices. This allows for inspection of the devices' descriptors and running [`usbredirect`](https://www.spice-space.org/usbredir.html) to allow for a qemu on a different machine to pass through the USB device (from the server) as if it were attached to that machine.

# Setup

For the server keys configuration, create a `data/config.toml`:

```toml
GITHUB_CLIENT_ID = '<your GitHub OAuth2 client ID>'
GITHUB_CLIENT_SECRET = '<your GitHub OAuth2 client secret>'
DATABASE_URI = 'sqlite:///<path to the sqlite db>'
TEMPLATES_AUTO_RELOAD = true

SECRET_KEY = '<your secret app key>'
```

For the configuration of other settings, create a `data/config.yml`:

```yaml
usb:
  ignored:
  - vendor: "1d6b"
```

To set up the venv, install the dependencies and to run the development server instance, do:

```sh
pipenv install
pipenv run flask run --reload --host 0.0.0.0
```
