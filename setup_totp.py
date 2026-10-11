"""One-time enrollment of an authenticator app for power mode ("do").

Run this ON THE LAPTOP, in a terminal you're sitting at:
    python setup_totp.py            (add --reset to replace an existing secret)

The secret is shown on this screen only -- never sent through Telegram,
whose chat history would defeat the point. It's saved to totp_secret.txt
(gitignored) only after you prove your app produces a matching code.
"""

import os
import sys
import tempfile

import qrcode

import totp

SECRET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "totp_secret.txt")


def main():
    if os.path.exists(SECRET_FILE) and "--reset" not in sys.argv:
        print("Power mode is already set up. Run with --reset to replace the secret.")
        return

    secret = totp.generate_secret()
    uri = totp.provisioning_uri(secret, account="laptop", issuer="AXIOM Remote")

    png = os.path.join(tempfile.gettempdir(), "axiom_totp_setup.png")
    qrcode.make(uri).save(png)
    try:
        os.startfile(png)
        print("A QR code just opened. In your authenticator app (Google Authenticator,")
        print("Microsoft Authenticator, Authy...) add an account and scan it.")
        print()
        print("Can't scan? Enter this key manually (type: time-based):")
        print(f"    {secret}")
        print()
        for attempt in range(3):
            code = input("Enter the 6-digit code your app shows now: ")
            if totp.verify(secret, code) is not None:
                with open(SECRET_FILE, "w") as f:
                    f.write(secret)
                print("Matched. Power mode is set up.")
                return
            print("That didn't match -- wait for the next code and try again.")
        print("Giving up; nothing was saved. Run the script again to retry.")
    finally:
        try:
            os.remove(png)
        except OSError:
            pass


if __name__ == "__main__":
    main()
