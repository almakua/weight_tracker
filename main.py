import csv
import io
import json as json_lib
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Depends, Request, Form, HTTPException, UploadFile, File
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


@app.get("/import", response_class=HTMLResponse)
async def import_page(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("import.html", {"request": request, "user": user})


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


# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

def parse_date_flexible(s: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_weight(raw: str) -> Optional[float]:
    try:
        w = round(float(str(raw).replace(",", ".")), 2)
        return w if 10 < w < 700 else None
    except (ValueError, TypeError):
        return None


def _existing_for_day(user_id: int, dt: datetime, db: Session) -> Optional[Weight]:
    day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end   = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return (
        db.query(Weight)
        .filter(Weight.user_id == user_id, Weight.recorded_at >= day_start, Weight.recorded_at <= day_end)
        .first()
    )


@app.post("/api/import/preview")
async def api_import_preview(
    request: Request,
    source: str = Form(...),
    file: Optional[UploadFile] = File(None),
    rows: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=401)

    parsed: list[dict] = []
    errors: list[str] = []

    if source == "csv":
        if not file:
            raise HTTPException(status_code=422, detail="File mancante")
        content = (await file.read()).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        for i, row in enumerate(reader, 1):
            date_val   = next((row[k] for k in ("data", "date", "Data", "Date") if k in row), None)
            weight_val = next((row[k] for k in ("peso_kg", "weight", "peso", "Weight", "Peso") if k in row), None)
            if date_val is None or weight_val is None:
                errors.append(f"Riga {i}: colonne non riconosciute ({list(row.keys())})")
                continue
            dt = parse_date_flexible(date_val)
            if not dt:
                errors.append(f"Riga {i}: data non valida '{date_val}'")
                continue
            w = _parse_weight(weight_val)
            if w is None:
                errors.append(f"Riga {i}: peso non valido '{weight_val}'")
                continue
            parsed.append({"date": dt.strftime("%Y-%m-%d"), "weight": w})

    elif source == "json":
        if not file:
            raise HTTPException(status_code=422, detail="File mancante")
        content = (await file.read()).decode("utf-8")
        try:
            data = json_lib.loads(content)
            if not isinstance(data, list):
                raise ValueError("Il JSON deve essere un array")
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        for i, item in enumerate(data, 1):
            date_val   = item.get("data") or item.get("date")
            weight_val = item.get("peso_kg") or item.get("weight") or item.get("peso")
            if date_val is None or weight_val is None:
                errors.append(f"Elemento {i}: campi data/peso mancanti")
                continue
            dt = parse_date_flexible(str(date_val))
            if not dt:
                errors.append(f"Elemento {i}: data non valida '{date_val}'")
                continue
            w = _parse_weight(weight_val)
            if w is None:
                errors.append(f"Elemento {i}: peso non valido '{weight_val}'")
                continue
            parsed.append({"date": dt.strftime("%Y-%m-%d"), "weight": w})

    elif source == "manual":
        if not rows:
            raise HTTPException(status_code=422, detail="Dati mancanti")
        try:
            manual = json_lib.loads(rows)
        except Exception:
            raise HTTPException(status_code=422, detail="Formato JSON non valido")
        for i, item in enumerate(manual, 1):
            date_val   = str(item.get("date", "")).strip()
            weight_val = str(item.get("weight", "")).strip()
            if not date_val and not weight_val:
                continue
            dt = parse_date_flexible(date_val)
            if not dt:
                errors.append(f"Riga {i}: data non valida '{date_val}'")
                continue
            w = _parse_weight(weight_val)
            if w is None:
                errors.append(f"Riga {i}: peso non valido '{weight_val}'")
                continue
            parsed.append({"date": dt.strftime("%Y-%m-%d"), "weight": w})
    else:
        raise HTTPException(status_code=422, detail="Source non valida")

    # Deduplicate by date (last wins)
    deduped = {r["date"]: r["weight"] for r in parsed}
    parsed = [{"date": d, "weight": w} for d, w in sorted(deduped.items())]

    # Check conflicts
    result_rows = []
    for row in parsed:
        dt = datetime.strptime(row["date"], "%Y-%m-%d")
        existing = _existing_for_day(user.id, dt, db)
        result_rows.append({
            "date": row["date"],
            "weight": row["weight"],
            "conflict": existing is not None,
            "existing_weight": existing.weight if existing else None,
            "existing_id": existing.id if existing else None,
        })

    return {
        "rows": result_rows,
        "errors": errors,
        "total": len(result_rows),
        "conflicts": sum(1 for r in result_rows if r["conflict"]),
    }


@app.post("/api/import/confirm")
async def api_import_confirm(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=401)

    body  = await request.json()
    rows  = body.get("rows", [])
    imported = skipped = 0

    for row in rows:
        if row.get("action") == "skip":
            skipped += 1
            continue
        dt  = datetime.strptime(row["date"], "%Y-%m-%d")
        w   = round(float(row["weight"]), 2)
        existing = _existing_for_day(user.id, dt, db)
        if existing:
            existing.weight = w
        else:
            db.add(Weight(user_id=user.id, weight=w, recorded_at=dt.replace(hour=12, minute=0, second=0)))
        imported += 1

    db.commit()
    return {"imported": imported, "skipped": skipped}
