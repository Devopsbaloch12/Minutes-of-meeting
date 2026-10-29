import pytest

from app import settings


@pytest.fixture()
def simple(signed_in, app):
    app.config["DEVELOPER_VIEW"] = False
    return signed_in


def test_simple_settings_show_only_the_minutes_prompt(simple):
    body = simple.get("/admin/settings").get_data(as_text=True)
    assert 'name="minutes_prompt"' in body
    for hidden in ("llm_base_url", "llm_api_key", "deepgram_api_key", "transcript_chunk_chars",
                   "max_upload_size_mb", "/admin/settings/test/"):
        assert hidden not in body
    assert "/admin/system" not in body  # no System entry in the sidebar either


def test_simple_settings_change_the_prompt_and_nothing_else(simple, app):
    settings.save({"deepgram_api_key": "dg-key", "llm_model": "gemma4:31b", "transcript_chunk_chars": "8000"})
    response = simple.post("/admin/settings", data={
        "minutes_prompt": "## Summary\nKeep it short.",
        # a crafted form cannot reach the hidden settings
        "llm_model": "other-model", "deepgram_api_key": "", "deepgram_api_key__clear": "on", "group": "speech",
    })
    assert response.status_code == 302
    with app.app_context():
        assert settings.get("minutes_prompt") == "## Summary\nKeep it short."
        assert settings.get("llm_model") == "gemma4:31b"
        assert settings.get("deepgram_api_key") == "dg-key"
        assert settings.get("transcript_chunk_chars") == "8000"


def test_an_empty_prompt_is_refused_and_the_default_can_be_restored(simple, app):
    settings.save({"minutes_prompt": "## Summary\nCustom."})
    simple.post("/admin/settings", data={"minutes_prompt": "   "})
    with app.app_context():
        assert settings.get("minutes_prompt") == "## Summary\nCustom."

    assert "Restore default" in simple.get("/admin/settings").get_data(as_text=True)
    simple.post("/admin/settings", data={"minutes_prompt": "ignored", "restore_default": "1"})
    with app.app_context():
        assert settings.get("minutes_prompt") == settings.DEFAULT_MINUTES_PROMPT
    assert "Restore default" not in simple.get("/admin/settings").get_data(as_text=True)


def test_simple_mode_closes_the_system_page_and_provider_tests(simple):
    assert simple.get("/admin/system").status_code == 404
    assert simple.post("/admin/settings/test/llm").status_code == 404
    assert simple.get("/admin/users").status_code == 200


def test_advanced_settings_stay_the_default(signed_in):
    body = signed_in.get("/admin/settings").get_data(as_text=True)
    assert 'name="llm_base_url"' in body and 'name="deepgram_api_key"' in body
    assert "/admin/system" in body
    assert signed_in.get("/admin/system").status_code == 200


@pytest.fixture()
def processed(signed_in):
    import io

    from app.extensions import db
    from app.models import Meeting

    signed_in.post("/meetings/upload", data={"file": (io.BytesIO(b"audio"), "standup.mp3"), "title": "Standup"},
                   content_type="multipart/form-data")
    meeting = db.session.query(Meeting).one()
    meeting.status, meeting.llm_model, meeting.speech_provider = "failed", "gemma4:31b", "deepgram"
    meeting.minutes_of_meeting = "## Summary\n\nDone."
    meeting.error_message = "gemma4:31b stopped at the Max output tokens limit (4000)"
    meeting.log("Writing minutes with gemma4:31b")
    db.session.commit()
    return meeting


def test_meeting_pages_keep_the_machinery_out_of_sight(simple, processed):
    body = simple.get(f"/meetings/{processed.id}").get_data(as_text=True)
    assert "Activity" not in body and "Writing minutes with" not in body
    assert "gemma4" not in body and "deepgram" not in body and "Max output tokens" not in body
    assert "Run again" in body and "could not be written" in body
    assert "v3.0" not in body  # nor the version in the sidebar

    status = simple.get(f"/api/v1/meetings/{processed.id}/status").get_json()
    assert status["events"] == [] and status["error_message"] is None and status["status"] == "failed"


def test_exports_leave_out_the_model_outside_the_developer_view(simple, processed, app):
    from docx import Document
    import io

    def header(client):
        document = Document(io.BytesIO(client.get(f"/meetings/{processed.id}/download/docx").data))
        return document.paragraphs[1].text

    assert "gemma4" not in header(simple)
    app.config["DEVELOPER_VIEW"] = True
    assert "summarised with gemma4:31b" in header(simple)


def test_the_developer_view_still_shows_everything(signed_in, processed):
    body = signed_in.get(f"/meetings/{processed.id}").get_data(as_text=True)
    assert "Activity" in body and "Writing minutes with gemma4:31b" in body
    assert "Max output tokens" in body and "summarised with gemma4:31b" in body
    assert signed_in.get(f"/api/v1/meetings/{processed.id}/status").get_json()["events"]


def test_a_configured_name_and_logo_replace_the_default_branding(signed_in, app):
    app.config.update(APP_NAME="IPS Meeting Minutes", APP_LOGO="brand/ips-logo.png")

    page = signed_in.get("/").get_data(as_text=True)
    assert "IPS Meeting Minutes" in page and "MoM AI" not in page
    assert page.count('src="/static/brand/ips-logo.png"') == 1  # sidebar
    assert '<link rel="icon" href="/static/brand/ips-logo.png" />' in page
    assert "<title>Dashboard | IPS Meeting Minutes</title>" in page

    signed_in.get("/auth/logout")
    login = signed_in.get("/auth/login").get_data(as_text=True)
    assert 'class="brand-logo-lg"' in login and "IPS Meeting Minutes" in login
    assert signed_in.get("/static/brand/ips-logo.png").status_code == 200


def test_pages_carry_no_logo(signed_in, app):
    page = signed_in.get("/").get_data(as_text=True)
    assert "bg-brand-600 rounded-lg" not in page  # the sidebar mark
    assert "favicon" not in page and '<link rel="icon" href="data:," />' in page  # nor a browser-tab icon

    signed_in.get("/auth/logout")
    login = signed_in.get("/auth/login").get_data(as_text=True)
    assert "MoM AI" in login and "bg-brand-600 rounded-2xl" not in login
