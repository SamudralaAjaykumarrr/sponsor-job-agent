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


# =============================================================================
# Workday/SmartRecruiters/Workable browser-assist hardening (2026-08-22):
# a Workday-style multi-step wizard with genuine client-side (JS) inline
# validation blocking Next on an empty required field, and a Workable-style
# (real, separately-loaded) 2-step form -- exercising the new
# app.applications.dynamic_validation detection and the generic multi-step
# engine against a Workable-shaped flow, since the one real Workable tenant
# found live (apply.workable.com/flosum) was single-page.
# =============================================================================

def workday_like_dynamic_validation_wizard_page(tmp_path: Path) -> str:
    """A single-page (client-side-only) 2-step wizard: clicking Next with
    the required 'Full Name' field empty injects a genuine
    `role="alert"` inline validation error and does NOT advance (no route
    change, no field-set change -- the error element itself is not an
    input/textarea/select, so it never counts as a field-set change).
    Filling the field first and clicking Next DOES advance (a real
    `history.pushState` route change plus a swapped-in step-2 field).

    The step transition replaces only the `#app-root` container's
    innerHTML, never `document.body.innerHTML` -- a real SPA's step
    transition re-renders its own root component, it does not also wipe
    out page-level `<head>`-equivalent metadata like the embedded
    JobPosting JSON-LD block. An earlier version of this fixture replaced
    the whole body (destroying the JSON-LD script tag on every step
    transition), which made `app.applications.job_identity`'s multi-signal
    check correctly but incidentally see zero comparable signals on step 2
    and pause `JOB_IDENTITY_UNVERIFIED` -- not a bug in the identity gate
    or in `browser_runtime`, but an unrealistic fixture destroying its own
    identity evidence on a step change that a genuine SPA would not."""
    return _write(tmp_path, "wd_dynamic_validation.html", _jsonld_block() + textwrap.dedent("""
        <div id="app-root">
          <div class="progress-indicator">Step 1 of 2</div>
          <form id="step1">
            <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
            <div id="error-slot"></div>
            <button type="button" id="next-btn">Next</button>
          </form>
        </div>
        <script>
          document.getElementById('next-btn').addEventListener('click', function () {
            var val = document.getElementById('fname').value.trim();
            var errorSlot = document.getElementById('error-slot');
            if (!val) {
              errorSlot.innerHTML = '<div role="alert" class="error">Full Name is required</div>';
              return;
            }
            errorSlot.innerHTML = '';
            history.pushState({}, '', '#step2');
            document.getElementById('app-root').innerHTML = (
              '<div class="progress-indicator">Step 2 of 2</div>' +
              '<form id="step2">' +
              '<label for="resume">Resume/CV</label><input id="resume" name="resume" type="file" required>' +
              '<button type="submit">Submit Application</button>' +
              '</form>'
            );
          });
        </script>
    """))


def workable_like_multistep_page(tmp_path: Path) -> tuple[str, str]:
    """Two real, separately-loaded pages (mirroring multi_step_pages()'s own
    real-navigation shape, closer to how a genuinely multi-step Workable
    account would behave) with Workable-shaped field labels -- the one real
    Workable tenant reached live (apply.workable.com/flosum) was single-page,
    so this exercises the SAME generic multi-step engine against a
    Workable-styled 2-step flow instead. Returns (page1_url, page2_url)."""
    page2 = _write(tmp_path, "workable_step2.html", _jsonld_block() + textwrap.dedent("""
        <form>
          <label for="linkedin">LinkedIn Profile</label><input id="linkedin" name="linkedin_url" type="text">
          <label for="salary">Desired Salary</label><input id="salary" name="salary_expectation" type="text">
          <label for="resume">Resume/CV</label><input id="resume" name="resume" type="file" required>
          <button type="submit">Submit Application</button>
        </form>
    """))
    page1 = _write(tmp_path, "workable_step1.html", textwrap.dedent(f"""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text" required>
          <label for="mail">Email</label><input id="mail" name="email" type="text" required>
          <label for="phone">Phone</label><input id="phone" name="phone" type="text">
          <button type="button" onclick="window.location.href='{page2}'">Next</button>
        </form>
    """))
    return page1, page2


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


