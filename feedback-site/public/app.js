/* ── AppWay Feedback · app.js ── */
(function () {
    'use strict';

    /* ------------------------------------------------------------------ */
    /* Config — replaced at runtime by CloudFront → this will be set via   */
    /* a small config.js injected by deploy.sh, or by a window global set  */
    /* in index.html after deploy. For local preview this is empty string. */
    /* ------------------------------------------------------------------ */
    var API_URL = window.FEEDBACK_API_URL || '';

    /* ------------------------------------------------------------------ */
    /* DOM refs                                                             */
    /* ------------------------------------------------------------------ */
    var form = document.getElementById('feedbackForm');
    var submitBtn = document.getElementById('submitBtn');
    var btnText = document.getElementById('btnText');
    var btnSpinner = document.getElementById('btnSpinner');
    var formError = document.getElementById('formError');
    var successPanel = document.getElementById('successPanel');
    var charCount = document.getElementById('charCount');
    var feedbackTA = document.getElementById('feedback');

    /* ------------------------------------------------------------------ */
    /* Character counter                                                    */
    /* ------------------------------------------------------------------ */
    feedbackTA.addEventListener('input', function () {
        charCount.textContent = feedbackTA.value.length;
    });

    /* ------------------------------------------------------------------ */
    /* Validation helpers                                                   */
    /* ------------------------------------------------------------------ */
    var EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

    function setErr(id, msg) {
        var el = document.getElementById('err_' + id);
        if (el) el.textContent = msg;
        var inp = document.getElementById(id);
        if (inp) inp.classList.toggle('invalid', msg !== '');
    }

    function clearErrors() {
        ['first_name', 'last_name', 'email', 'feedback'].forEach(function (f) {
            setErr(f, '');
        });
        formError.style.display = 'none';
        formError.textContent = '';
    }

    function validate(data) {
        var ok = true;
        if (!data.first_name) { setErr('first_name', 'First name is required.'); ok = false; }
        if (!data.last_name) { setErr('last_name', 'Surname is required.'); ok = false; }
        if (!data.email || !EMAIL_RE.test(data.email)) {
            setErr('email', 'Please enter a valid email address.'); ok = false;
        }
        if (!data.feedback || data.feedback.length < 5) {
            setErr('feedback', 'Feedback must be at least 5 characters.'); ok = false;
        }
        return ok;
    }

    /* ------------------------------------------------------------------ */
    /* Form submit                                                          */
    /* ------------------------------------------------------------------ */
    form.addEventListener('submit', function (e) {
        e.preventDefault();
        clearErrors();

        var fd = new FormData(form);

        /* honeypot check */
        if (fd.get('website')) return;

        var ratingInput = form.querySelector('input[name="rating"]:checked');

        var data = {
            first_name: (fd.get('first_name') || '').trim(),
            last_name: (fd.get('last_name') || '').trim(),
            email: (fd.get('email') || '').trim(),
            phone: (fd.get('phone') || '').trim() || null,
            rating: ratingInput ? parseInt(ratingInput.value, 10) : null,
            feedback: (fd.get('feedback') || '').trim(),
        };

        if (!validate(data)) return;

        /* If no API URL configured (local preview), show success immediately */
        if (!API_URL) {
            showSuccess();
            return;
        }

        setLoading(true);

        fetch(API_URL + '/feedback', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        })
            .then(function (res) {
                return res.json().then(function (body) {
                    return { status: res.status, body: body };
                });
            })
            .then(function (r) {
                if (r.status === 200 && r.body.ok) {
                    showSuccess();
                } else {
                    var msg = (r.body && r.body.error) || 'An error occurred. Please try again.';
                    showFormError(msg);
                }
            })
            .catch(function () {
                showFormError('Network error. Please check your connection and try again.');
            })
            .finally(function () {
                setLoading(false);
            });
    });

    /* ------------------------------------------------------------------ */
    /* UI state helpers                                                     */
    /* ------------------------------------------------------------------ */
    function setLoading(on) {
        submitBtn.disabled = on;
        btnText.style.display = on ? 'none' : '';
        btnSpinner.style.display = on ? '' : 'none';
    }

    function showFormError(msg) {
        formError.textContent = msg;
        formError.style.display = '';
        formError.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    function showSuccess() {
        form.style.display = 'none';
        successPanel.style.display = '';
        successPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
})();
