"""Standalone worker that reads current Windows toast notifications
and prints them as JSON to stdout.

Run as a subprocess by bot.py rather than in-process: a malformed
notification can trigger a native crash deep in the WinRT bindings
(observed with a synthetic test toast lacking real app info) --
isolating this in its own short-lived process means that crash can
never take down the main bot process, just one poll cycle.
"""
import asyncio
import json

from winsdk.windows.ui.notifications import NotificationKinds
from winsdk.windows.ui.notifications.management import (
    UserNotificationListener,
    UserNotificationListenerAccessStatus,
)


async def fetch():
    listener = UserNotificationListener.current
    status = await listener.request_access_async()
    if status != UserNotificationListenerAccessStatus.ALLOWED:
        return []

    notifications = await listener.get_notifications_async(NotificationKinds.TOAST)
    items = []
    for n in notifications:
        try:
            try:
                app_name = n.app_info.display_info.display_name if n.app_info else "Unknown"
            except Exception:
                app_name = "Unknown"
            bindings = n.notification.visual.bindings
            texts = [t.text for t in bindings[0].get_text_elements()] if bindings.size > 0 else []
            items.append([n.id, app_name, texts])
        except Exception:
            continue
    return items


def main():
    items = asyncio.run(fetch())
    print(json.dumps(items))


if __name__ == "__main__":
    main()