# =============================================================================
# Real Provider Execution V1: realistic, LOCAL, deterministic Greenhouse- and
# Lever-shaped fixtures.
#
# Field NAMES and LABELS are modeled on each provider's genuine, already
# documented shapes recorded elsewhere in this repository -- Greenhouse's
# from the real `boards-api.greenhouse.io/...?questions=true` payload captured
# in tests/test_applications_providers_greenhouse.py::FIXTURE_PAYLOAD
# (first_name/last_name/email/phone/resume/question_N/disability_status),
# Lever's from the real rendered form live-verified in Phase 10-12 and
# described in app.applications.browser_capability_matrix's lever row
# (name/email/phone/org/urls[...]/resume/comments/cards[...]).
#
# These are `file://` pages like every other fixture in this module: no real
# employer is ever contacted, no network access is required, and nothing here
# is ever submitted anywhere.
# =============================================================================

_GREENHOUSE_STANDARD_FIELDS = """
  <label for="first_name">First Name</label><input id="first_name" name="first_name" type="text" required>
  <label for="last_name">Last Name</label><input id="last_name" name="last_name" type="text" required>
  <label for="email">Email</label><input id="email" name="email" type="text" required>
  <label for="phone">Phone</label><input id="phone" name="phone" type="text">
  <label for="resume">Resume/CV</label><input id="resume" name="resume" type="file" required>
"""

_LEVER_STANDARD_FIELDS = """
  <label for="name">Full name</label><input id="name" name="name" type="text" required>
  <label for="email">Email</label><input id="email" name="email" type="text" required>
  <label for="phone">Phone</label><input id="phone" name="phone" type="text">
  <label for="org">Current company</label><input id="org" name="org" type="text">
  <label for="linkedin">LinkedIn URL</label><input id="linkedin" name="urls[LinkedIn]" type="text">
  <label for="resume">Resume</label><input id="resume" name="resume" type="file" required>
"""

_GREENHOUSE_SPONSORSHIP_QUESTION = """
  <fieldset>
    <legend>Will you now or in the future require sponsorship for a visa to remain in your current location?</legend>
    <label><input type="radio" name="question_47110" value="Yes"> Yes</label>
    <label><input type="radio" name="question_47110" value="No"> No</label>
  </fieldset>
"""

_LEVER_SPONSORSHIP_QUESTION = """
  <fieldset>
    <legend>Will you now or in the future require sponsorship?</legend>
    <label><input type="radio" name="cards[a1b2c3][field0]" value="Yes"> Yes</label>
    <label><input type="radio" name="cards[a1b2c3][field0]" value="No"> No</label>
  </fieldset>
"""

_GREENHOUSE_UNKNOWN_QUESTION = """
  <label for="gh_unknown">Which internal Acme initiative most closely matches your background?</label>
  <input id="gh_unknown" name="question_99001" type="text" required>
"""

_LEVER_UNKNOWN_QUESTION = """
  <label for="lever_unknown">Describe a time you disagreed with our published engineering values.</label>
  <textarea id="lever_unknown" name="cards[a1b2c3][field9]" required></textarea>
"""

_GREENHOUSE_COVER_LETTER = """
  <label for="cover_letter">Cover Letter</label><input id="cover_letter" name="cover_letter" type="file">
"""

_LEVER_COVER_LETTER = """
  <label for="comments">Additional information / cover letter</label>
  <textarea id="comments" name="comments"></textarea>
  <label for="lever_cover">Cover letter</label><input id="lever_cover" name="cover_letter" type="file">
"""

# Workday + Ashby Provider Execution V1: field shapes modeled on each real
# provider's genuine label conventions (Workday's "Legal Name" wizard step,
# Ashby's system fields), even though neither provider's real DOM field
# *names* are guessable without live-opening a real tenant/board (the
# normalized form model matches on LABEL text, exactly like the Lever fixture
# above -- the field `name` attribute is never load-bearing for matching).
_WORKDAY_STANDARD_FIELDS = """
  <label for="firstName">Legal Name - First Name</label>
  <input id="firstName" name="legalName--firstName" type="text" required>
  <label for="lastName">Legal Name - Last Name</label>
  <input id="lastName" name="legalName--lastName" type="text" required>
  <label for="email">Email Address</label><input id="email" name="email" type="text" required>
  <label for="phone">Phone Number</label><input id="phone" name="phoneNumber" type="text">
  <label for="resume">Resume/CV</label><input id="resume" name="resumeAttachment" type="file" required>
"""

