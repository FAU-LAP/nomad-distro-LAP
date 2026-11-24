"""Dashboard plugin mounted inside NOMAD's dashboard catalogue."""

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from nomad.app.v1.routers.auth import get_current_user
from nomad.auth.scopes import Scope
from nomad.config import config


# Protect the dashboard mount itself, not only the /camels API called by the
# embedded page. This prevents GUI v2 from showing a previously public iframe
# after a user has signed out.
read_user = get_current_user([Scope.ENTRIES_READ], allow_anonymous=False)

# Deployment base path of the NOMAD backend, e.g. "/backend" or "/nomad-oasis".
API_BASE_PATH = config.services.api_base_path.rstrip("/")

AUTHENTICATION_REQUIRED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Authentication required</title>
<style>
html,body{width:100%;height:100%}body{margin:0;background:#f8f9fb;font-family:Arial,sans-serif;color:#172033;display:flex;align-items:center;justify-content:center}
.card{box-sizing:border-box;max-width:420px;width:100%;margin:16px;padding:20px;background:#fff;border:1px solid #d4d8e0;border-radius:8px;box-shadow:0 2px 4px #0002;color:#1d47c9}
.content{display:flex;flex-direction:column;align-items:center;gap:16px;text-align:center}.title{font-size:25px;font-weight:700;line-height:1.2;margin:0;display:flex;align-items:center;gap:12px}.lock{width:40px;height:40px;fill:#202124;flex:none}p{font-size:14px;margin:0;color:#29374c}button{width:100%;margin-top:20px;border:0;border-radius:6px;background:#2850dd;color:#fff;font-weight:700;padding:10px;cursor:pointer;text-transform:none}
</style></head><body><main class="card"><div class="content"><h1 class="title"><svg class="lock" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 17a2 2 0 1 0 0-4 2 2 0 0 0 0 4zm6-9h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6-5c1.66 0 3 1.34 3 3v2H9V6c0-1.66 1.34-3 3-3zm6 17H6V10h12v10z"/></svg>Authentication required</h1><p>You need to sign in to access this resource.</p></div><button onclick="signIn()">Sign in</button></main><script>function signIn(){try{const login=window.top.document.querySelector('[aria-label=Login]');if(login){login.click();return;}}catch(error){}window.top.location.assign('__GUI_V2_PATH__');}</script></body></html>""".replace(
    "__GUI_V2_PATH__", f"{API_BASE_PATH}/gui/v2/"
)


def _redirect_dashboard(target: str) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(HTTPException)
    async def authentication_error(_request: Request, exc: HTTPException):
        """Render 401s like GUI v2 Actions instead of raw JSON in an iframe."""
        if exc.status_code == 401:
            return HTMLResponse(
                AUTHENTICATION_REQUIRED_PAGE,
                status_code=401,
                headers=exc.headers,
            )
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )

    @app.get("/")
    def index(_user=Depends(read_user)):
        # GUI v2 already embeds a dashboard in an iframe. Redirecting lets it
        # load the interactive setup page directly, instead of nesting a second
        # iframe that can interfere with focus and button interactions.
        return RedirectResponse(
            url=target,
            status_code=307,
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    return app


def create_dashboard() -> FastAPI:
    """Connection-management dashboard kept for backwards compatibility."""
    return _redirect_dashboard(f"{API_BASE_PATH}/camels/setup")


def create_monitor_dashboard() -> FastAPI:
    """Dedicated dashboard for measurement status and live plots."""
    return _redirect_dashboard(f"{API_BASE_PATH}/camels/dashboard")
