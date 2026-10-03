# Status on the phone through Home Assistant

The hub has no phone app. Home Assistant has one, and its iOS app can show a Live Activity on the lock screen. This page connects the two: Home Assistant reads the status of the hub and keeps one Live Activity up to date.

This setup is a draft. The hub side is tested. The Home Assistant side is written from the documentation and has not run yet.

## What you need

- Home Assistant Core 2026.7.0 or later, and the Home Assistant app on iOS 17.2 or later. See [Live Activities and Live Updates](https://companion.home-assistant.io/docs/notifications/live-activities/).
- Home Assistant must reach the hub at `http://192.168.178.185:8787`.
- The name of the notify action of your phone, for example `notify.mobile_app_iphone`.

## What the hub provides

`GET /status.json` returns the open sessions:

```json
{
  "waiting": 1, "running": 2, "paused": 0, "active": 3,
  "headline": "1 waiting for you, 2 running",
  "detail": "orbis: Fix the pale fill · claude-hub: Add usage page",
  "sessions": [],
  "generated_at": "2026-10-03T10:00:00Z"
}
```

A reply counts as "waiting for you" for two hours. A lock screen is visible to others, so consider `http://192.168.178.185:8787/status.json?hide=1` as the resource: it shows private projects as "Project n" and hides their session titles. If the web view has a password, add `username` and `password` to the `rest` entry.

## Home Assistant configuration

Replace `MOBILE_APP_NAME` with the name of your phone in Home Assistant.

```yaml
rest:
  - resource: http://192.168.178.185:8787/status.json
    scan_interval: 30
    sensor:
      - name: Claude hub active
        value_template: "{{ value_json.active }}"
      - name: Claude hub waiting
        value_template: "{{ value_json.waiting }}"
      - name: Claude hub headline
        value_template: "{{ value_json.headline }}"
      - name: Claude hub detail
        value_template: "{{ value_json.detail }}"

automation:
  - alias: Claude hub live activity
    mode: queued
    triggers:
      - trigger: state
        entity_id:
          - sensor.claude_hub_headline
          - sensor.claude_hub_detail
    actions:
      - choose:
          - conditions: "{{ states('sensor.claude_hub_active') | int(0) == 0 }}"
            sequence:
              - action: notify.MOBILE_APP_NAME
                data:
                  message: clear_notification
                  data:
                    tag: claude_hub
        default:
          - action: notify.MOBILE_APP_NAME
            data:
              title: Claude hub
              message: "{{ states('sensor.claude_hub_headline') }}. {{ states('sensor.claude_hub_detail') }}"
              data:
                tag: claude_hub
                live_update: true
                silent: true
```

## Limits

- The status is as fresh as the last collector run. `hub loop` runs every 300 seconds, so a change can take five minutes to reach the phone.
- iOS ends a Live Activity after at most eight hours. The next status change starts a new one.
- iOS slows down frequent updates.
- The phone and Home Assistant exchange a token before the first Live Activity. That needs a connection between the two.
