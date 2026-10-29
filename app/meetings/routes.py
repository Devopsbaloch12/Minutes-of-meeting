import logging
import os
import threading
import time
import uuid
from datetime import timedelta

from flask import (
    Blueprint, Response, abort, current_app, flash, redirect,
    render_template, request, url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import or_
from werkzeug.utils import secure_filename

from app import settings
from app.extensions import celery, db
from app.metrics import MEETINGS_UPLOADED
from app.models import ACTIVE_STATUSES, Meeting, utcnow
from app.utils.storage import remove_recording

meetings_bp = Blueprint("meetings", __name__, url_prefix="/meetings")
log = logging.getLogger("mom.meetings")

PER_PAGE = 15

# kind in the URL -> (Meeting column, file extension, mimetype)
DOWNLOADS = {
    "minutes": ("minutes_of_meeting", "md", "text/markdown"),
    "transcript": ("transcript", "txt", "text/plain"),
    "pdf": ("minutes_of_meeting", "pdf", "application/pdf"),
    "docx": ("minutes_of_meeting", "docx",
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
}


@meetings_bp.before_request
def _apply_upload_limit():
    if request.method == "POST":
        request.max_content_length = settings.get_int("max_upload_size_mb") * 1024 * 1024


@meetings_bp.route("/")
@login_required
def list_meetings():
    query = _visible_meetings()

    status = request.args.get("status", "").strip()
    if status:
        query = query.filter(Meeting.status == status)

    search = request.args.get("q", "").strip()
    if search:
        pattern = f"%{search}%"
        query = query.filter(or_(Meeting.title.ilike(pattern), Meeting.original_filename.ilike(pattern)))

    meetings = query.order_by(Meeting.created_at.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=PER_PAGE, error_out=False
    )
    return render_template("meetings/list.html", meetings=meetings, status_filter=status, search=search)


@meetings_bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    max_mb = settings.get_int("max_upload_size_mb")
    allowed = sorted(current_app.config["ALLOWED_EXTENSIONS"])

    if request.method == "POST":
        upload_file = request.files.get("file")
        if not upload_file or not upload_file.filename:
            flash("Choose a recording to upload.", "error")
            return redirect(request.url)

        original = secure_filename(upload_file.filename)
        extension = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        if extension not in current_app.config["ALLOWED_EXTENSIONS"]:
            flash(f"That file type is not supported. Allowed: {', '.join(allowed)}.", "error")
            return redirect(request.url)

        stored = f"{uuid.uuid4().hex}.{extension}"
        path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored)
        upload_file.save(path)

        meeting = Meeting(
            user_id=current_user.id,
            title=request.form.get("title", "").strip() or os.path.splitext(original)[0],
            original_filename=original,
            stored_filename=stored,
            file_size=os.path.getsize(path),
            language=_chosen_language(),
            status="queued",
            expires_at=utcnow() + timedelta(days=max(1, settings.get_int("meeting_retention_days"))),
        )
        db.session.add(meeting)
        db.session.commit()
        meeting.log("Uploaded")

        if not _enqueue(meeting):
            db.session.commit()
            flash("Upload saved, but the job queue is unreachable. Start Redis, then retry.", "error")
            return redirect(url_for("meetings.detail", meeting_id=meeting.id))

        db.session.commit()
        MEETINGS_UPLOADED.inc()
        flash(f'"{meeting.title}" is queued for processing.', "success")
        return redirect(url_for("meetings.detail", meeting_id=meeting.id))

    return render_template("meetings/upload.html", max_mb=max_mb, allowed=allowed)


@meetings_bp.route("/<int:meeting_id>")
@login_required
def detail(meeting_id: int):
    meeting = _get_meeting(meeting_id)
    return render_template("meetings/detail.html", meeting=meeting, events=_recent_events(meeting))


@meetings_bp.route("/<int:meeting_id>/retry", methods=["POST"])
@login_required
def retry(meeting_id: int):
    meeting = _get_meeting(meeting_id)
    if meeting.is_active:
        flash("That meeting is already being processed.", "info")
        return redirect(url_for("meetings.detail", meeting_id=meeting.id))
    if not meeting.has_source_file:
        flash("The recording has been deleted, so it cannot be processed again.", "error")
        return redirect(url_for("meetings.detail", meeting_id=meeting.id))

    meeting.status = "queued"
    meeting.progress = 0
    meeting.error_message = None
    meeting.cancel_requested = False
    meeting.finished_at = None
    meeting.log("Queued again")

    if not _enqueue(meeting):
        db.session.commit()
        flash("The job queue is unreachable. Start Redis and try again.", "error")
    else:
        db.session.commit()
        flash("Processing restarted.", "success")
    return redirect(url_for("meetings.detail", meeting_id=meeting.id))


@meetings_bp.route("/<int:meeting_id>/cancel", methods=["POST"])
@login_required
def cancel(meeting_id: int):
    meeting = _get_meeting(meeting_id)
    if not meeting.is_active:
        flash("That meeting is not being processed.", "info")
        return redirect(url_for("meetings.detail", meeting_id=meeting.id))

    meeting.cancel_requested = True
    meeting.log("Cancellation requested")
    db.session.commit()
    if meeting.task_id:
        try:
            celery.control.revoke(meeting.task_id)
        except Exception as exc:
            log.warning("Could not revoke task %s: %s", meeting.task_id, exc)

    flash("Cancelling. The job stops at the end of the current step.", "info")
    return redirect(url_for("meetings.detail", meeting_id=meeting.id))


@meetings_bp.route("/<int:meeting_id>/delete", methods=["POST"])
@login_required
def delete(meeting_id: int):
    meeting = _get_meeting(meeting_id)
    if meeting.is_active:
        flash("Cancel the processing before deleting this meeting.", "error")
        return redirect(url_for("meetings.detail", meeting_id=meeting.id))
    remove_recording(meeting)
    db.session.delete(meeting)
    db.session.commit()
    flash("Meeting deleted.", "success")
    return redirect(url_for("meetings.list_meetings"))


@meetings_bp.route("/delete-all", methods=["POST"])
@login_required
def delete_all():
    meetings = _visible_meetings().all()
    removable = [meeting for meeting in meetings if meeting.status not in ACTIVE_STATUSES]
    for meeting in removable:
        remove_recording(meeting)
        db.session.delete(meeting)
    db.session.commit()

    skipped = len(meetings) - len(removable)
    if skipped:
        flash(
            f"Deleted {len(removable)} meeting{'s' if len(removable) != 1 else ''}. "
            f"{skipped} still processing {'was' if skipped == 1 else 'were'} left alone.",
            "info",
        )
    else:
        flash(f"Deleted {len(removable)} meeting{'s' if len(removable) != 1 else ''}.", "success")
    return redirect(url_for("meetings.list_meetings"))


@meetings_bp.route("/<int:meeting_id>/download/<kind>")
@login_required
def download(meeting_id: int, kind: str):
    meeting = _get_meeting(meeting_id)
    if kind not in DOWNLOADS:
        abort(404)
    source, extension, mimetype = DOWNLOADS[kind]
    text = getattr(meeting, source)
    if not text:
        abort(404)

    if extension in ("pdf", "docx"):
        from app.utils.export import minutes_to_docx, minutes_to_pdf
        render = minutes_to_pdf if extension == "pdf" else minutes_to_docx
        body = render(text, meeting.title, _export_details(meeting))
        log.info("Exported meeting %s as %s", meeting.id, extension)
    else:
        body = text

    label = "transcript" if source == "transcript" else "minutes"
    filename = f"{secure_filename(meeting.title) or 'meeting'}-{label}.{extension}"
    return Response(body, mimetype=mimetype, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _export_details(meeting: Meeting) -> str:
    """The line under the title: when, how long, and (for developers) what produced the minutes."""
    parts = []
    if meeting.created_at:
        parts.append(meeting.created_at.strftime("%d %b %Y, %H:%M"))
    if meeting.duration_seconds:
        parts.append(meeting.duration_label)
    if meeting.llm_model and current_app.config["DEVELOPER_VIEW"]:
        parts.append(f"summarised with {meeting.llm_model}")
    return " · ".join(parts)


def _chosen_language() -> str | None:
    """The language picked on the upload page, if it is one on offer.

    Anything else (no choice offered, or a forged value) falls back to the first
    language offered, or to None, which means the Settings language.
    """
    offered = current_app.config["MEETING_LANGUAGES"]
    codes = [code for code, _ in offered]
    chosen = request.form.get("language", "").strip().lower()
    if chosen in codes:
        return chosen
    return codes[0] if codes else None


def _visible_meetings():
    query = db.session.query(Meeting)
    return query if current_user.is_admin else query.filter(Meeting.user_id == current_user.id)


def _get_meeting(meeting_id: int) -> Meeting:
    meeting = _visible_meetings().filter(Meeting.id == meeting_id).first()
    if meeting is None:
        abort(404)
    return meeting


def _recent_events(meeting: Meeting, limit: int = 40):
    from app.models import MeetingEvent
    return (
        meeting.events.order_by(MeetingEvent.created_at.desc(), MeetingEvent.id.desc())
        .limit(limit)
        .all()
    )


def _enqueue(meeting: Meeting) -> bool:
    """Reserve the task id before queueing so status polling works immediately."""
    from app.tasks.process import process_meeting

    task_id = uuid.uuid4().hex
    meeting.task_id = task_id

    if os.environ.get("MOM_LOCAL_TASKS") == "1":
        app_obj = current_app._get_current_object()
        meeting_id = meeting.id

        def _run_local() -> None:
            deadline = time.time() + 30
            while time.time() < deadline:
                with app_obj.app_context():
                    if db.session.get(Meeting, meeting_id) is not None:
                        break
                time.sleep(0.25)
            with app_obj.app_context():
                try:
                    process_meeting.run(meeting_id)
                except Exception:
                    log.exception("Local processing of meeting %s failed", meeting_id)

        threading.Thread(target=_run_local, daemon=True).start()
        return True

    try:
        process_meeting.apply_async(args=[meeting.id], task_id=task_id)
        return True
    except Exception as exc:
        log.error("Could not queue meeting %s: %s", meeting.id, exc)
        meeting.status = "failed"
        meeting.error_message = f"The job queue is unreachable: {exc}"
        meeting.log(meeting.error_message, level="error")
        return False
