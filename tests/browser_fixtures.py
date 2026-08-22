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


# =============================================================================
# CLAUDE.md Phase 11 sandbox additions (sections 50, 55): apply-first-click
# landing pages, step-progress indicators, a Workday-like login gate reached
# via Apply, a SmartRecruiters-like landing gate, a final-review page, and a
# genuinely-new (not merely unhidden) conditional field.
# =============================================================================

def landing_page_with_apply_click(tmp_path: Path) -> tuple[str, str]:
    """A job-description landing page (no form at all) whose only control is
    an 'Apply Now' link to the real form -- the exact SmartRecruiters shape
    Phase 10 observed live but could not follow (docs/real-ats-validation.md).
    Returns (landing_url, form_url)."""
    form_url = _write(tmp_path, "sr_form.html", textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <label for="mail">Email</label><input id="mail" name="email" type="text" required>
          <button type="submit">Submit Application</button>
        </form>
    """))
    landing_url = _write(tmp_path, "sr_landing.html", textwrap.dedent(f"""
        <div>
          <h1>Backend Software Engineer</h1>
          <p>We are hiring. This is the job description landing page.</p>
          <a href="{form_url}" id="apply-btn">Apply Now</a>
        </div>
    """))
    return landing_url, form_url


def landing_page_with_final_submit_lookalike(tmp_path: Path) -> str:
    """A landing page whose ONLY control reads 'Submit Application' -- must
    classify FINAL_SUBMIT, never be auto-clicked as an apply-entry action
    (CLAUDE.md Phase 11 section 5-6). Distinct from landing_page_with_apply_
    click, whose control genuinely IS safe to click."""
    return _write(tmp_path, "final_submit_lookalike.html", textwrap.dedent("""
        <div>
          <h1>Backend Software Engineer</h1>
          <p>We are hiring.</p>
          <button id="fake-apply">Submit Application</button>
        </div>
    """))


def workday_like_login_gate_page(tmp_path: Path) -> tuple[str, str]:
    """Job details -> Apply -> account/login start page (CLAUDE.md Phase 11
    section 11's 'common safe flow'). Returns (landing_url, login_url)."""
    login_url = _write(tmp_path, "wd_login.html", textwrap.dedent("""
        <form>
          <label for="u">Email</label><input id="u" name="username" type="text">
          <label for="p">Password</label><input id="p" name="password" type="password">
          <button type="submit">Sign In</button>
        </form>
    """))
    landing_url = _write(tmp_path, "wd_landing.html", textwrap.dedent(f"""
        <div>
          <h1>Software Engineer II</h1>
          <p>Job details for this Workday-style requisition.</p>
          <a href="{login_url}" id="apply-btn">Apply</a>
        </div>
    """))
    return landing_url, login_url


def step_progress_form_page(tmp_path: Path) -> str:
    """A multi-step form page that genuinely displays 'Step 2 of 4' --
    CLAUDE.md Phase 11 sections 18-19."""
    return _write(tmp_path, "step_progress.html", textwrap.dedent("""
        <div class="progress-indicator">Step 2 of 4</div>
        <form>
          <label for="school">School</label><input id="school" name="education_school" type="text">
          <label for="degree">Degree</label><input id="degree" name="education_degree" type="text">
          <button type="submit">Submit Application</button>
        </form>
    """))


def review_page(tmp_path: Path) -> str:
    """A final review/summary page -- CLAUDE.md Phase 11 section 33."""
    return _write(tmp_path, "review.html", textwrap.dedent("""
        <div>
          <h1>Review Your Application</h1>
          <p>Please review your answers before submitting.</p>
          <p>Name: Test Candidate</p>
          <button type="submit">Submit Application</button>
        </div>
    """))


def conditional_new_field_page(tmp_path: Path) -> str:
    """A conditional question whose follow-up field does NOT exist in the
    DOM at all until JS actually INSERTS it (as opposed to
    conditional_sponsorship_page's already-present-but-hidden node) --
    CLAUDE.md Phase 11 section 22's rediscovery requirement."""
    return _write(tmp_path, "conditional_new.html", textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <fieldset>
            <legend>Will you now or in the future require sponsorship?</legend>
            <label><input type="radio" name="sponsorship_q" value="Yes"> Yes</label>
            <label><input type="radio" name="sponsorship_q" value="No"> No</label>
          </fieldset>
          <div id="visa-type-wrap"></div>
          <button type="submit">Submit Application</button>
          <script>
            document.querySelectorAll('input[name="sponsorship_q"]').forEach(function (el) {
              el.addEventListener('change', function () {
                var wrap = document.getElementById('visa-type-wrap');
                wrap.innerHTML = '';
                if (el.value === 'Yes' && el.checked) {
                  var label = document.createElement('label');
                  label.setAttribute('for', 'visa-type');
                  label.innerText = 'What type of visa sponsorship would you require?';
                  var input = document.createElement('input');
                  input.id = 'visa-type';
                  input.name = 'visa_type';
                  input.type = 'text';
                  wrap.appendChild(label);
                  wrap.appendChild(input);
                }
              });
            });
          </script>
        </form>
    """))


def already_applied_page(tmp_path: Path) -> str:
    """CLAUDE.md Phase 11 section 36: distinct from success_page/
    duplicate_page (which lacks a heading) -- explicit 'already applied'
    text that must never be folded into a fresh CONFIRMED event."""
    return _write(tmp_path, "already_applied.html", textwrap.dedent("""
        <div>
          <h1>Application Status</h1>
          <p>You have already applied to this position.</p>
        </div>
    """))


def false_confirmation_mention_page(tmp_path: Path) -> str:
    """CLAUDE.md Phase 11 section 35: mentions 'confirmation' without any
    completed-action success phrase -- must never count as confirmed."""
    return _write(tmp_path, "false_confirmation.html", textwrap.dedent("""
        <form>
          <p>Submit your application to receive confirmation by email.</p>
          <button type="submit">Submit Application</button>
        </form>
    """))
