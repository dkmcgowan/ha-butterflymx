# ButterflyMX for Home Assistant

[![hacs][hacs-badge]][hacs-url]
[![Validate][validate-badge]][validate-url]

A [HACS][hacs-url] custom integration that connects Home Assistant to
[ButterflyMX](https://butterflymx.com) intercoms and access points over the
ButterflyMX cloud API.

It gives you:

- **Locks.** Every access point and unit smart lock your account can open,
  exposed as standard Home Assistant `lock` entities. `lock.open` and
  `lock.unlock` both buzz the door.
- **Doorbell events.** An `event` entity with the `doorbell` device class that
  fires when a visitor calls your unit, carrying the calling station, the
  notification type and the snapshot URL.
- **Snapshot image.** An `image` entity holding the still captured by the
  intercom on the most recent call.
- **Last call sensor.** A timestamp sensor with the details of the last call.

> This is an unofficial community integration. It is not built or supported by
> ButterflyMX.
>
> Parts of this project were written with the help of Claude Code. Everything
> here has been reviewed before being committed.

## Requirements

You need ButterflyMX API credentials (a **client ID** and **client secret**).
ButterflyMX issues these through their developer program, so start at
<https://apidocs.butterflymx.com/docs/getting-started>. Sandbox credentials
arrive by email after you sign their developer terms. Production credentials are
requested separately.

The account you sign in with during setup must be a **resident/tenant** of the
building whose doors you want to control. The integration only ever acts as that
tenant.

## Installation

### HACS (recommended)

1. In Home Assistant, go to **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/dkmcgowan/ha-butterflymx` with category
   **Integration**.
3. Install **ButterflyMX**, then restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration** and search for
   **ButterflyMX**.

### Manual

Copy `custom_components/butterflymx` into your Home Assistant `config/custom_components`
directory and restart.

## Setup

The config flow walks through four short steps.

1. **Environment.** Production, Sandbox, or a custom ButterflyMX deployment.

   | Environment | OAuth host | API host |
   | --- | --- | --- |
   | Production | `https://accounts.butterflymx.com` | `https://api.butterflymx.com` |
   | Sandbox | `https://accounts.na.sandbox.butterflymx.com` | `https://api.na.sandbox.butterflymx.com` |

2. **Credentials.** Your client ID and secret. Leave **Redirect URI** at the
   default `urn:ietf:wg:oauth:2.0:oob` unless ButterflyMX registered a custom
   one for your application.

3. **Authorize.** Home Assistant shows a ButterflyMX sign-in link. Open it, sign
   in, and approve access.

4. **Paste the code.** With the default redirect URI, ButterflyMX displays an
   authorization code in the browser. Paste it back into Home Assistant. If you
   have a custom redirect URI you will be sent to that URL instead, in which case
   paste the whole URL and the integration will pull the `code` out of it.

Authorization codes are single-use and short-lived, so do not leave the browser
sitting on the code for long.

### Why paste a code instead of a normal OAuth redirect?

ButterflyMX's documented default redirect URI is the out-of-band value
`urn:ietf:wg:oauth:2.0:oob`, which displays the code instead of redirecting.
Home Assistant's built-in OAuth helper always redirects, so this integration
performs the token exchange itself. That keeps setup working with the standard
developer credentials, and still supports a registered redirect URI if you have
one.

## Tokens and re-authorization

ButterflyMX access tokens are valid for **24 hours**. Refresh tokens do not
expire. The integration refreshes the access token automatically a few minutes
before it lapses and writes the rotated token pair back to the config entry.
ButterflyMX issues a new refresh token on every refresh, so both halves are
stored.

If ButterflyMX ever rejects the refresh token, whether because the application
was revoked, the password changed or the tenancy ended, Home Assistant raises a
repair notification and starts a re-authentication flow. You link the account
again, and entities and their history are preserved.

## Options

**Settings → Devices & services → ButterflyMX → Configure**

| Option | Default | Notes |
| --- | --- | --- |
| Call polling interval | 10 s | How often the call log is checked for new visitor calls. Minimum 5 s. |
| Show door as unlocked for | 5 s | How long a lock entity reports `unlocked` after a successful release. Cosmetic only; it does not change how long the door stays open. |
| Webhook push (experimental) | off | See below. |

### Rate limiting

ButterflyMX publishes no rate limits, so the integration errs on the
conservative side:

- at most 4 requests in flight, spaced at least 250 ms apart;
- exponential backoff with jitter on `429` and `5xx`, honouring `Retry-After`;
- topology (buildings, access points, devices) refreshed only once an hour;
- **door releases are never retried automatically**, since a retry could buzz a
  door twice, and repeated releases of the same door within 3 seconds are
  dropped.

If you have many buildings on one account, raise the call polling interval.

### Webhook push (experimental)

If Home Assistant is reachable from the internet, ButterflyMX can push call
events to it instead of the integration polling. Enabling the option registers a
Home Assistant webhook and a matching ButterflyMX tenant integration, and removes
them when the option is turned off or the entry is unloaded.

It is marked experimental because ButterflyMX documents the registration
endpoints and the general contents of the payload but not an exact schema. The
parser is forgiving and logs anything it does not recognise at debug level. If
push does not fire for you, please open an issue with a debug log. Polling keeps
running regardless.

## Automation example

```yaml
automation:
  - alias: "Announce and show ButterflyMX visitor"
    triggers:
      - trigger: state
        entity_id: event.unit_4b_doorbell
    actions:
      - action: notify.mobile_app_pixel
        data:
          title: "Someone is at {{ trigger.to_state.attributes.device_name }}"
          message: "{{ trigger.to_state.attributes.notification_type }}"
          data:
            image: "{{ trigger.to_state.attributes.image_url }}"
```

An event is also fired on the Home Assistant bus as `butterflymx_call` with the
same data, if you prefer an event trigger.

To open a door:

```yaml
actions:
  - action: lock.open
    target:
      entity_id: lock.front_entrance
```

## What this integration does not do

- **User management.** Adding, removing or editing tenants, units, access groups
  and PINs is out of scope by design. This integration only reads the topology it
  needs and opens doors.
- **Live video or two-way audio.** The ButterflyMX REST API does not expose a
  stream. Real-time video and intercom audio are only available through
  ButterflyMX's proprietary iOS and Android SDKs, which cannot run inside Home
  Assistant. The still snapshot from the call log is the closest available
  substitute, which is why calls surface as an `image` entity rather than a
  `camera`.
- **Third-party camera feeds.** The v4 API surfaces no camera or stream
  endpoints, so there is nothing to expose.
- **Temporary passcodes and virtual keys.** The API supports them
  (`/v4/keychains`, `/v4/virtual_keys`) and they may be added later, but they are
  not implemented yet.

## Troubleshooting

Enable debug logging:

```yaml
logger:
  default: warning
  logs:
    custom_components.butterflymx: debug
```

Diagnostics can be downloaded from the integration page. Credentials, tokens and
personal details are redacted.

## Development

### Unit tests

```bash
pip install -r requirements-dev.txt
ruff check custom_components tests scripts
pytest
```

The suite runs against the Home Assistant test harness and needs Linux or macOS.
Home Assistant imports `fcntl`, so it cannot run on Windows. CI runs it on every
push.

### Probing the real API

`scripts/probe_api.py` is a standalone tool that walks the OAuth flow once and
reports what a live account actually returns: which endpoints a resident is
allowed to call, what values appear in call payloads, and whether snapshot URLs
are pre-signed. It uses only the standard library, so no virtualenv is needed.
Use it to check assumptions before deploying, and to capture real payloads for
test fixtures.

```bash
export BMX_CLIENT_ID=... BMX_CLIENT_SECRET=...
python scripts/probe_api.py --env sandbox
```

It caches the token in `scripts/.bmx-token-<env>.json` and writes a redacted
transcript to `scripts/probe-output-<env>.json`. Both are gitignored. Names,
emails, serial numbers and URL signatures are stripped before anything is
written, so the transcript is safe to attach to an issue.

### Deploying to a Home Assistant instance

`scripts/deploy.sh` copies the integration over SSH and restarts Home Assistant
Core. It needs the **Advanced SSH & Web Terminal** add-on with key
authentication. It uses only `ssh` and `tar`, so it works from Git Bash on
Windows too.

```bash
echo 'HAOS_HOST=homeassistant.local' > scripts/.env.deploy
scripts/deploy.sh              # copy and restart
scripts/deploy.sh --logs       # copy, restart, then follow the log
scripts/deploy.sh --no-restart # copy only
```

A restart is required for Python changes, because reloading a config entry
re-runs setup but does not re-import changed modules.

Test against **sandbox credentials first**. Door releases are real, and a mistake
while pointed at production buzzes an actual door.

Issues and pull requests are welcome.

## License

[MIT](LICENSE)

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://hacs.xyz
[validate-badge]: https://github.com/dkmcgowan/ha-butterflymx/actions/workflows/validate.yml/badge.svg
[validate-url]: https://github.com/dkmcgowan/ha-butterflymx/actions/workflows/validate.yml