_ASHBY_STANDARD_FIELDS = """
  <label for="name">Full name</label><input id="name" name="_systemfield_name" type="text" required>
  <label for="email">Email</label><input id="email" name="_systemfield_email" type="text" required>
  <label for="phone">Phone</label><input id="phone" name="phone" type="text">
  <label for="linkedin">LinkedIn URL</label><input id="linkedin" name="field_linkedin" type="text">
  <label for="resume">Resume</label><input id="resume" name="_systemfield_resume" type="file" required>
"""

_WORKDAY_SPONSORSHIP_QUESTION = """
  <fieldset>
    <legend>Are you legally authorized to work in the country in which this position is based, and will you
    now or in the future require sponsorship for employment visa status?</legend>
    <label><input type="radio" name="sponsorshipRequired" value="Yes"> Yes</label>
    <label><input type="radio" name="sponsorshipRequired" value="No"> No</label>
  </fieldset>
"""

_ASHBY_SPONSORSHIP_QUESTION = """
  <fieldset>
    <legend>Will you now or in the future require visa sponsorship to work in the United States?</legend>
    <label><input type="radio" name="field_sponsorship" value="Yes"> Yes</label>
    <label><input type="radio" name="field_sponsorship" value="No"> No</label>
  </fieldset>
"""

_WORKDAY_UNKNOWN_QUESTION = """
  <label for="wd_unknown">What is your expected start date and how many hours can you commit weekly?</label>
  <input id="wd_unknown" name="q_unknown_workday" type="text" required>
"""

_ASHBY_UNKNOWN_QUESTION = """
  <label for="ashby_unknown">What's a project you're most proud of and why?</label>
  <textarea id="ashby_unknown" name="field_unknown_ashby" required></textarea>
"""

_WORKDAY_COVER_LETTER = """
  <label for="cover_letter">Cover Letter (optional)</label>
  <input id="cover_letter" name="coverLetterAttachment" type="file">
"""

_ASHBY_COVER_LETTER = """
  <label for="ashby_cover">Cover Letter</label><input id="ashby_cover" name="_systemfield_coverletter" type="file">
"""


def _provider_form_page(
    tmp_path: Path, name: str, standard: str, *, sponsorship: str = "", unknown: str = "",
    cover_letter: str = "", extra_head: str = "", title: str = DEFAULT_JOB_TITLE,
    company: str = DEFAULT_JOB_COMPANY, submit_label: str = "Submit Application",
) -> str:
    body = _jsonld_block(title, company) + extra_head + textwrap.dedent(f"""
        <h1>{title}</h1>
        <form id="application-form">
          {standard}
          {cover_letter}
          {sponsorship}
          {unknown}
          <button type="submit">{submit_label}</button>
        </form>
    """)
    return _write(tmp_path, name, body)


# --- Greenhouse-shaped -------------------------------------------------------

def greenhouse_like_application_page(
    tmp_path: Path, *, with_cover_letter: bool = True, with_unknown_question: bool = False,
    title: str = DEFAULT_JOB_TITLE, company: str = DEFAULT_JOB_COMPANY,
) -> str:
    """A complete Greenhouse-shaped application form: the standard
    name/email/phone/resume block, a genuine sponsorship radio GROUP (whose
    question text lives in the fieldset legend, exactly like the real one --
    CLAUDE.md's Phase 10 rule about legend-over-choice-label priority),
    optionally a cover-letter upload, and optionally an employer-specific
    question this project has no verified answer for."""
    return _provider_form_page(
        tmp_path, "greenhouse_form.html", _GREENHOUSE_STANDARD_FIELDS,
        sponsorship=_GREENHOUSE_SPONSORSHIP_QUESTION,
        unknown=_GREENHOUSE_UNKNOWN_QUESTION if with_unknown_question else "",
        cover_letter=_GREENHOUSE_COVER_LETTER if with_cover_letter else "",
        title=title, company=company,
    )


