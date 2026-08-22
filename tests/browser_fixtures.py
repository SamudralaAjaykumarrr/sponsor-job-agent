"""CLAUDE.md Phase 10 section 55: a small, realistic local mock-ATS browser
sandbox used ONLY by `@pytest.mark.browser` tests. Every page is static
HTML served via `file://` -- never a real website, never requires internet
access. Mirrors real ATS form shapes closely enough to exercise
app.applications.browser_runtime's actual DOM-scanning/filling/navigation
code against a real (if synthetic) browser session."""

import textwrap
from pathlib import Path


def _write(tmp_path: Path, name: str, html: str) -> str:
    path = tmp_path / name
    path.write_text(f"<!doctype html><html><body>{html}</body></html>")
    return path.as_uri()


def simple_form_page(tmp_path: Path) -> str:
    return _write(tmp_path, "simple.html", textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <label for="mail">Email</label><input id="mail" name="email" type="text" required>
          <button type="submit">Submit Application</button>
        </form>
    """))


def login_page(tmp_path: Path) -> str:
    return _write(tmp_path, "login.html", textwrap.dedent("""
        <form>
          <label for="u">Username</label><input id="u" name="username" type="text">
          <label for="p">Password</label><input id="p" name="password" type="password">
          <button type="submit">Log In</button>
        </form>
    """))


def captcha_page(tmp_path: Path) -> str:
    return _write(tmp_path, "captcha.html", textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text">
          <div class="g-recaptcha" data-sitekey="fake-test-key">Please verify you are human (captcha).</div>
          <button type="submit">Submit Application</button>
        </form>
    """))


def legal_question_page(tmp_path: Path) -> str:
    return _write(tmp_path, "legal.html", textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <label for="felony">Have you ever been convicted of a felony?</label>
          <input id="felony" name="criminal_history" type="text" required>
          <button type="submit">Submit Application</button>
        </form>
    """))


def unknown_field_page(tmp_path: Path) -> str:
    return _write(tmp_path, "unknown_field.html", textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <label for="referral">How did you originally hear about this very specific referral program?</label>
          <input id="referral" name="referral_source_code" type="text" required>
          <button type="submit">Submit Application</button>
        </form>
    """))


def conditional_sponsorship_page(tmp_path: Path) -> str:
    """A visa-type text field that only becomes required/visible once "Yes"
    is chosen -- CLAUDE.md Phase 10 section 11."""
    return _write(tmp_path, "conditional.html", textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <fieldset>
            <legend>Will you now or in the future require sponsorship?</legend>
            <label><input type="radio" name="sponsorship_q" value="Yes"> Yes</label>
            <label><input type="radio" name="sponsorship_q" value="No"> No</label>
          </fieldset>
          <div id="visa-type-wrap" style="display:none;">
            <label for="visa-type">What type of visa sponsorship would you require?</label>
            <input id="visa-type" name="visa_type" type="text">
          </div>
          <button type="submit">Submit Application</button>
          <script>
            document.querySelectorAll('input[name="sponsorship_q"]').forEach(function (el) {
              el.addEventListener('change', function () {
                document.getElementById('visa-type-wrap').style.display = (el.value === 'Yes' && el.checked) ? 'block' : 'none';
              });
            });
          </script>
        </form>
    """))


def multi_step_pages(tmp_path: Path) -> tuple[str, str]:
    """Two real, separately-loaded pages linked by a "Next" control -- closer
    to how real multi-page ATS forms (e.g. Workday) behave than a single
    page with JS show/hide panels."""
    page2 = _write(tmp_path, "step2.html", textwrap.dedent("""
        <form>
          <label for="school">School</label><input id="school" name="education_school" type="text">
          <label for="degree">Degree</label><input id="degree" name="education_degree" type="text">
          <button type="submit">Submit Application</button>
        </form>
    """))
    page1 = _write(tmp_path, "step1.html", textwrap.dedent(f"""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <label for="mail">Email</label><input id="mail" name="email" type="text" required>
          <button type="button" onclick="window.location.href='{page2}'">Next</button>
        </form>
    """))
    return page1, page2


def success_page(tmp_path: Path) -> str:
    return _write(tmp_path, "success.html", textwrap.dedent("""
        <div>
          <h1>Thank you for applying!</h1>
          <p>Your application has been submitted. Confirmation Number: ABC-1234-XYZ</p>
        </div>
    """))


def duplicate_page(tmp_path: Path) -> str:
    return _write(tmp_path, "duplicate.html", textwrap.dedent("""
        <div><p>Our records show you have already applied to this position.</p></div>
    """))


def form_with_file_upload_page(tmp_path: Path) -> str:
    return _write(tmp_path, "upload.html", textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <label for="resume">Resume/CV</label><input id="resume" name="resume" type="file" required>
          <button type="submit">Submit Application</button>
        </form>
    """))
