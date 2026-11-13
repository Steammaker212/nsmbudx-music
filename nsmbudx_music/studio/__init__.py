"""nsmbudx-music Studio: the archive editor as a local web app.

    python -m nsmbudx_music.studio [Sound.bfsar] [--browser] [--port N]

Opens in a Microsoft Edge app window (no tabs or address bar) when Edge is installed,
otherwise in the default browser. The server stops when the window closes.
"""
import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

EDGE_PATHS = [
    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
]


def find_edge():
    for p in EDGE_PATHS:
        if os.path.isfile(p):
            return p
    return shutil.which("msedge")


def main(argv=None):
    from . import server

    p = argparse.ArgumentParser(prog="nsmbudx-music-studio", description="Sound archive editor")
    p.add_argument("archive", nargs="?", help="Sound.bfsar to open")
    p.add_argument("--browser", action="store_true", help="use the default browser, not an app window")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--serve", action="store_true", help="only run the server and print its address")
    a = p.parse_args(argv)

    srv = server.make_server(a.port)
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/?t={server.Handler.token}"
    if a.archive:
        url += "&open=" + __import__("urllib.parse").parse.quote(os.path.abspath(a.archive))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    print(f"nsmbudx-music Studio on {url}", flush=True)
    if a.serve:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return srv.shutdown()
    edge = None if a.browser else find_edge()
    if edge:
        profile = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "nsmbudx-music", "window")
        os.makedirs(profile, exist_ok=True)
        proc = subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                                 "--disable-features=Translate", "--window-size=1440,900"])
        proc.wait()                      # this profile's Edge exits when the window closes
    else:
        webbrowser.open(url)
        # without a window to watch, stop once the page has stopped calling (it checks in
        # every 10 seconds while open)
        while time.time() - server.Handler.last_seen < 60:
            time.sleep(2)
    srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
