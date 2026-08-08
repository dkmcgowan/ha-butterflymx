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

`<door>` below is the name ButterflyMX already gives that door, and `<unit>` is
your unit. A door called "Front Entrance" in an apartment 4B becomes
`lock.front_entrance` and `event.unit_4b_doorbell`. Yours will read differently.

| Entity | What it is |
| --- | --- |
| `lock.<door>` | One for every door you can already open in the ButterflyMX app. Press to buzz it open. |
| `event.<unit>_doorbell` | Fires the instant a visitor calls your unit. Use it to trigger notifications. |
| `image.<unit>_last_call_snapshot` | The photo the intercom took of your most recent visitor. |
| `sensor.<unit>_last_call` | When the last call happened, and who or what it came from. |
| `event.<unit>_door_opened` | Fires whenever one of your doors is opened, however it happened: a PIN at the keypad, a fob, someone answering the intercom in the app, or Home Assistant. |
| `sensor.<unit>_last_door_opened` | When a door was last opened, which one, and how: `PIN`, `Fob`, `App call` or `API`. |
| `sensor.<unit>_passes` | How many visitor and delivery codes are currently valid, and what they are for. See [Visitor and delivery passes](#visitor-and-delivery-passes). |

The examples below use the same placeholders, so copying one means replacing
`<door>` and `<unit>` with your own. To find them, look under **Settings →
Devices & services → ButterflyMX → entities**, or start typing in any entity
picker.

The door entities only ever cover your own tenancy. ButterflyMX scopes the
access log to the signed-in resident, so you see your own comings and goings and
not the neighbors'. That applies to whoever else lives with you as well. If you
each have your own ButterflyMX login, see
[Two residents, two logins](#two-residents-two-logins).

## Before you start

1. **A ButterflyMX account** that is a resident of the building, the same login
   you use in their app. The integration only ever acts as you, and can only
   open doors you can already open.
2. **Home Assistant 2025.3 or newer.**
3. **A ButterflyMX client ID**, but only if this copy does not ship one. Setup
   asks if it needs it; get one from
   [their developer program](https://apidocs.butterflymx.com/docs/getting-started).
   It identifies the application rather than you, and is a public client, so
   there is no secret to go with it.

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

1. **Enter your client ID**, if you are asked for one. Leave the redirect URI
   alone.
2. **Sign in.** Home Assistant shows a link; open it and approve access on
   ButterflyMX's own site, so your password never reaches Home Assistant.
3. **Paste the code back.** ButterflyMX shows a short code rather than
   redirecting, which is why this step exists. If you land on a web address
   instead, paste the whole thing and the code is picked out of it.

The code expires quickly. Your doors, doorbell and snapshot appear straight
away.

## Things to do with it

The examples with `alias:` at the top are complete automations. To use one, add
a new automation, open the three-dot menu, choose **Edit in YAML**, and replace
everything in the box. The shorter examples that start with `actions:` are just
the action, meant to be dropped into an automation or script you already have.

**See who is at the door and let them in from the notification:**

```yaml
alias: "Someone is at the door"
triggers:
  - trigger: state
    entity_id: event.<unit>_doorbell
actions:
  - action: notify.mobile_app_<your_phone>
    data:
      title: ButterflyMX
      message: "Someone is at {{ trigger.to_state.attributes.device_name }}"
      data:
        tag: butterflymx
        channel: ButterflyMX Visitor
        importance: high
        visibility: public
        ttl: 0
        priority: high
        image: "{{ trigger.to_state.attributes.image_url }}"
        actions:
          - action: BMX_UNLOCK
            title: Unlock
          - action: BMX_DECLINE
            title: Decline
  - wait_for_trigger:
      - trigger: event
        event_type: mobile_app_notification_action
        event_data:
          action: BMX_UNLOCK
      - trigger: event
        event_type: mobile_app_notification_action
        event_data:
          action: BMX_DECLINE
    timeout: "00:00:40"
    continue_on_timeout: true
  - choose:
      # Unlock tapped.
      - conditions:
          - condition: template
            value_template: >-
              {{ wait.trigger is not none
                 and wait.trigger.event.data.action == 'BMX_UNLOCK' }}
        sequence:
          - action: lock.open
            target:
              entity_id: lock.<door>
          - action: notify.mobile_app_<your_phone>
            data:
              message: clear_notification
              data:
                tag: butterflymx
                ttl: 0
                priority: high
      # Decline tapped.
      - conditions:
          - condition: template
            value_template: "{{ wait.trigger is not none }}"
        sequence:
          - action: butterflymx.decline_call
            target:
              entity_id: event.<unit>_doorbell
          - action: notify.mobile_app_<your_phone>
            data:
              message: clear_notification
              data:
                tag: butterflymx
                ttl: 0
                priority: high
    # Nobody tapped anything for 40 seconds.
    default:
      - action: notify.mobile_app_<your_phone>
        data:
          message: clear_notification
          data:
            tag: butterflymx
      - action: notify.mobile_app_<your_phone>
        data:
          title: ButterflyMX
          message: "Missed visitor at {{ trigger.to_state.attributes.device_name }}"
          data:
            tag: butterflymx_missed
            channel: ButterflyMX Missed
            importance: low
            image: "{{ trigger.to_state.attributes.image_url }}"
mode: restart
```

**The four action IDs have to agree.** `BMX_UNLOCK` and `BMX_DECLINE` appear
twice each, as a button and as a trigger. Miss one and nothing matches, so every
visitor silently becomes a missed one.

**Use the `notify.mobile_app_...` action, not a notify entity.** The buttons and
photo travel in the nested `data:` block, and `notify.send_message` accepts only
a title and a message, so it drops all of it. Find the exact name under
**Developer tools → Actions**.

**Channels decide whether it pops up; `priority` only decides how fast it
arrives.** The companion app's default channel is a quiet one, so the ring gets
its own at `importance: high` and the missed notice a second at `importance:
low`, matching the split ButterflyMX uses. A channel keeps the importance it was
created with and can only be lowered afterwards, so if the ring does not pop,
that name already exists: rename it, or turn on "Pop on screen" for it in
Android's notification settings. These are Android options; on iOS use a `push`
block instead.

**"Decline" really does end the call.** `butterflymx.decline_call` sends the
same command the app's own Decline button sends, so the panel stops dialing
instead of rolling the visitor over to a phone call. Clearing the notification
afterwards is only tidying up. What it cannot do is let you speak to them:
answering exists only in ButterflyMX's app.

Opening the door needs no equivalent. `lock.open` tells the panel by itself
whenever a visitor is calling, which is why the example just opens the lock.

The photo needs no special setup: `image_url` points at a plain web address
your phone can load directly, unlike a camera snapshot that would need Home
Assistant to be reachable from outside.

**Open the door from anywhere:**

```yaml
actions:
  - action: lock.open
    target:
      entity_id: lock.<door>
```

Both `lock.open` and `lock.unlock` buzz the door. Add the lock to a dashboard, a
wall tablet by the door, or a voice assistant.

## Visitor and delivery passes

The same two things you can create in the ButterflyMX app, with the same names:

- A **delivery pass** is a single-use code. It opens every door, works once, and
  expires after 30 days. Good for a parcel that needs to get inside the lobby.
- A **visitor pass** is a reusable code valid over a window you choose, for
  whichever doors you choose. Good for a cleaner, a dog walker, or family
  staying the weekend.

### Where the codes live

Creating a pass, and `butterflymx.list_passes`, both return the same thing:
`pass_id`, the window, and a `keys` list holding

- `pin_code`, six digits
- `qr_code_url`, a PNG you can attach straight to a notification
- `instructions_url`, a page explaining the pass to whoever you send it to

The response is keyed by the entity you targeted, so it reads
`response['sensor.<unit>_passes']`.

**None of that is stored in Home Assistant.** The codes are not in the sensor or
its attributes, so they never reach your history database, your logbook or a
diagnostics download. A PIN opens your building's front door and Home Assistant
keeps state history for weeks, which is a bad combination. Anyone holding the
QR link can use it too: it needs no sign-in.

Nothing is lost by that. `butterflymx.list_passes` gives you any code back
whenever you want it, and the ButterflyMX app lists every pass as well.

### Creating a delivery code

```yaml
actions:
  - action: butterflymx.create_delivery_pass
    target:
      entity_id: sensor.<unit>_passes
    data:
      name: "Amazon - Tuesday"
    response_variable: delivery
  - variables:
      pass: "{{ delivery['sensor.<unit>_passes'] }}"
  - action: notify.mobile_app_my_phone
    data:
      title: "Delivery code"
      message: "PIN {{ pass.keys[0].pin_code }}"
      data:
        image: "{{ pass.keys[0].qr_code_url }}"
```

The name is worth choosing well: it is what shows up in the ButterflyMX access
log when the code gets used.

### Creating a visitor code

```yaml
actions:
  - action: butterflymx.create_visitor_pass
    target:
      entity_id: sensor.<unit>_passes
    data:
      name: "Cleaner"
      starts_at: "2026-08-10 09:00:00"
      ends_at: "2026-08-10 13:00:00"
      doors:
        - lock.<door>
    response_variable: visitor
```

Everything but `name` is optional. Leave the times out and it starts now and
runs for four hours; leave `doors` out and it opens all of them.

The response is the same shape as the delivery pass above.

### Seeing and revoking passes

`sensor.<unit>_passes` counts the passes valid right now. Its `passes`
attribute lists them all, expired ones included, with the ID you need to revoke
one and a `used` flag so you can tell whether a delivery code has been redeemed.

```yaml
# Read the codes back, including the PINs.
# all_passes['sensor.<unit>_passes'].passes is the list.
actions:
  - action: butterflymx.list_passes
    target:
      entity_id: sensor.<unit>_passes
    response_variable: all_passes
```

Run that one from **Developer tools → Actions** when you just want to look up a
code: the response appears right there on the page.

```yaml
# Revoke one. This deletes its codes too, and cannot be undone.
actions:
  - action: butterflymx.revoke_pass
    target:
      entity_id: sensor.<unit>_passes
    data:
      pass_id: 44138578
```

**Tidy up expired passes automatically:**

```yaml
alias: "Remove finished ButterflyMX passes"
triggers:
  - trigger: time
    at: "03:00:00"
actions:
  - repeat:
      for_each: >
        {{ state_attr('sensor.<unit>_passes', 'passes')
           | selectattr('ends_at', 'lt', now().isoformat())
           | map(attribute='pass_id') | list }}
      sequence:
        - action: butterflymx.revoke_pass
          target:
            entity_id: sensor.<unit>_passes
          data:
            pass_id: "{{ repeat.item }}"
```

### Sending a code to someone

ButterflyMX can email or text a code for you. That is not offered here. Send it
yourself with a Home Assistant automation, where you control who it goes to, or
create the pass in the ButterflyMX app if you want their delivery.

## Two residents, two logins

Integrations run per instance, not per Home Assistant user, and one ButterflyMX
login only ever sees its own calls. So if you each have your own ButterflyMX
account, **add the integration twice**. Each doorbell entity is then tied to one
resident for good, and the entity that fired tells you who was called.

Whether only one fires depends on your building's directory. Listed separately,
as "David Unit 4B" and "Sarah Unit 4B", a visitor picks a name and one doorbell
rings. Listed as a single unit, both ring for the same person at the door.

Three things to expect:

- **Each login gets its own copy of everything, doors included**, so two lock
  entities for one physical door. That is on purpose: a release is performed
  *as* a resident and the access log records who, so opening from your lock is
  logged as you.
- **Sharing a unit means near-identical names**, since both are "Unit 4B".
  Rename them before writing automations.
- **Passes are per login too.** A code your partner made appears under theirs.

Door releases need none of this care: whoever's `event.*_door_opened` fires is
whoever opened the door.

### Unlocking as whoever tapped

With two locks for one door, pick the one belonging to the person who pressed
the button, so the access log names them and not you. Companion app
notifications carry the Home Assistant user whose phone it was, and person
entities expose theirs, so no IDs need pasting in. Swap the `lock.open` step in
the doorbell automation for this:

```yaml
actions:
  - action: lock.open
    target:
      entity_id: >-
        {{ 'lock.front_and_inner_door_2'
           if wait.trigger.event.context.user_id
              == state_attr('person.sarah_mcgowan', 'user_id')
           else 'lock.front_and_inner_door' }}
```

This only holds if each phone was set up by signing in as **its own** Home
Assistant user. Set up with one shared login, every tap looks like the same
person and lands on the `else`. To check, have them open the door once and see
whose name ButterflyMX records.

Before any of this, check you need it. If you share a unit, one entry may
already cover both of you, and one is simpler than two.

Nobody has run two entries side by side yet. The scoping above was checked
against a real account; the two-entry setup itself has not been. Please open an
issue if it misbehaves.

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
while Home Assistant happens to be restarting is gone for good, because
ButterflyMX does not send it again, so a slow check stays running to catch
anything push misses.

**If you turn it on and doorbells stop arriving**, push is not reaching you.
Turn the setting back off, confirm the doorbell works again on polling alone,
and then look at why the address is not reachable: a reverse proxy, a firewall,
or an external URL in Home Assistant that no longer matches reality. Turning it
off is always safe.

## What it cannot do

- **No video or intercom audio.** Those live only in ButterflyMX's own apps,
  over technology that cannot run inside Home Assistant. You get the still
  photo from each call instead. Same answer for the cameras you see in their
  app; if those are ordinary cameras on your network, connect to them directly.
- **No answering a call.** The API can open doors and stop a call, not pick one
  up. Talking to a visitor means their app.
- **No account management.** It does not add residents, units, access groups or
  fobs, and never will.

It can only ever do what your own account can, against doors you can already
open from their app.

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

```bash
pip install -r requirements-dev.txt
ruff check custom_components tests scripts
pytest
```

The suite needs Linux or macOS. The test harness imports `homeassistant.runner`,
which imports `fcntl` at the top level, so collection fails on Windows before
any test runs. `ruff` works everywhere, and CI runs the suite on Ubuntu on every
push.

`scripts/probe_api.py` signs in and reports what a live account actually
returns. Standard library only, every request a `GET`, and it never opens a
door. The token is cached in `scripts/.bmx-token-<env>.json` and a redacted
transcript written to `scripts/probe-output-<env>.json`; both are gitignored and
names, emails, PINs and pass codes are stripped, so the transcript is safe to
attach to an issue.

```bash
export BMX_CLIENT_ID=...
python scripts/probe_api.py --env sandbox
```

Test against sandbox first. Door releases are real, and a mistake pointed at
production opens an actual door.

Issues and pull requests are welcome.

## Trademarks

ButterflyMX and its logo are trademarks of ButterflyMX, Inc. The images in
`custom_components/butterflymx/brand/` are their property and identify the
product this integration talks to, the way Home Assistant's
[brands repository](https://github.com/home-assistant/brands) does for every
integration. This project is not built, endorsed or supported by ButterflyMX,
and the MIT license covers the code here, not those images.

## License

[MIT](LICENSE)

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://hacs.xyz
[validate-badge]: https://github.com/dkmcgowan/ha-butterflymx/actions/workflows/validate.yml/badge.svg
[validate-url]: https://github.com/dkmcgowan/ha-butterflymx/actions/workflows/validate.yml
