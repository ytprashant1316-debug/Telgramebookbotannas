import urllib.request
import re
import time
from bot import launch

def check_playwright():
    md5 = "e5e78000b253d2a9054b42961d485f9a"
    url = f"https://welib.org/fast_preview/{md5}/0/0?viewer=1"
    browser = launch(headless=True)
    try:
        ctx = browser.new_context()
        page = ctx.new_page()
        for i in range(5):
            print(f"Fetching {url}")
            page.goto(url)
            page.wait_for_timeout(2000)
            print("Response URL:", page.url)
            html = page.content()
            if "fast_view" in page.url or "fast_view" in html:
                print("Found fast_view!")
                m = re.search(r'fast_view\?url=[^\s"\'<>]+', html)
                if m:
                    print("Extracted:", m.group(0))
                break
            time.sleep(7)
    except Exception as e:
        print("Error:", e)
    finally:
        browser.close()

check_playwright()