def greenhouse_like_form_changed_page(tmp_path: Path) -> str:
    """The SAME posting's form after the employer changed it -- one field
    added, one removed. Written to a DIFFERENT file so a test can open the
    first, then navigate to this one, producing a genuinely different field
    fingerprint (the PAUSED_FORM_CHANGED / stale-authorization case)."""
    changed_standard = _GREENHOUSE_STANDARD_FIELDS.replace(
        '<label for="phone">Phone</label><input id="phone" name="phone" type="text">',
        '<label for="pronouns">Pronouns</label><input id="pronouns" name="question_50002" type="text">',
    )
    return _provider_form_page(
        tmp_path, "greenhouse_form_changed.html", changed_standard,
        sponsorship=_GREENHOUSE_SPONSORSHIP_QUESTION,
    )


def greenhouse_like_captcha_page(tmp_path: Path) -> str:
    """A Greenhouse-shaped form behind a genuinely RENDERED CAPTCHA widget --
    a real `g-recaptcha` element, not merely a referenced script tag (the
    exact false positive CLAUDE.md's Phase 13 rules require this project's
    DOM-element-based detection to avoid)."""
    return _write(tmp_path, "greenhouse_captcha.html", _jsonld_block() + textwrap.dedent(f"""
        <h1>{DEFAULT_JOB_TITLE}</h1>
        <form>
          {_GREENHOUSE_STANDARD_FIELDS}
          <div class="g-recaptcha" data-sitekey="local-fixture-key">Verify you are human.</div>
          <button type="submit">Submit Application</button>
        </form>
    """))


def greenhouse_like_login_page(tmp_path: Path) -> str:
    """A Greenhouse-shaped candidate sign-in wall reached instead of the
    form."""
    return _write(tmp_path, "greenhouse_login.html", textwrap.dedent("""
        <h1>Sign in to continue your application</h1>
        <form>
          <label for="gh_user">Email</label><input id="gh_user" name="email" type="text">
          <label for="gh_pass">Password</label><input id="gh_pass" name="password" type="password">
          <button type="submit">Sign In</button>
        </form>
    """))


def greenhouse_like_otp_page(tmp_path: Path) -> str:
    """A Greenhouse-shaped one-time-passcode challenge -- distinct from the
    plain sign-in wall above, exercising the MFA phrase-detection path
    (`app.applications.browser_runtime._MFA_PHRASES`) rather than the
    `input[type=password]` heuristic. Added by Workday + Ashby Provider
    Execution V1 to prove the generic engine's MFA detection is genuinely
    provider-agnostic, not just added for the two new providers."""
    return _write(tmp_path, "greenhouse_otp.html", textwrap.dedent("""
        <h1>Verify Your Identity</h1>
        <p>Enter the one-time code we sent to your email to continue.</p>
        <form>
          <label for="gh_otp">Verification code</label><input id="gh_otp" name="otp" type="text">
          <button type="submit">Verify</button>
        </form>
    """))


def greenhouse_like_expired_page(tmp_path: Path) -> str:
    """The page a closed Greenhouse posting shows: no form at all, and no
    apply control -- the "job expired" terminal case."""
    return _write(tmp_path, "greenhouse_expired.html", textwrap.dedent("""
        <div>
          <h1>This job is no longer accepting applications</h1>
          <p>The position you are looking for has been closed. Browse our other openings.</p>
        </div>
    """))


def greenhouse_like_confirmation_page(tmp_path: Path) -> str:
    """A Greenhouse-shaped post-submission confirmation page, carrying both a
    trusted success phrase and a confirmation id (the STRONG-evidence
    case)."""
    return _write(tmp_path, "greenhouse_confirmation.html", textwrap.dedent("""
        <div>
          <h1>Thank you for applying to Acme Corp</h1>
          <p>Your application has been submitted. Confirmation Number: GH-2026-88134</p>
        </div>
    """))


# --- Lever-shaped ------------------------------------------------------------

def lever_like_application_page(
    tmp_path: Path, *, with_cover_letter: bool = True, with_unknown_question: bool = False,
    title: str = DEFAULT_JOB_TITLE, company: str = DEFAULT_JOB_COMPANY,
) -> str:
    """A complete Lever-shaped application form. Lever's real DOM uses
    `urls[...]`/`cards[...][fieldN]` input names, which is precisely why the
    normalized form model must map on LABEL text rather than on a provider's
    field-name convention."""
    return _provider_form_page(
        tmp_path, "lever_form.html", _LEVER_STANDARD_FIELDS,
        sponsorship=_LEVER_SPONSORSHIP_QUESTION,
        unknown=_LEVER_UNKNOWN_QUESTION if with_unknown_question else "",
        cover_letter=_LEVER_COVER_LETTER if with_cover_letter else "",
        title=title, company=company,
    )


