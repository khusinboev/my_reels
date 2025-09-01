# src/utils/cookie_refresher.py
import asyncio
import instaloader
import requests.utils
from http.cookiejar import MozillaCookieJar

COOKIES_PATH = "/home/myreels/cookies.txt"

async def refresh_cookies(username: str, password: str):
    L = instaloader.Instaloader()
    L.login(username, password)

    # Session saqlash
    session = L.context._session

    # Yangi cookies.txt yaratish
    cj = MozillaCookieJar(COOKIES_PATH)
    for c in session.cookies:
        cj.set_cookie(requests.cookies.create_cookie(
            name=c.name, value=c.value, domain=c.domain, path=c.path
        ))
    cj.save(ignore_discard=True, ignore_expires=True)

    return COOKIES_PATH
