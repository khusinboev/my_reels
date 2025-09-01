# update_cookies.py
import instaloader
import os

COOKIES_PATH = "/home/myreels/cookies.txt"  # yt-dlp ishlatadigan cookie fayl

def update_cookies(username: str, password: str):
    L = instaloader.Instaloader()

    # Instagram login
    L.login(username, password)

    # Session saqlash
    session_file = f"{username}.session"
    L.save_session_to_file(session_file)

    # Instaloader session → Netscape format (yt-dlp uchun)
    from http.cookiejar import MozillaCookieJar
    import requests.utils

    session = L.context._session
    cj = MozillaCookieJar(COOKIES_PATH)
    for c in session.cookies:
        ck = requests.utils.dict_from_cookiejar(session.cookies)
        cj.set_cookie(requests.cookies.create_cookie(
            name=c.name, value=c.value, domain=c.domain, path=c.path
        ))
    cj.save(ignore_discard=True, ignore_expires=True)

    print(f"[+] Cookies updated → {COOKIES_PATH}")

if __name__ == "__main__":
    import getpass
    username = input("Instagram username: ")
    password = getpass.getpass("Instagram password: ")
    update_cookies(username, password)
