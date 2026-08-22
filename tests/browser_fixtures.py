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


# CLAUDE.md Phase 13 acceptance correction (sections 4, 9-10): every
# `tests/test_browser_assist_*_e2e.py` file's `_prepared()` fixture opens its
# job with this exact title/company -- these are the "genuine" identity
# signals for a fixture-driven session. Real FORM pages below embed this as
# a schema.org JobPosting JSON-LD block (the same standard mechanism
# app.applications.browser_runtime._extract_observed_job_meta reads) so a
# fixture whose entire point is testing something ELSE (multi-step, iframe,
# shadow-DOM, legal questions, duplicate detection, ...) reaches a VERIFIED
# identity and is not incidentally blocked by the job-identity gate. Tests
# that ARE about the identity gate (see tests/test_browser_assist_phase13_e2e.py)
# use `jsonld_job_posting_page()` directly with an explicit, possibly
# DIFFERENT company/title instead of this default.
DEFAULT_JOB_TITLE = "Backend Software Engineer"
DEFAULT_JOB_COMPANY = "Acme Corp"


def _jsonld_block(title: str = DEFAULT_JOB_TITLE, company: str = DEFAULT_JOB_COMPANY) -> str:
    return textwrap.dedent(f"""
        <script type="application/ld+json">
        {{"@context": "https://schema.org/", "@type": "JobPosting", "title": "{title}",
          "hiringOrganization": {{"@type": "Organization", "name": "{company}"}}}}
        </script>
    """)


