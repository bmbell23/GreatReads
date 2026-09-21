"""User settings API routes."""

import json
from typing import Dict, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.user_settings import (
    UserSettings,
    UserSettingsCreate,
    UserSettingsUpdate,
    UserSettingsResponse,
    ReadingSpeedsSettings
)
from ..services.settings_service import (
    calculate_auto_wpd,
    get_wpd_mode as _get_wpd_mode,
)

router = APIRouter()


class WpdModeRequest(BaseModel):
    mode: str  # "manual" or "auto"


class FormatTrackingSettings(BaseModel):
    """Which formats are tracked against a DAILY reading goal (#289).

    Independent of the per-format words-per-day values on purpose: `*_wpd` still
    drives TBR/chain estimates (chain_calculator does
    `days_remaining = words_remaining / wpd`), so switching a format off must NOT
    zero its speed — that would divide-by-zero the queue the user explicitly
    wants left untouched. Turning a format off only removes its daily goal bar
    from Home + Stats; reading in it is still recorded and still counts toward
    book progress and year/stats totals.
    """
    ebook: bool = True
    physical: bool = True
    audio: bool = True


class AutoWpdResponse(BaseModel):
    ebook: Optional[int] = None
    physical: Optional[int] = None
    audio: Optional[int] = None
    ebook_sample_count: int = 0
    physical_sample_count: int = 0
    audio_sample_count: int = 0


class WpdModeResponse(BaseModel):
    mode: str


@router.get("/wpd-mode", response_model=WpdModeResponse)
async def get_wpd_mode_endpoint(db: Session = Depends(get_db)):
    """Get the current WPD calculation mode ('manual' or 'auto')."""
    return WpdModeResponse(mode=_get_wpd_mode(db))


@router.put("/wpd-mode", response_model=WpdModeResponse)
async def set_wpd_mode(request: WpdModeRequest, db: Session = Depends(get_db)):
    """Set the WPD calculation mode to 'manual' or 'auto'."""
    if request.mode not in ("manual", "auto"):
        raise HTTPException(status_code=400, detail="mode must be 'manual' or 'auto'")

    setting = db.query(UserSettings).filter(
        UserSettings.setting_key == "wpd_mode"
    ).first()

    if setting:
        setting.setting_value = request.mode
    else:
        setting = UserSettings(setting_key="wpd_mode", setting_value=request.mode)
        db.add(setting)

    db.commit()
    return WpdModeResponse(mode=request.mode)


@router.get("/auto-wpd", response_model=AutoWpdResponse)
async def get_auto_wpd(db: Session = Depends(get_db)):
    """Calculate and return auto WPD for each format based on historical reads."""
    from ..models.reading import Reading

    auto = calculate_auto_wpd(db)

    # Count samples per format
    finished = db.query(Reading).filter(
        Reading.date_finished_actual.isnot(None),
        Reading.date_started.isnot(None),
    ).all()

    counts = {"ebook": 0, "physical": 0, "audio": 0}
    for r in finished:
        if not r.book or not r.book.word_count:
            continue
        media = (r.media or "").lower()
        if media in ("ebook", "kindle"):
            counts["ebook"] += 1
        elif media in ("physical", "hardcover", "paperback"):
            counts["physical"] += 1
        elif media in ("audio", "audiobook"):
            counts["audio"] += 1

    return AutoWpdResponse(
        ebook=auto.get("ebook"),
        physical=auto.get("physical"),
        audio=auto.get("audio"),
        ebook_sample_count=counts["ebook"],
        physical_sample_count=counts["physical"],
        audio_sample_count=counts["audio"],
    )


@router.get("/reading-speeds", response_model=ReadingSpeedsSettings)
async def get_reading_speeds(db: Session = Depends(get_db)):
    """Get reading speeds settings."""
    # Try to get from database
    setting = db.query(UserSettings).filter(
        UserSettings.setting_key == "reading_speeds"
    ).first()

    if setting:
        speeds = json.loads(setting.setting_value)
        return ReadingSpeedsSettings(**speeds)

    # Return defaults if not found
    return ReadingSpeedsSettings()