def lever_like_form_changed_page(tmp_path: Path) -> str:
    changed_standard = _LEVER_STANDARD_FIELDS.replace(
        '<label for="org">Current company</label><input id="org" name="org" type="text">',
        '<label for="github">GitHub URL</label><input id="github" name="urls[GitHub]" type="text">',
    )
    return _provider_form_page(
        tmp_path, "lever_form_changed.html", changed_standard, sponsorship=_LEVER_SPONSORSHIP_QUESTION,
    )


def lever_like_captcha_page(tmp_path: Path) -> str:
    return _write(tmp_path, "lever_captcha.html", _jsonld_block() + textwrap.dedent(f"""
        <h1>{DEFAULT_JOB_TITLE}</h1>
        <form>
          {_LEVER_STANDARD_FIELDS}
          <iframe id="hcaptcha-frame" src="about:blank" class="h-captcha captcha-widget"
                  style="width:300px;height:80px;"></iframe>
          <button type="submit">Submit Application</button>
        </form>
    """))


def lever_like_login_page(tmp_path: Path) -> str:
    return _write(tmp_path, "lever_login.html", textwrap.dedent("""
        <h1>Sign in to apply</h1>
        <form>
          <label for="lv_user">Email</label><input id="lv_user" name="email" type="text">
          <label for="lv_pass">Password</label><input id="lv_pass" name="password" type="password">
          <button type="submit">Sign In</button>
        </form>
    """))


def lever_like_otp_page(tmp_path: Path) -> str:
    """A Lever-shaped one-time-passcode challenge -- see
    greenhouse_like_otp_page's docstring."""
    return _write(tmp_path, "lever_otp.html", textwrap.dedent("""
        <h1>Two-Factor Authentication</h1>
        <p>Enter your authentication code to continue.</p>
        <form>
          <label for="lv_otp">Authentication code</label><input id="lv_otp" name="otp" type="text">
          <button type="submit">Verify</button>
        </form>
    """))


def lever_like_expired_page(tmp_path: Path) -> str:
    return _write(tmp_path, "lever_expired.html", textwrap.dedent("""
        <div>
          <h1>Job not found</h1>
          <p>This posting is no longer available. See all open roles.</p>
        </div>
    """))


def lever_like_confirmation_page(tmp_path: Path) -> str:
    """A Lever-shaped confirmation page with a trusted phrase but NO
    confirmation id -- the MODERATE-evidence case, deliberately distinct
    from the Greenhouse fixture above so both grades are exercised.

    The filename deliberately avoids every substring in
    `app.applications.confirmation_evidence._URL_CONFIRMATION_HINTS`
    ("thank"/"confirm"/"success"/"received"/"complete"): the `file://` URL
    IS the page's URL, and a hint in it would supply a second corroborating
    signal and silently upgrade this fixture to STRONG, defeating its whole
    purpose."""
    return _write(tmp_path, "lever_applied.html", textwrap.dedent("""
        <div>
          <h1>Application received</h1>
          <p>We have received your application and will be in touch.</p>
        </div>
    """))


# --- Workday-shaped ----------------------------------------------------------

def workday_like_application_page(
    tmp_path: Path, *, with_cover_letter: bool = True, with_unknown_question: bool = False,
    title: str = DEFAULT_JOB_TITLE, company: str = DEFAULT_JOB_COMPANY,
) -> str:
    """A complete Workday-shaped application form: the standard legal-name/
    email/phone/resume block, a genuine sponsorship radio GROUP whose
    question text lives in the fieldset legend, optionally a cover-letter
    upload, and optionally an employer-specific question this project has no
    verified answer for."""
    return _provider_form_page(
        tmp_path, "workday_form.html", _WORKDAY_STANDARD_FIELDS,
        sponsorship=_WORKDAY_SPONSORSHIP_QUESTION,
        unknown=_WORKDAY_UNKNOWN_QUESTION if with_unknown_question else "",
        cover_letter=_WORKDAY_COVER_LETTER if with_cover_letter else "",
        title=title, company=company,
    )