def simple_form_page(tmp_path: Path) -> str:
    return _write(tmp_path, "simple.html", _jsonld_block() + textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <label for="mail">Email</label><input id="mail" name="email" type="text" required>
          <button type="submit">Submit Application</button>
        </form>
    """))


def simple_form_page_no_identity(tmp_path: Path) -> str:
    """Same shape as simple_form_page but deliberately carries NO JSON-LD
    (or any other identity signal) -- used to test the genuinely
    INSUFFICIENT verdict path (CLAUDE.md Phase 13 acceptance correction),
    distinct from every other fixture in this module, which embeds the
    default JobPosting block so tests about OTHER mechanisms aren't
    incidentally blocked by the identity gate."""
    return _write(tmp_path, "simple_no_identity.html", textwrap.dedent("""
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
    return _write(tmp_path, "legal.html", _jsonld_block() + textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <label for="felony">Have you ever been convicted of a felony?</label>
          <input id="felony" name="criminal_history" type="text" required>
          <button type="submit">Submit Application</button>
        </form>
    """))


def unknown_field_page(tmp_path: Path) -> str:
    return _write(tmp_path, "unknown_field.html", _jsonld_block() + textwrap.dedent("""
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
    return _write(tmp_path, "conditional.html", _jsonld_block() + textwrap.dedent("""
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
    page2 = _write(tmp_path, "step2.html", _jsonld_block() + textwrap.dedent("""
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
    return _write(tmp_path, "upload.html", _jsonld_block() + textwrap.dedent("""
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
    form_url = _write(tmp_path, "sr_form.html", _jsonld_block() + textwrap.dedent("""
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
    return _write(tmp_path, "step_progress.html", _jsonld_block() + textwrap.dedent("""
        <div class="progress-indicator">Step 2 of 4</div>
        <form>
          <label for="school">School</label><input id="school" name="education_school" type="text">
          <label for="degree">Degree</label><input id="degree" name="education_degree" type="text">
          <button type="submit">Submit Application</button>
        </form>
    """))


def review_page(tmp_path: Path) -> str:
    """A final review/summary page -- CLAUDE.md Phase 11 section 33."""
    return _write(tmp_path, "review.html", _jsonld_block() + textwrap.dedent("""
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
    return _write(tmp_path, "conditional_new.html", _jsonld_block() + textwrap.dedent("""
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


# =============================================================================
# CLAUDE.md Phase 12 sections 58-62: JS-rendered SPA fixtures. A genuinely
# JS-delayed Apply control, a client-side route/DOM-replacement transition
# (no full page load), and dynamic form mounting -- the exact SPA shape
# Phase 10/11 could not reach on real SmartRecruiters postings, reproduced
# here as a deterministic local fixture so the generic SPA-hardening logic
# can be tested without depending on any specific real posting's HTML
# surviving unchanged.
# =============================================================================

def smartrecruiters_like_spa_page(tmp_path: Path, *, apply_delay_ms: int = 150) -> str:
    """A single file:// page that behaves like a client-side-rendered
    SmartRecruiters posting: the job description renders immediately, but
    the 'Apply Now' control is inserted by JS after `apply_delay_ms`, and
    clicking it performs a `history.pushState` route change (no real
    navigation) plus swaps in a genuinely new application form via
    `innerHTML` -- including a resume upload field, a 'Next' step, and a
    final 'Submit Application' control. Exercises: dynamic apply-control
    discovery, SPA route detection, dynamic form mounting, resume upload,
    multi-step, final-submit boundary -- all in one page, matching CLAUDE.md
    section 58."""
    return _write(tmp_path, "sr_spa.html", _jsonld_block() + textwrap.dedent(f"""
        <div id="app">
          <h1>Backend Software Engineer</h1>
          <p>We are hiring. This is the job description landing page.</p>
          <div id="apply-slot"></div>
        </div>
        <script>
          setTimeout(function () {{
            var a = document.createElement('a');
            a.id = 'apply-btn';
            a.href = '#apply';
            a.innerText = 'Apply Now';
            a.addEventListener('click', function (e) {{
              e.preventDefault();
              history.pushState({{}}, '', '#apply');
              document.getElementById('app').innerHTML = (
                '<form id="step1">' +
                '<div class="progress-indicator">Step 1 of 2</div>' +
                '<label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>' +
                '<label for="mail">Email</label><input id="mail" name="email" type="text" required>' +
                '<button type="button" id="next-btn">Next</button>' +
                '</form>'
              );
              document.getElementById('next-btn').addEventListener('click', function () {{
                document.getElementById('app').innerHTML = (
                  '<form id="step2">' +
                  '<div class="progress-indicator">Step 2 of 2</div>' +
                  '<label for="resume">Resume/CV</label><input id="resume" name="resume" type="file" required>' +
                  '<button type="submit">Submit Application</button>' +
                  '</form>'
                );
              }});
            }});
            document.getElementById('apply-slot').appendChild(a);
          }}, {apply_delay_ms});
        </script>
    """))


def smartrecruiters_like_never_renders_page(tmp_path: Path) -> str:
    """A landing page whose apply control never actually appears (simulates
    a genuinely unreachable/broken SPA render) -- used to verify the bounded
    DOM-stabilization wait times out cleanly rather than hanging."""
    return _write(tmp_path, "sr_spa_never.html", textwrap.dedent("""
        <div>
          <h1>Backend Software Engineer</h1>
          <p>We are hiring.</p>
        </div>
    """))


def workday_like_progress_wizard_page(tmp_path: Path) -> tuple[str, str]:
    """CLAUDE.md Phase 12 section 59: a Workday-like fixture with a delayed-
    hydration landing page, an Apply control, and a genuine 'Step 2 of 3'
    progress wizard on the form page it leads to. Returns (landing_url,
    form_url)."""
    form_url = _write(tmp_path, "wd_wizard_form.html", _jsonld_block() + textwrap.dedent("""
        <div class="progress-indicator" role="progressbar">Step 2 of 3</div>
        <form>
          <label for="school">School</label><input id="school" name="education_school" type="text">
          <label for="resume">Resume/CV</label><input id="resume" name="resume" type="file">
          <button type="submit">Submit Application</button>
        </form>
    """))
    landing_url = _write(tmp_path, "wd_wizard_landing.html", textwrap.dedent(f"""
        <div id="app">
          <h1>Software Engineer II</h1>
          <p>Job details for this Workday-style requisition. Posted 7/31.</p>
        </div>
        <script>
          setTimeout(function () {{
            var a = document.createElement('a');
            a.id = 'apply-btn';
            a.href = '{form_url}';
            a.innerText = 'Apply';
            document.getElementById('app').appendChild(a);
          }}, 150);
        </script>
    """))
    return landing_url, form_url


def multiple_apply_controls_same_destination_page(tmp_path: Path) -> tuple[str, str]:
    """CLAUDE.md Phase 12 sections 36-37: top AND bottom 'Apply Now' buttons
    pointing at the SAME form -- the ordinary sticky/repeated-button
    pattern, never ambiguous."""
    form_url = _write(tmp_path, "multi_ctrl_form.html", textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <button type="submit">Submit Application</button>
        </form>
    """))
    landing_url = _write(tmp_path, "multi_ctrl_landing.html", textwrap.dedent(f"""
        <div>
          <a href="{form_url}" id="apply-top">Apply Now</a>
          <h1>Backend Software Engineer</h1>
          <p>We are hiring.</p>
          <a href="{form_url}" id="apply-bottom">Apply Now</a>
        </div>
    """))
    return landing_url, form_url


def multiple_apply_controls_different_destination_page(tmp_path: Path) -> tuple[str, str, str]:
    """CLAUDE.md Phase 12 sections 36-37: an Apply control for THIS job and
    a second one for a genuinely different "similar job" recommendation --
    must never be resolved by guessing. Returns (landing_url, this_job_form,
    other_job_form)."""
    this_form = _write(tmp_path, "this_job_form.html", textwrap.dedent("""
        <form><label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
        <button type="submit">Submit Application</button></form>
    """))
    other_form = _write(tmp_path, "other_job_form.html", textwrap.dedent("""
        <form><label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
        <button type="submit">Submit Application</button></form>
    """))
    landing_url = _write(tmp_path, "ambiguous_landing.html", textwrap.dedent(f"""
        <div>
          <h1>Backend Software Engineer</h1>
          <a href="{this_form}" id="apply-this">Apply Now</a>
          <h2>Similar Jobs</h2>
          <p>Frontend Software Engineer</p>
          <a href="{other_form}" id="apply-other">Apply Now</a>
        </div>
    """))
    return landing_url, this_form, other_form


def iframe_form_page(tmp_path: Path) -> str:
    """CLAUDE.md Phase 12 section 14, 61: the real application form lives
    inside an iframe (a real ATS pattern, e.g. an embedded application
    widget) rather than the top-level document. Same-origin (file://, like
    the parent) -- must be discovered and filled exactly like a top-level
    form."""
    inner_url = _write(tmp_path, "iframe_inner_form.html", textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <label for="mail">Email</label><input id="mail" name="email" type="text" required>
          <button type="submit">Submit Application</button>
        </form>
    """))
    outer_url = _write(tmp_path, "iframe_outer.html", _jsonld_block() + textwrap.dedent(f"""
        <div>
          <h1>Backend Software Engineer</h1>
          <iframe id="app-frame" src="{inner_url}" style="width:600px;height:400px;"></iframe>
        </div>
    """))
    return outer_url


def no_iframe_form_page(tmp_path: Path) -> str:
    """A plain top-level form (no iframe at all) -- the control case for
    iframe-scan tests, confirming the iframe scan never breaks ordinary
    top-level discovery."""
    return _write(tmp_path, "no_iframe.html", _jsonld_block() + textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <button type="submit">Submit Application</button>
        </form>
    """))


def shadow_dom_form_page(tmp_path: Path) -> str:
    """CLAUDE.md Phase 12 sections 15, 62: the application form is mounted
    inside an OPEN shadow root (a real pattern for web-component-based ATS
    widgets) -- must be discovered via the deep-query shadow-piercing scan,
    never bypassing a CLOSED root (not exercised here, since that would
    require attempting a genuine bypass this project never performs)."""
    return _write(tmp_path, "shadow_form.html", _jsonld_block() + textwrap.dedent("""
        <div id="host"></div>
        <script>
          var host = document.getElementById('host');
          var root = host.attachShadow({mode: 'open'});
          root.innerHTML = (
            '<form>' +
            '<label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>' +
            '<button type="submit">Submit Application</button>' +
            '</form>'
          );
        </script>
    """))


def closed_shadow_dom_form_page(tmp_path: Path) -> str:
    """A CLOSED shadow root -- must remain genuinely undiscoverable (the
    honest UNSUPPORTED outcome, never a bypass attempt)."""
    return _write(tmp_path, "closed_shadow_form.html", textwrap.dedent("""
        <div id="host"></div>
        <script>
          var host = document.getElementById('host');
          var root = host.attachShadow({mode: 'closed'});
          root.innerHTML = (
            '<form>' +
            '<label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>' +
            '<button type="submit">Submit Application</button>' +
            '</form>'
          );
        </script>
    """))


def jsonld_job_posting_page(tmp_path: Path, *, title: str = "Backend Software Engineer",
                             company: str = "Acme Corp", identifier: str = "", location: str = "") -> str:
    """CLAUDE.md Phase 13 sections 4, 8, 72: a real application FORM page
    that also carries a schema.org JobPosting JSON-LD block -- the same
    standard mechanism search engines use, and the source
    app.applications.browser_runtime._extract_observed_job_meta reads for
    the formal multi-signal job-identity check. `identifier` lets a test
    supply a requisition-id-shaped value distinct from the URL itself;
    `location` (jobLocation.address.addressLocality) lets a test exercise
    the weak, corroborating-only location signal."""
    ident_json = f'"identifier": {{"@type": "PropertyValue", "value": "{identifier}"}},' if identifier else ""
    location_json = (
        f'"jobLocation": {{"@type": "Place", "address": {{"addressLocality": "{location}"}}}},'
        if location else ""
    )
    return _write(tmp_path, "jsonld_form.html", textwrap.dedent(f"""
        <script type="application/ld+json">
        {{
          "@context": "https://schema.org/",
          "@type": "JobPosting",
          "title": "{title}",
          {ident_json}
          {location_json}
          "hiringOrganization": {{"@type": "Organization", "name": "{company}"}}
        }}
        </script>
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <label for="resume">Resume/CV</label><input id="resume" name="resume" type="file">
          <button type="submit">Submit Application</button>
        </form>
    """))


def job_identity_pages(tmp_path: Path) -> tuple[str, str]:
    """CLAUDE.md Phase 12 sections 37-39: two REAL, independently reachable
    pages representing two DIFFERENT job requisitions (a query-string
    `job_id` token, since file:// fixtures can't carry a real Workday-style
    path segment across two independently-written files) -- used to open a
    session at the first and simulate the live page ending up on the
    second, verifying the job-identity-mismatch pause fires. Returns
    (original_url_with_token, other_job_url_with_token)."""
    original = _write(tmp_path, "job_a_landing.html", textwrap.dedent("""
        <div><h1>Backend Software Engineer (Job A)</h1></div>
    """))
    other_form = _write(tmp_path, "job_b_form.html", textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <button type="submit">Submit Application</button>
        </form>
    """))
    return f"{original}?job_id=1234", f"{other_form}?job_id=9999"