@router.put("/reading-speeds", response_model=ReadingSpeedsSettings)
async def update_reading_speeds(
    speeds: ReadingSpeedsSettings,
    db: Session = Depends(get_db)
):
    """Update reading speeds settings."""
    # Check if setting exists
    setting = db.query(UserSettings).filter(
        UserSettings.setting_key == "reading_speeds"
    ).first()

    speeds_dict = {
        "ebook_wpd": speeds.ebook_wpd,
        "physical_wpd": speeds.physical_wpd,
        "audio_wpd": speeds.audio_wpd
    }

    if setting:
        # Update existing
        setting.setting_value = json.dumps(speeds_dict)
    else:
        # Create new
        setting = UserSettings(
            setting_key="reading_speeds",
            setting_value=json.dumps(speeds_dict)
        )
        db.add(setting)

    db.commit()
    db.refresh(setting)

    return speeds


# NOTE: these literal routes MUST stay above the dynamic "/{setting_key}"
# routes below — FastAPI matches in registration order, so a later literal
# path is swallowed by the earlier catch-all and 404s "Setting not found".
FORMAT_TRACKING_KEY = "format_tracking"


def _read_format_tracking(db: Session) -> FormatTrackingSettings:
    """Stored tracking flags, defaulting every format to ON (current behaviour)."""
    setting = db.query(UserSettings).filter(
        UserSettings.setting_key == FORMAT_TRACKING_KEY
    ).first()
    if not setting:
        return FormatTrackingSettings()
    try:
        return FormatTrackingSettings(**json.loads(setting.setting_value))
    except (ValueError, TypeError):
        # Corrupt/legacy value: fall back to "track everything" rather than
        # silently hiding the user's goal bars.
        return FormatTrackingSettings()


@router.get("/format-tracking", response_model=FormatTrackingSettings)
async def get_format_tracking(db: Session = Depends(get_db)):
    """Per-format daily-goal tracking flags (#289)."""
    return _read_format_tracking(db)


@router.put("/format-tracking", response_model=FormatTrackingSettings)
async def update_format_tracking(
    tracking: FormatTrackingSettings,
    db: Session = Depends(get_db),
):
    """Set per-format daily-goal tracking flags (#289).

    Writes only this key — `reading_speeds` is deliberately left alone so the
    estimates keep working exactly as before.
    """
    payload = json.dumps({
        "ebook": bool(tracking.ebook),
        "physical": bool(tracking.physical),
        "audio": bool(tracking.audio),
    })
    setting = db.query(UserSettings).filter(
        UserSettings.setting_key == FORMAT_TRACKING_KEY
    ).first()
    if setting:
        setting.setting_value = payload
    else:
        setting = UserSettings(setting_key=FORMAT_TRACKING_KEY, setting_value=payload)
        db.add(setting)
    db.commit()
    db.refresh(setting)
    return tracking

@router.get("/all")
async def get_all_settings(db: Session = Depends(get_db)):
    """Get all user settings."""
    settings = db.query(UserSettings).all()
    result = {}

    for setting in settings:
        try:
            result[setting.setting_key] = json.loads(setting.setting_value)
        except json.JSONDecodeError:
            result[setting.setting_key] = setting.setting_value

    return result


@router.get("/{setting_key}", response_model=UserSettingsResponse)
async def get_setting(setting_key: str, db: Session = Depends(get_db)):
    """Get a specific setting."""
    setting = db.query(UserSettings).filter(
        UserSettings.setting_key == setting_key
    ).first()

    if not setting:
        raise HTTPException(status_code=404, detail="Setting not found")

    return setting


@router.put("/{setting_key}", response_model=UserSettingsResponse)
async def update_setting(
    setting_key: str,
    update: UserSettingsUpdate,
    db: Session = Depends(get_db)
):
    """Update a specific setting."""
    setting = db.query(UserSettings).filter(
        UserSettings.setting_key == setting_key
    ).first()

    if setting:
        setting.setting_value = update.setting_value
    else:
        setting = UserSettings(
            setting_key=setting_key,
            setting_value=update.setting_value
        )
        db.add(setting)

    db.commit()
    db.refresh(setting)

    return setting


@router.post("/", response_model=UserSettingsResponse)
async def create_setting(
    setting: UserSettingsCreate,
    db: Session = Depends(get_db)
):
    """Create a new setting."""
    # Check if already exists
    existing = db.query(UserSettings).filter(
        UserSettings.setting_key == setting.setting_key
    ).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail="Setting already exists. Use PUT to update."
        )

    db_setting = UserSettings(**setting.dict())
    db.add(db_setting)
    db.commit()
    db.refresh(db_setting)

    return db_setting
