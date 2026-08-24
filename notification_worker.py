"""Standalone worker for reading Windows toast notifications, run as a
subprocess by bot.py rather than in-process.

Two modes, both printing JSON to stdout:

  notification_worker.py list
      -> [[id, app_name], ...] for every notification currently in
         the Action Center. Only touches .id and .app_info, which
         has proven safe in testing.

  notification_worker.py text <id>
      -> [line, ...] the text elements of one specific notification.

Why two modes, and why subprocesses at all: reading a notification's
text via get_text_elements() was found to reliably segfault -- a
native crash in the winsdk bindings, not a raiseable Python
exception, so no try/except can catch it. It happens on both
synthetic test toasts AND genuine system notifications (a real "USB
device" notification reproduced it), so it isn't just an edge case.

Isolating each notification's text fetch in its own subprocess means
one bad notification can only take down the fetch for itself -- the
listing (which app sent something) still works, and other
notifications' text is unaffected.
"""
import asyncio
import json
import sys

from winsdk.windows.ui.notifications import NotificationKinds
from winsdk.windows.ui.notifications.management import (
    UserNotificationListener,
    UserNotificationListenerAccessStatus,
)


async def _get_listener():
    listener = UserNotificationListener.current
    status = await listener.request_access_async()
    if status != UserNotificationListenerAccessStatus.ALLOWED:
        return None
    return listener


async def list_notifications():
    listener = await _get_listener()
    if listener is None:
        return []

    notifications = await listener.get_notifications_async(NotificationKinds.TOAST)
    items = []
    for n in notifications:
        try:
            app_name = n.app_info.display_info.display_name if n.app_info else "Unknown"
        except Exception:
            app_name = "Unknown"
        items.append([n.id, app_name])
    return items


async def get_text(notif_id: int):
    listener = await _get_listener()
    if listener is None:
        return []

    notifications = await listener.get_notifications_async(NotificationKinds.TOAST)
    for n in notifications:
        if n.id != notif_id:
            continue
        bindings = n.notification.visual.bindings
        if bindings.size == 0:
            return []
        return [t.text for t in bindings[0].get_text_elements()]
    return []


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "list"
    if mode == "list":
        result = asyncio.run(list_notifications())
    elif mode == "text":
        notif_id = int(sys.argv[2])
        result = asyncio.run(get_text(notif_id))
    else:
        raise SystemExit(f"Unknown mode: {mode}")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
