import csv
import io
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import engine, get_db, Base
from models import User, Weight, UserSettings
from auth import verify_password, hash_password, create_token, decode_token

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Weight Tracker")
templates = Jinja2Templates(directory="templates")

COOKIE = "wt_token"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    uid = decode_token(token)
    if uid is None:
        return None
    return db.query(User).filter(User.id == uid).first()


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user


def get_or_create_settings(user: User, db: Session) -> UserSettings:
    if user.settings:
        return user.settings
    s = UserSettings(user_id=user.id)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    return RedirectResponse("/dashboard" if user else "/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, registered: Optional[str] = None):
    return templates.TemplateResponse("login.html", {
        "request": request,
        "registered": registered == "1",
    })


@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username.strip().lower()).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Credenziali non valide.",
        })
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie(COOKIE, create_token(user.id), httponly=True, max_age=30 * 86400, samesite="lax")
    return response


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.post("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    username = username.strip().lower()
    if len(username) < 3:
        return templates.TemplateResponse("register.html", {
            "request": request, "error": "Username troppo corto (minimo 3 caratteri)."
        })
    if len(password) < 6:
        return templates.TemplateResponse("register.html", {
            "request": request, "error": "Password troppo corta (minimo 6 caratteri)."
        })
    if len(password.encode("utf-8")) > 72:
        return templates.TemplateResponse("register.html", {
            "request": request, "error": "Password troppo lunga (massimo 72 caratteri)."
        })
    if db.query(User).filter(User.username == username).first():
        return templates.TemplateResponse("register.html", {
            "request": request, "error": "Username già in uso."
        })
    user = User(username=username, password_hash=hash_password(password))
    db.add(user)
    db.flush()
    db.add(UserSettings(user_id=user.id))
    db.commit()
    return RedirectResponse("/login?registered=1", status_code=302)


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(COOKIE)
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    settings = get_or_create_settings(user, db)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "settings": settings,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    settings = get_or_create_settings(user, db)
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
        "settings": settings,
    })


@app.post("/settings", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    target_weight: Optional[float] = Form(None),
    show_target: Optional[str] = Form(None),
    default_period: int = Form(30),
    moving_avg_days: int = Form(7),
    current_password: Optional[str] = Form(None),
    new_password: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    settings = get_or_create_settings(user, db)
    settings.target_weight = target_weight
    settings.show_target = show_target == "on"
    settings.default_period = max(0, default_period)
    settings.moving_avg_days = max(2, min(30, moving_avg_days))

    error = None
    success = "Impostazioni salvate."

    if current_password:
        if not verify_password(current_password, user.password_hash):
            error = "Password attuale non corretta."
        elif not new_password or len(new_password) < 6:
            error = "Nuova password troppo corta (minimo 6 caratteri)."
        elif len(new_password.encode("utf-8")) > 72:
            error = "Nuova password troppo lunga (massimo 72 caratteri)."
        else:
            user.password_hash = hash_password(new_password)
            success = "Impostazioni e password aggiornate."

    db.commit()

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
        "settings": settings,
        "error": error,
        "success": None if error else success,
    })


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/weights")
async def api_get_weights(
    request: Request,
    days: int = 30,
    db: Session = Depends(get_db),
):
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=401)

    q = db.query(Weight).filter(Weight.user_id == user.id)
    if days > 0:
        since = datetime.utcnow() - timedelta(days=days)
        q = q.filter(Weight.recorded_at >= since)
    weights = q.order_by(Weight.recorded_at.asc()).all()

    return [{"id": w.id, "weight": w.weight, "date": w.recorded_at.isoformat()} for w in weights]


@app.post("/api/weights")
async def api_add_weight(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=401)

    body = await request.json()
    val = body.get("weight")
    if val is None or not (10 < float(val) < 700):
        raise HTTPException(status_code=422, detail="Valore non valido.")

    w = Weight(user_id=user.id, weight=round(float(val), 2))
    db.add(w)
    db.commit()
    db.refresh(w)
    return {"id": w.id, "weight": w.weight, "date": w.recorded_at.isoformat()}


@app.delete("/api/weights/{weight_id}")
async def api_delete_weight(weight_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=401)

    w = db.query(Weight).filter(Weight.id == weight_id, Weight.user_id == user.id).first()
    if not w:
        raise HTTPException(status_code=404)
    db.delete(w)
    db.commit()
    return {"ok": True}


@app.get("/api/export/csv")
async def api_export_csv(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=401)

    weights = (
        db.query(Weight)
        .filter(Weight.user_id == user.id)
        .order_by(Weight.recorded_at.asc())
        .all()
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["data", "peso_kg"])
    for w in weights:
        writer.writerow([w.recorded_at.strftime("%Y-%m-%d %H:%M:%S"), w.weight])

    buf.seek(0)
    filename = f"peso_{user.username}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