def workday_like_form_changed_page(tmp_path: Path) -> str:
    """The SAME requisition's form after the tenant changed it -- one field
    replaced -- producing a genuinely different field fingerprint (the
    PAUSED_FORM_CHANGED / stale-authorization case)."""
    changed_standard = _WORKDAY_STANDARD_FIELDS.replace(
        '<label for="phone">Phone Number</label><input id="phone" name="phoneNumber" type="text">',
        '<label for="location_pref">Preferred Location</label>'
        '<input id="location_pref" name="locationPreference" type="text">',
    )
    return _provider_form_page(
        tmp_path, "workday_form_changed.html", changed_standard, sponsorship=_WORKDAY_SPONSORSHIP_QUESTION,
    )


def workday_like_captcha_page(tmp_path: Path) -> str:
    """A Workday-shaped form behind a genuinely RENDERED CAPTCHA widget -- a
    real element carrying "captcha" in its class, not merely a referenced
    script tag."""
    return _write(tmp_path, "workday_captcha.html", _jsonld_block() + textwrap.dedent(f"""
        <h1>{DEFAULT_JOB_TITLE}</h1>
        <form>
          {_WORKDAY_STANDARD_FIELDS}
          <div class="g-recaptcha" data-sitekey="local-fixture-key">Verify you are human.</div>
          <button type="submit">Submit Application</button>
        </form>
    """))


def workday_like_login_page(tmp_path: Path) -> str:
    """A Workday-shaped candidate account sign-in wall reached instead of the
    form (Workday's real apply flow commonly requires an account)."""
    return _write(tmp_path, "workday_login.html", textwrap.dedent("""
        <h1>Sign In to Apply</h1>
        <form>
          <label for="wd_user">Email</label><input id="wd_user" name="email" type="text">
          <label for="wd_pass">Password</label><input id="wd_pass" name="password" type="password">
          <button type="submit">Sign In</button>
        </form>
    """))


def workday_like_otp_page(tmp_path: Path) -> str:
    """A Workday-shaped one-time-passcode challenge -- distinct from the
    plain sign-in wall above, exercising the MFA phrase-detection path
    (`app.applications.browser_runtime._MFA_PHRASES`) rather than the
    `input[type=password]` heuristic."""
    return _write(tmp_path, "workday_otp.html", textwrap.dedent("""
        <h1>Verify Your Identity</h1>
        <p>Enter the one-time code we sent to your email to continue your application.</p>
        <form>
          <label for="wd_otp">Verification code</label><input id="wd_otp" name="otp" type="text">
          <button type="submit">Verify</button>
        </form>
    """))


def workday_like_expired_page(tmp_path: Path) -> str:
    """The page a closed Workday requisition shows: no form at all, and no
    apply control -- the "job expired" terminal case."""
    return _write(tmp_path, "workday_expired.html", textwrap.dedent("""
        <div>
          <h1>This requisition is no longer accepting applications</h1>
          <p>The position you are looking for is closed. Search our other open roles.</p>
        </div>
    """))


def workday_like_confirmation_page(tmp_path: Path) -> str:
    """A Workday-shaped post-submission confirmation page, carrying both a
    trusted success phrase and a confirmation id (the STRONG-evidence
    case)."""
    return _write(tmp_path, "workday_confirmation.html", textwrap.dedent("""
        <div>
          <h1>Thank you for applying to Acme Corp</h1>
          <p>Your application has been submitted. Confirmation Number: WD-2026-77201</p>
        </div>
    """))


# --- Ashby-shaped --------------------------------------------------------------

def ashby_like_application_page(
    tmp_path: Path, *, with_cover_letter: bool = True, with_unknown_question: bool = False,
    title: str = DEFAULT_JOB_TITLE, company: str = DEFAULT_JOB_COMPANY,
) -> str:
    """A complete Ashby-shaped application form: the standard name/email/
    phone/LinkedIn/resume block, a genuine sponsorship radio GROUP whose
    question text lives in the fieldset legend, optionally a cover-letter
    upload, and optionally an employer-specific question this project has no
    verified answer for."""
    return _provider_form_page(
        tmp_path, "ashby_form.html", _ASHBY_STANDARD_FIELDS,
        sponsorship=_ASHBY_SPONSORSHIP_QUESTION,
        unknown=_ASHBY_UNKNOWN_QUESTION if with_unknown_question else "",
        cover_letter=_ASHBY_COVER_LETTER if with_cover_letter else "",
        title=title, company=company,
    )


