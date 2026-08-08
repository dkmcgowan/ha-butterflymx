# ButterflyMX for Home Assistant

[![hacs][hacs-badge]][hacs-url]
[![Validate][validate-badge]][validate-url]

Use your [ButterflyMX](https://butterflymx.com) intercom from Home Assistant.

Unlock the front door from a dashboard, your phone, a wall tablet or your watch.
Get a notification the moment a visitor buzzes your unit, with a photo of who is
standing there. Issue a code for a delivery or a cleaner without opening the app.
Build automations around all of it, like turning the hall light on when someone
is let in after dark.

> **Unofficial.** This is a community project. It is not built, endorsed or
> supported by ButterflyMX.
>
> Parts of it were written with the help of Claude Code. Every file has since
> been read and reviewed line by line, and every request it makes has been run
> against a real ButterflyMX account: signing in, listing doors, reading the
> call log, receiving a pushed call, opening a door, and issuing and revoking
> visitor and delivery passes.

## What you get

Once set up, these appear in Home Assistant automatically. You do not configure
them by hand.

| Entity | What it is |
| --- | --- |
| `lock.front_entrance` | One for every door you can already open in the ButterflyMX app. Press to buzz it open. |
| `event.unit_4b_doorbell` | Fires the instant a visitor calls your unit. Use it to trigger notifications. |
| `image.unit_4b_last_call_snapshot` | The photo the intercom took of your most recent visitor. |
| `sensor.unit_4b_last_call` | When the last call happened, and who or what it came from. |
| `event.unit_4b_door_opened` | Fires whenever one of your doors is opened, however it happened: a PIN at the keypad, a fob, someone answering the intercom in the app, or Home Assistant. |
| `sensor.unit_4b_last_door_opened` | When a door was last opened, which one, and by what method. |
| `sensor.unit_4b_passes` | How many visitor and delivery codes are currently valid, and what they are for. See [Visitor and delivery passes](#visitor-and-delivery-passes). |

Names will match your own building and unit.

The door entities only ever cover your own tenancy. ButterflyMX scopes the
access log to the signed-in resident, so you see your own comings and goings and
not the neighbours'. That applies to whoever else lives with you as well — if
you each have your own ButterflyMX login, see
[Two residents, two logins](#two-residents-two-logins).

## Before you start

You need two things:

1. **A ButterflyMX account** that is a resident of the building, the same login
   you use in their app. The integration only ever acts as you, and can only
   open doors you can already open.
2. **Home Assistant 2026.3 or newer.**

**And possibly a third: a ButterflyMX client ID.** Whether you need one depends
on this copy of the integration. If it ships with a client ID, setup just asks
you to sign in and you can skip the rest of this section. If it does not, setup
asks you for one, and you have to request it from ButterflyMX's developer
programme at <https://apidocs.butterflymx.com/docs/getting-started>.

Either way you sign in with your own account and the integration only ever holds
your own access. A client ID is not a password and is not treated as one: it
identifies the application, not you, and ButterflyMX issues it as a public
client, so there is no secret to go with it. If you were given a client secret
as well, there is a field for it, but most credentials do not have one.

## Install

### Through HACS

1. In Home Assistant, open **HACS → ⋮ → Custom repositories**.
2. Paste `https://github.com/dkmcgowan/ha-butterflymx` and choose category
   **Integration**.
3. Install **ButterflyMX**, then restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration** and search for
   **ButterflyMX**.

### By hand

Copy the `custom_components/butterflymx` folder into your Home Assistant
`config/custom_components` folder and restart.

## Set it up

Two short steps, or three if you are supplying your own client ID.

1. **Enter your client ID**, if you are asked for one. Leave the client secret
   empty unless ButterflyMX gave you one, and leave the redirect URI alone
   unless they registered a custom one. If setup goes straight to signing in,
   this copy already has a client ID and there is nothing to enter.

2. **Sign in to ButterflyMX.** Home Assistant shows you a link. Open it, sign in
   with your normal ButterflyMX account, and approve access. This happens on
   ButterflyMX's own site, so your password is never typed into Home Assistant.

3. **Paste the code back.** ButterflyMX shows you a short authorization code.
   Copy it into Home Assistant and you are done. If you were sent to a web
   address instead of shown a code, paste the whole address and the integration
   will pick the code out of it.

The code expires quickly, so do not leave it sitting in the browser.

Your doors, doorbell and snapshot appear straight away.

### Why the copy-and-paste step?

Most integrations bounce you back to Home Assistant automatically after signing
in. ButterflyMX's standard setup shows you a code on screen instead of
redirecting anywhere, so there is nothing for Home Assistant to catch. Pasting
the code does the same job. If ButterflyMX has set up a redirect address for you,
that works too, and you can paste the whole address instead.

### Staying signed in

You sign in once. The integration keeps your access current on its own, and it
does not store your password at any point.

If it ever loses access, because you changed your password or moved out, Home
Assistant shows a **Reconfigure** prompt and you sign in again. Your entities,
automations and history survive that.

## Things to do with it

**See who is at the door and let them in from the notification:**

```yaml
automation:
  - alias: "Someone is at the door"
    mode: restart
    variables:
      # The door the "Let them in" button opens.
      door: lock.front_entrance
    triggers:
      - trigger: state
        entity_id: event.unit_4b_doorbell
    actions:
      - action: notify.mobile_app_my_phone
        data:
          title: "Someone is at {{ trigger.to_state.attributes.device_name }}"
          message: "Tap to see who it is"
          data:
            image: "{{ trigger.to_state.attributes.image_url }}"
            actions:
              - action: BMX_OPEN
                title: "Let them in"
              - action: BMX_IGNORE
                title: "Ignore"
      - wait_for_trigger:
          - trigger: event
            event_type: mobile_app_notification_action
            event_data:
              action: BMX_OPEN
        timeout: "00:02:00"
        continue_on_timeout: true
      # Nothing tapped, or "Ignore" tapped: stop here without opening.
      - condition: template
        value_template: "{{ wait.trigger is not none }}"
      - action: lock.open
        target:
          entity_id: "{{ door }}"
```

Change `door:` at the top to whichever lock you want the button to open.
`mode: restart` means a second visitor replaces the first rather than being
dropped while the automation is still waiting.

**"Ignore" only dismisses the notification.** ButterflyMX has no endpoint for
answering or declining a call — the API can list calls and open doors, and that
is all — so nothing here can hang up on a visitor or stop their intercom from
ringing. They give up, or you answer in the ButterflyMX app. The button exists
so the notification has an obvious way out; letting the two-minute timeout
expire does exactly the same thing.

The photo needs no special setup: `image_url` points at a plain web address
your phone can load directly, unlike a camera snapshot that would need Home
Assistant to be reachable from outside.

**Open the door from anywhere:**

```yaml
actions:
  - action: lock.open
    target:
      entity_id: lock.front_entrance
```

Both `lock.open` and `lock.unlock` buzz the door. Add the lock to a dashboard, a
wall tablet by the door, or a voice assistant.

**Other ideas:** flash a light when someone buzzes while you have music on, log
every visitor to a calendar, or announce the door on a speaker so you hear it
away from your phone.

## Visitor and delivery passes

The same two things you can create in the ButterflyMX app, with the same names:

- A **delivery pass** is a single-use code. It opens every door, works once, and
  expires after 30 days. Good for a parcel that needs to get inside the lobby.
- A **visitor pass** is a reusable code valid over a window you choose, for
  whichever doors you choose. Good for a cleaner, a dog walker, or family
  staying the weekend.

Both come back as a six-digit PIN plus a QR code your visitor can scan.

### Where the codes live

**The PIN and QR link are returned to whatever called the action, and are not
stored in Home Assistant.** They are not in the sensor, not in its attributes,
and so not in your history database, your logbook or a diagnostics download. A
PIN opens your building's front door, and Home Assistant keeps state history for
weeks by default, which is no place to leave one.

You can always read a code back with `butterflymx.list_passes`, and the
ButterflyMX app shows every pass too. So nothing is lost — the code just has to
be asked for rather than sitting in a database.

### Creating a delivery code

```yaml
actions:
  - action: butterflymx.create_delivery_pass
    target:
      entity_id: sensor.unit_4b_passes
    data:
      name: "Amazon - Tuesday"
    response_variable: delivery
  - variables:
      pass: "{{ delivery['sensor.unit_4b_passes'] }}"
  - action: notify.mobile_app_my_phone
    data:
      title: "Delivery code"
      message: "PIN {{ pass.keys[0].pin_code }}"
```

The name is worth choosing well: it is what shows up in the ButterflyMX access
log when the code gets used.

The response is keyed by the entity you targeted, the same shape
`weather.get_forecasts` returns. With one unit there is only ever the one key,
which is why the example pulls it out into a variable first.

### Creating a visitor code

```yaml
actions:
  - action: butterflymx.create_visitor_pass
    target:
      entity_id: sensor.unit_4b_passes
    data:
      name: "Cleaner"
      starts_at: "2026-08-10 09:00:00"
      ends_at: "2026-08-10 13:00:00"
      doors:
        - lock.front_entrance
    response_variable: visitor
```

Everything but `name` is optional. Leave the times out and it starts now and
runs for four hours; leave `doors` out and it opens all of them.

Under `visitor['sensor.unit_4b_passes']` you get `pass_id`, the window, and a
`keys` list with `pin_code`, `qr_code_url` and `instructions_url` — the last
being a page you can send someone that explains how to use the code.

### Seeing and revoking passes

`sensor.unit_4b_passes` counts the passes valid right now. Its `passes`
attribute lists them all, expired ones included, with the ID you need to revoke
one and a `used` flag so you can tell whether a delivery code has been redeemed.

```yaml
# Read the codes back, including the PINs.
# all_passes['sensor.unit_4b_passes'].passes is the list.
actions:
  - action: butterflymx.list_passes
    target:
      entity_id: sensor.unit_4b_passes
    response_variable: all_passes
```

Run that one from **Developer tools → Actions** when you just want to look up a
code: the response appears right there on the page.

```yaml
# Revoke one. This deletes its codes too, and cannot be undone.
actions:
  - action: butterflymx.revoke_pass
    target:
      entity_id: sensor.unit_4b_passes
    data:
      pass_id: 44138578
```

**Tidy up expired passes automatically:**

```yaml
automation:
  - alias: "Remove finished ButterflyMX passes"
    triggers:
      - trigger: time
        at: "03:00:00"
    actions:
      - repeat:
          for_each: >
            {{ state_attr('sensor.unit_4b_passes', 'passes')
               | selectattr('ends_at', 'lt', now().isoformat())
               | map(attribute='pass_id') | list }}
          sequence:
            - action: butterflymx.revoke_pass
              target:
                entity_id: sensor.unit_4b_passes
              data:
                pass_id: "{{ repeat.item }}"
```

### One thing these deliberately do not do

ButterflyMX can email or text a code to a recipient for you when a pass is
created. That is not offered here. An action that mails a working door code to
whatever address it is handed is one typo away from letting a stranger into your
building, and an automation makes typos at three in the morning without anyone
watching. The response gives you the PIN and the QR link; send them yourself,
through a notify service you already trust.

## Two residents, two logins

Home Assistant runs integrations for the whole instance, not per user account.
Signing in to Home Assistant as yourself does not give you a different
ButterflyMX than anyone else in the house sees. What the integration is signed
in to ButterflyMX as, everybody gets.

And **one ButterflyMX login only ever sees its own calls.** ButterflyMX scopes
the call log to the signed-in resident, so if two of you have separate
ButterflyMX accounts, one setup covers exactly one of you. There is no
recipient field to branch on inside an automation, because every call your
account can see was placed to you.

So if you want each person's calls to reach their own phone, **add the
integration twice**, once per ButterflyMX login. Each doorbell entity is tied to
one resident for good, so the entity that fired *is* the answer to who the call
was for — there is no ID to inspect.

How exact that is depends on your building's directory. If it lists residents
separately, as in "David Unit 4B" and "Blair Unit 4B", a visitor picks a name
and only that person's doorbell fires. If it lists the unit once instead, a
visitor rings the unit and both doorbells fire for the same person at the door,
which means two notifications unless you handle it.

```yaml
automation:
  - alias: "Doorbell goes to the right phone"
    triggers:
      - trigger: state
        entity_id: event.davids_doorbell
        id: david
      - trigger: state
        entity_id: event.sarahs_doorbell
        id: sarah
    actions:
      - choose:
          - conditions:
              - condition: trigger
                id: david
            sequence:
              - action: notify.mobile_app_davids_phone
                data:
                  message: "Someone is at the door"
                  data:
                    image: "{{ trigger.to_state.attributes.image_url }}"
          - conditions:
              - condition: trigger
                id: sarah
            sequence:
              - action: notify.mobile_app_sarahs_phone
                data:
                  message: "Someone is at the door"
                  data:
                    image: "{{ trigger.to_state.attributes.image_url }}"
```

Three things to expect when you set up the second one:

- **Each login gets its own copy of everything, doors included.** You will have
  two `lock.front_entrance`-ish entities for one physical door. That is
  deliberate, not a bug: a door release is performed *as* a resident, and the
  ButterflyMX access log records who opened it. Opening the door from your
  lock is logged as you, and from your partner's is logged as them.
- **If you share a unit, the two sets arrive with nearly identical names**,
  since both are "Unit 4B". Rename them to something you can tell apart before
  you write any automations, as the example above assumes.
- **Passes are per login too.** Each `sensor.*_passes` shows the codes created
  by that account. A code your partner made in the app appears under theirs.

Door releases need none of this care. The access log is scoped per resident, so
whoever's `event.*_door_opened` fires is whoever opened the door, which makes
"someone got home" automations straightforward.

Before doing any of this, check whether you need it. If you share a unit,
buzzing it may already reach whichever account is set up, and one entry is
simpler than two.

> Not yet tried on a live instance. Two entries is a supported arrangement and
> the account scoping above was checked against a real account, but nobody has
> run two side by side yet. If it misbehaves, please open an issue.

## Settings

**Settings → Devices & services → ButterflyMX → Configure**

| Setting | Default | What it does |
| --- | --- | --- |
| Call polling interval | 10 seconds | How quickly a visitor call reaches you. Lower is faster and makes more requests. |
| Show door as unlocked for | 5 seconds | How long the lock shows as unlocked afterwards. Cosmetic; it does not change how long the door stays open. |
| Webhook push | Off | Advanced. If your Home Assistant is reachable from the internet, ButterflyMX can tell it about a call the moment one happens, instead of waiting for the next check. Experimental. See below. |

### Webhook push

Off by default, and polling works perfectly well without it. Turning it on
makes the doorbell effectively instant.

The settings screen shows you the exact address ButterflyMX will be told to
send to, something like
`https://your-home-assistant.example.com/api/webhook/a1b2c3...`. **Check that
address is reachable from outside your network before you switch this on.** If
Home Assistant has no external address configured, the setting says so and
there is no point enabling it.

Polling does not stop when push is on, it just slows down. A call delivered
while Home Assistant happens to be restarting is gone for good — ButterflyMX
does not send it again — so a slow check stays running to catch anything push
misses.

**If you turn it on and doorbells stop arriving**, push is not reaching you.
Turn the setting back off, confirm the doorbell works again on polling alone,
and then look at why the address is not reachable: a reverse proxy, a firewall,
or an external URL in Home Assistant that no longer matches reality. Turning it
off is always safe.

## Questions

**Can it see or do anything I cannot?**
No. It signs in as you and is limited to exactly what your account can already
do: the doors you can open, and calls to your own unit.

**Does it manage residents, keys or accounts?**
No, and that is deliberate. It does not add or remove residents, units, access
groups or key fobs. It opens doors and tells you when someone is at one. Anything
administrative belongs in ButterflyMX's own tools.

**Do I need to expose Home Assistant to the internet?**
No. It checks for new calls on a timer by default and works entirely from inside
your network. Exposing Home Assistant is only needed for the optional webhook
setting.

**Can I answer or decline a call from Home Assistant?**
No. ButterflyMX's API can list calls and open doors; there is no endpoint for
picking up or hanging up. You can be told instantly that someone is there, see
their photo and let them in, but talking to them means the ButterflyMX app.

**Why is there no live video or intercom audio?**
ButterflyMX only offers those through their own phone apps, using technology that
cannot run inside Home Assistant. There is no way around it. The still photo from
each call is the closest available, which is why you get an image rather than a
camera. Keep using the ButterflyMX app when you want to see and talk to a
visitor, and use this to unlock the door and to build automations around it.

**What about the cameras I see in the ButterflyMX app?**
Same answer: not available outside their apps. If those are ordinary security
cameras on your own network, Home Assistant can usually connect to them directly,
which works far better than going through ButterflyMX anyway.

**Can I create visitor or delivery codes?**
Yes. See [Visitor and delivery passes](#visitor-and-delivery-passes).

**Will it work in my building?**
If ButterflyMX is installed and you are a resident with an account, yes.

## Troubleshooting

**The doorbell is slow.** Lower the polling interval in the settings above.

**Doors are missing.** Only doors your ButterflyMX account can open appear. If
you cannot open a door in their app, it will not show up here.

**Something is broken.** Turn on detailed logging, reproduce the problem, then
[open an issue](https://github.com/dkmcgowan/ha-butterflymx/issues) with what the
log says:

```yaml
logger:
  default: warning
  logs:
    custom_components.butterflymx: debug
```

The integration page also has a **Download diagnostics** button. It is safe to
attach to an issue; passwords, tokens and personal details are removed
automatically.

---

## For developers

Everything below is only relevant if you want to work on the integration itself.

### Running the tests

```bash
pip install -r requirements-dev.txt
ruff check custom_components tests scripts
pytest
```

The suite uses the Home Assistant test harness, which needs Linux or macOS.
Home Assistant imports `fcntl`, so it will not run on Windows. CI runs it on
every push.

### Checking the API by hand

`scripts/probe_api.py` signs in once and reports what a live account actually
returns: which endpoints a resident may call, what values appear in call
payloads, whether snapshot URLs are pre-signed, and the shape of access logs,
schedules, passes and PINs. Standard library only, so no virtualenv is needed.

```bash
export BMX_CLIENT_ID=... BMX_CLIENT_SECRET=...
python scripts/probe_api.py --env sandbox
```

Every request it makes is a `GET`; it never creates anything or opens a door. It
caches the token in `scripts/.bmx-token-<env>.json` and writes a redacted
transcript to `scripts/probe-output-<env>.json`. Both are gitignored, and names,
emails, PINs, pass codes and URL signatures are stripped before anything is
written, so the transcript is safe to attach to an issue.

Sandbox credentials are requested separately from production ones. Test against
sandbox first: door releases are real, and a mistake pointed at production opens
an actual door.

### Trying a change on a real instance

Install it the way everyone else does: add your fork or branch to HACS as a
custom repository, per [Through HACS](#through-hacs) above. That exercises the
packaging as well as the code — `hacs.json`, the manifest version, the brand
images — which copying files over SSH quietly skips.

Bump `version` in `manifest.json` before you push, or HACS will not offer the
update. A full Home Assistant restart is needed for Python changes; reloading
the config entry re-runs setup without re-importing changed modules.

### How it talks to ButterflyMX

Authorization is the OAuth authorization-code flow with PKCE. ButterflyMX issues
public clients, so there is no client secret in the flow and nothing sensitive in
the URL you open in your browser. Access tokens last 24 hours. The integration
refreshes a few minutes before expiry and stores both halves of the new pair,
because ButterflyMX rotates the refresh token every time. A rejected refresh
raises a re-authentication flow.

There is no client-side request throttling, deliberately. ButterflyMX publishes
no rate limits and returns no rate-limit headers, requests are issued one at a
time rather than in parallel, and pacing them only added delay. What remains is
what matters when something goes wrong: exponential backoff with jitter on `429`
and `5xx`, honoring `Retry-After`. Door releases are never retried, because a
retry could open a door twice, and repeated releases of the same door within
3 seconds are dropped. Building topology is refreshed hourly.

Calls are read from the call log and door openings from the access log, always.
When webhook push is on, a delivery does not carry either: it only tells the
integration to read the logs immediately. A delivery has no call ID the REST API recognises, no timestamp,
and nothing saying whether anyone answered, so using it as data would mean
ringing the doorbell twice for one visitor and knowing less about it. The logs
are the single source of truth and push just makes them prompt.

The access log is polled more slowly than the call log. A visitor at the door is
a call and needs to be immediate; a door having been opened is worth knowing but
does not need checking every few seconds.

Issues and pull requests are welcome.

## Trademarks

ButterflyMX and the ButterflyMX logo are trademarks of ButterflyMX, Inc. The
brand images in `custom_components/butterflymx/brand/` are ButterflyMX's
property and are included only to identify the product this integration talks
to, the way Home Assistant's own
[brands repository](https://github.com/home-assistant/brands) does for every
other integration.

This project is not built, endorsed or supported by ButterflyMX, and the MIT
licence below covers the code in this repository, not those images.

## License

[MIT](LICENSE)

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://hacs.xyz
[validate-badge]: https://github.com/dkmcgowan/ha-butterflymx/actions/workflows/validate.yml/badge.svg
[validate-url]: https://github.com/dkmcgowan/ha-butterflymx/actions/workflows/validate.yml