def ashby_like_form_changed_page(tmp_path: Path) -> str:
    changed_standard = _ASHBY_STANDARD_FIELDS.replace(
        '<label for="linkedin">LinkedIn URL</label><input id="linkedin" name="field_linkedin" type="text">',
        '<label for="portfolio">Portfolio URL</label><input id="portfolio" name="field_portfolio" type="text">',
    )
    return _provider_form_page(
        tmp_path, "ashby_form_changed.html", changed_standard, sponsorship=_ASHBY_SPONSORSHIP_QUESTION,
    )


def ashby_like_captcha_page(tmp_path: Path) -> str:
    return _write(tmp_path, "ashby_captcha.html", _jsonld_block() + textwrap.dedent(f"""
        <h1>{DEFAULT_JOB_TITLE}</h1>
        <form>
          {_ASHBY_STANDARD_FIELDS}
          <div class="g-recaptcha" data-sitekey="local-fixture-key">Verify you are human.</div>
          <button type="submit">Submit Application</button>
        </form>
    """))


def ashby_like_login_page(tmp_path: Path) -> str:
    return _write(tmp_path, "ashby_login.html", textwrap.dedent("""
        <h1>Sign in to continue</h1>
        <form>
          <label for="ab_user">Email</label><input id="ab_user" name="email" type="text">
          <label for="ab_pass">Password</label><input id="ab_pass" name="password" type="password">
          <button type="submit">Sign In</button>
        </form>
    """))


def ashby_like_otp_page(tmp_path: Path) -> str:
    """An Ashby-shaped one-time-passcode challenge -- see
    workday_like_otp_page's docstring."""
    return _write(tmp_path, "ashby_otp.html", textwrap.dedent("""
        <h1>Two-Factor Authentication</h1>
        <p>Enter your authentication code to continue.</p>
        <form>
          <label for="ab_otp">Authentication code</label><input id="ab_otp" name="otp" type="text">
          <button type="submit">Verify</button>
        </form>
    """))


def ashby_like_expired_page(tmp_path: Path) -> str:
    return _write(tmp_path, "ashby_expired.html", textwrap.dedent("""
        <div>
          <h1>This job is no longer available</h1>
          <p>This posting has closed. View all open positions.</p>
        </div>
    """))


def ashby_like_confirmation_page(tmp_path: Path) -> str:
    """An Ashby-shaped post-submission confirmation page, carrying both a
    trusted success phrase and a confirmation id (the STRONG-evidence
    case)."""
    return _write(tmp_path, "ashby_confirmation.html", textwrap.dedent("""
        <div>
          <h1>Thank you for applying to Acme Corp</h1>
          <p>Your application has been submitted. Confirmation Number: AB-2026-31940</p>
        </div>
    """))


_STANDARD_FIELDS_BY_PROVIDER = {
    "greenhouse": lambda: _GREENHOUSE_STANDARD_FIELDS,
    "lever": lambda: _LEVER_STANDARD_FIELDS,
    "workday": lambda: _WORKDAY_STANDARD_FIELDS,
    "ashby": lambda: _ASHBY_STANDARD_FIELDS,
}


def provider_like_identity_mismatch_pages(tmp_path: Path, *, provider: str) -> tuple[str, str]:
    """Two genuinely different requisitions on the same provider: the session
    is opened against the first, and the live page ends up on the second.
    Returns (session_url, other_job_url), both carrying a distinct
    requisition-shaped `job_id` query token so
    `app.applications.job_identity.verify_job_identity` can CONFIDENTLY
    extract one from each side (its whole design is to stay UNVERIFIABLE
    rather than guess when it cannot)."""
    other = _provider_form_page(
        tmp_path, f"{provider}_other_requisition.html",
        _STANDARD_FIELDS_BY_PROVIDER[provider](),
        title="Frontend Software Engineer", company="Globex Industries",
    )
    session_page = _write(tmp_path, f"{provider}_session_landing.html", textwrap.dedent(f"""
        <div><h1>{DEFAULT_JOB_TITLE}</h1><p>Job description for the requisition this session opened.</p></div>
    """))
    return f"{session_page}?job_id=778811", f"{other}?job_id=990022"
