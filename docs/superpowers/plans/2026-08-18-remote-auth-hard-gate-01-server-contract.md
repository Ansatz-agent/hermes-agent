# Hermes Remote Auth Server Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a machine-readable Django Session endpoint with absolute non-sliding expiry while preserving administrator-only account distribution and all existing owner-scoped history behavior.

**Architecture:** Keep the existing Django LoginView/LogoutView and Cookie contract. A custom LoginView stamps an absolute expiry datetime into the Session, a read-only endpoint validates that timestamp without extending or mutating the Session, and a shared decorator applies the same absolute-expiry rule to every client-facing history/memory view while excluding Django Admin. Public account lifecycle routes remain absent, while Django Admin remains the only account-management surface.

**Tech Stack:** Django 5.2.17, django-axes 8.3.1, SQLite, uv, Podman Compose, Django TestCase.

---

## File map

- Create: `/opt/agent-history-portal/history/auth_views.py` — custom LoginView and strict Session JSON endpoint.
- Create: `/opt/agent-history-portal/history/tests/test_client_session_api.py` — login, expiry, schema, route-absence, and non-renewal behavior.
- Modify: `/opt/agent-history-portal/config/settings.py` — fixed absolute Session lifetime setting.
- Modify: `/opt/agent-history-portal/config/urls.py` — use the custom LoginView and expose `api/session/`.
- Modify: `/opt/agent-history-portal/history/views.py` — replace client-facing `login_required` decorators with the absolute Session decorator.
- Modify: `/opt/agent-history-portal/history/tests/test_admin_auth.py` — complete administrator-only lifecycle regression set.
- Modify: `/opt/agent-history-portal/OPERATIONS.md` — backup, deploy, smoke, rollback, and administrator reset notification procedure.

### Task 1: Establish a recoverable server baseline

- [ ] **Step 1: Prove the source is unversioned and `.env` is ignored**

Run:

```bash
ssh root@121.37.182.49 'cd /opt/agent-history-portal && test ! -d .git && grep -Fx .env .gitignore && test "$(stat -c %a .env)" = 600'
```

Expected on the first run: `.env` is printed, all checks exit `0`, and no Git repository exists yet. This is a one-time characterization step; if a prior attempt already created `.git`, do not rerun initialization—resume from Step 4 and verify the recorded baseline instead.

- [ ] **Step 2: Create a database backup and verify it before source changes**

Run:

```bash
ssh root@121.37.182.49 'cd /opt/agent-history-portal && ./scripts/backup.sh && latest=$(find /var/backups/agent-history -maxdepth 1 -type f -name "db-*.sqlite3" -printf "%T@ %p\n" | sort -nr | head -1 | cut -d" " -f2-) && ./scripts/restore-verify.sh "$latest"'
```

Expected: backup and restore verification both succeed and print the verified backup path.

- [ ] **Step 3: Initialize a private local Git history on the server**

Run:

```bash
ssh root@121.37.182.49 'cd /opt/agent-history-portal && git init -b main && git config user.name "Hermes Deployment" && git config user.email "deployment@localhost" && git add . && git commit -m "chore: capture deployed portal baseline" && git switch -c feature/hermes-client-session-api'
```

Expected: the baseline commit succeeds, `.env` is absent from `git ls-files`, and the active branch is `feature/hermes-client-session-api`.

- [ ] **Step 4: Record the backup and baseline commit outside Git**

Run:

```bash
ssh root@121.37.182.49 'cd /opt/agent-history-portal && git rev-parse HEAD && git status --short && test -z "$(git ls-files .env)"'
```

Expected: one commit hash and an empty status. Save the hash and verified backup path in the implementation transcript.

### Task 2: Specify the Session endpoint and absolute expiry

- [ ] **Step 1: Write the failing endpoint tests**

Create `/opt/agent-history-portal/history/tests/test_client_session_api.py` with:

```python
from datetime import datetime, timedelta

from axes.models import AccessAttempt
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone


@override_settings(HERMES_SESSION_ABSOLUTE_AGE_SECONDS=3600)
class ClientSessionApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="alice", password="safe-test-pass-1"
        )

    def login(self):
        response = self.client.post(
            reverse("login"),
            {"username": "alice", "password": "safe-test-pass-1"},
        )
        self.assertEqual(response.status_code, 302)

    def test_anonymous_response_is_strict_401_json(self):
        response = self.client.get(reverse("client-session"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"authenticated": False})

    def test_login_sets_absolute_expiry_and_authenticated_schema(self):
        before = timezone.now()
        self.login()
        response = self.client.get(reverse("client-session"))
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(body), {
            "authenticated", "username", "server_time", "session_expires_at"
        })
        self.assertIs(body["authenticated"], True)
        self.assertEqual(body["username"], "alice")
        expires = datetime.fromisoformat(body["session_expires_at"])
        self.assertGreaterEqual(expires, before + timedelta(minutes=59))

    def test_status_checks_do_not_slide_expiry(self):
        self.login()
        first = self.client.get(reverse("client-session")).json()
        second = self.client.get(reverse("client-session")).json()
        self.assertEqual(first["session_expires_at"], second["session_expires_at"])

    def test_status_checks_do_not_increment_axes_after_failed_login(self):
        self.client.post(
            reverse("login"),
            {"username": "alice", "password": "wrong-password"},
        )
        before = AccessAttempt.objects.count()
        self.assertGreater(before, 0)
        for _ in range(3):
            self.client.get(reverse("client-session"))
        self.assertEqual(AccessAttempt.objects.count(), before)

    def test_login_rotates_session_key(self):
        session = self.client.session
        session["pre_login"] = True
        session.save()
        before = session.session_key
        self.login()
        self.assertNotEqual(self.client.session.session_key, before)

    def test_expired_absolute_timestamp_is_rejected(self):
        self.login()
        session = self.client.session
        session["hermes_absolute_session_expires_at"] = (
            timezone.now() - timedelta(seconds=1)
        ).isoformat()
        session.save()
        response = self.client.get(reverse("client-session"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"authenticated": False})

    def test_missing_absolute_timestamp_is_rejected(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("client-session"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"authenticated": False})

    def test_rejected_status_is_read_only_and_does_not_logout_admin(self):
        admin = get_user_model().objects.create_superuser(
            username="server-admin", password="safe-admin-pass-1"
        )
        self.client.force_login(admin)
        session_key = self.client.session.session_key
        self.assertEqual(self.client.get(reverse("client-session")).status_code, 401)
        self.assertEqual(self.client.session.session_key, session_key)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)

    def test_expired_absolute_session_cannot_read_memory_or_history(self):
        self.login()
        session = self.client.session
        session["hermes_absolute_session_expires_at"] = (
            timezone.now() - timedelta(seconds=1)
        ).isoformat()
        session.save()
        for name in ("history:memory-pool", "history:session-list"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 302)
```

- [ ] **Step 2: Run the test and verify the red state**

Run on the server:

```bash
cd /opt/agent-history-portal && uv run python manage.py test history.tests.test_client_session_api
```

Expected: FAIL because `client-session` is not registered and the login view does not stamp the absolute expiry.

- [ ] **Step 3: Add the fixed lifetime setting**

Append to `config/settings.py` next to the Cookie settings:

```python
HERMES_SESSION_ABSOLUTE_AGE_SECONDS = int(
    os.getenv("HERMES_SESSION_ABSOLUTE_AGE_SECONDS", str(14 * 24 * 60 * 60))
)
if HERMES_SESSION_ABSOLUTE_AGE_SECONDS < 300:
    raise ImproperlyConfigured(
        "HERMES_SESSION_ABSOLUTE_AGE_SECONDS must be at least 300"
    )
```

- [ ] **Step 4: Implement the custom LoginView and endpoint**

Create `history/auth_views.py` with:

```python
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, redirect_to_login
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET

ABSOLUTE_EXPIRY_KEY = "hermes_absolute_session_expires_at"


class HermesLoginView(LoginView):
    def form_valid(self, form):
        response = super().form_valid(form)
        expires_at = timezone.now() + timedelta(
            seconds=settings.HERMES_SESSION_ABSOLUTE_AGE_SECONDS
        )
        self.request.session[ABSOLUTE_EXPIRY_KEY] = expires_at.isoformat()
        self.request.session.set_expiry(expires_at)
        return response


def _absolute_expiry(request):
    value = request.session.get(ABSOLUTE_EXPIRY_KEY)
    return parse_datetime(value) if isinstance(value, str) else None


def has_valid_absolute_session(request):
    expires_at = _absolute_expiry(request)
    return expires_at is not None and expires_at > timezone.now()


def hermes_session_required(view):
    @wraps(view)
    def absolute_checked(request, *args, **kwargs):
        if not has_valid_absolute_session(request):
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        return view(request, *args, **kwargs)

    return login_required(absolute_checked)


def _reject():
    response = JsonResponse({"authenticated": False}, status=401)
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def client_session(request):
    if not request.user.is_authenticated:
        return _reject()
    expires_at = _absolute_expiry(request)
    if expires_at is None or expires_at <= timezone.now():
        return _reject()
    response = JsonResponse({
        "authenticated": True,
        "username": request.user.get_username(),
        "server_time": timezone.now().isoformat(),
        "session_expires_at": expires_at.isoformat(),
    })
    response["Cache-Control"] = "no-store"
    return response
```

Django 5.2.17's `SessionBase.set_expiry(datetime)` converts the datetime to an ISO string before the default JSON serializer saves the Session; the deployed container source was checked explicitly. Keep the datetime form so the framework Cookie/session expiry and `ABSOLUTE_EXPIRY_KEY` share the same fixed instant.

- [ ] **Step 5: Apply absolute expiry to client-facing history and memory views**

In `history/views.py`, replace the import of Django's `login_required` with:

```python
from .auth_views import hermes_session_required
```

Replace every `@login_required` that currently exists in `history/views.py` with `@hermes_session_required`. Leave `healthz` and every view that is currently public unchanged; do not expand the protected-view set by guessing from URL names. Do not apply this decorator to `admin_site`; Django Admin retains its separate administrator Session and never requires the Hermes client timestamp.

- [ ] **Step 6: Register only the fixed login, logout, and session routes**

Read the current `config/urls.py`, replace only the existing `accounts/login/` view with `HermesLoginView`, add only `path("api/session/", client_session, name="client-session")`, and retain every unrelated existing route in its current order. The resulting auth imports and three auth routes are:

```python
from django.contrib.auth import views as auth_views
from django.urls import include, path

from history.admin import admin_site
from history.auth_views import HermesLoginView, client_session

path("accounts/login/", HermesLoginView.as_view(), name="login"),
path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
path("api/session/", client_session, name="client-session"),
```

Confirm the file does not include `django.contrib.auth.urls`; do not replace the whole `urlpatterns` list.

- [ ] **Step 7: Run the endpoint and existing auth tests**

Run:

```bash
cd /opt/agent-history-portal && uv run python manage.py test history.tests.test_client_session_api history.tests.test_admin_auth history.tests.test_access_control
```

Expected: all tests pass; repeated status calls return the identical `session_expires_at`.

- [ ] **Step 8: Commit the endpoint**

```bash
git add config/settings.py config/urls.py history/auth_views.py history/views.py history/tests/test_client_session_api.py
git commit -m "feat: add absolute client session endpoint"
```

### Task 3: Lock account lifecycle to server administrators

- [ ] **Step 1: Add negative-route coverage**

Add to `history/tests/test_admin_auth.py`:

```python
    def test_public_account_lifecycle_routes_do_not_exist(self):
        paths = [
            "/signup/",
            "/register/",
            "/accounts/signup/",
            "/accounts/register/",
            "/accounts/password_reset/",
            "/accounts/password_reset/done/",
            "/accounts/password_change/",
            "/accounts/password_change/done/",
            "/accounts/reset/test-user/test-token/",
            "/accounts/reset/done/",
            "/accounts/invite/",
            "/accounts/invitation/",
            "/api/accounts/",
            "/api/signup/",
            "/api/invitations/",
            "/api/password-reset/",
            "/api/password-change/",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
                self.assertEqual(self.client.post(path).status_code, 404)

    def test_client_session_endpoint_never_exposes_account_actions(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["hermes_absolute_session_expires_at"] = (
            timezone.now() + timedelta(hours=1)
        ).isoformat()
        session.save()
        body = self.client.get(reverse("client-session")).json()
        self.assertNotIn("signup", body)
        self.assertNotIn("password_reset", body)
        self.assertNotIn("invitation", body)
```

Also add:

```python
from datetime import timedelta
from django.utils import timezone
```

- [ ] **Step 2: Run the account lifecycle tests**

Run:

```bash
cd /opt/agent-history-portal && uv run python manage.py test history.tests.test_admin_auth
```

Expected: PASS with the explicit URL table; including `django.contrib.auth.urls` later makes this regression fail.

- [ ] **Step 3: Verify existing memory and history endpoints still require Session auth**

Run:

```bash
cd /opt/agent-history-portal && uv run python manage.py test history.tests.test_memory_pool history.tests.test_access_control
```

Expected: all anonymous memory/history access remains rejected and owner isolation remains green.

- [ ] **Step 4: Document administrator reset notification**

Add this policy to `OPERATIONS.md` under “门户账号”:

```markdown
### Hermes 客户端账户分发

- 账户只由超级管理员在 `/agent/admin/` 创建、停用或重置密码。
- 不启用公开注册、邀请、密码重置或密码修改 URL。
- 管理员创建账户或重置密码后，通过已核验的带外渠道交付初始密码；不得写入 Git、命令参数、服务器日志或无访问控制的聊天/工单。
- 用户首次成功登录后仍由管理员负责后续重置；Hermes 客户端不提供账户生命周期操作。
```

- [ ] **Step 5: Commit the lifecycle contract**

```bash
git add history/tests/test_admin_auth.py OPERATIONS.md
git commit -m "test: lock account lifecycle to server admins"
```

### Task 4: Deploy and prove rollback

- [ ] **Step 1: Run the complete server suite and lint**

Run:

```bash
cd /opt/agent-history-portal && uv run python manage.py test && uv run ruff check .
```

Expected: zero test failures and zero Ruff errors.

- [ ] **Step 2: Build without replacing the live container**

Run:

```bash
cd /opt/agent-history-portal && podman-compose build web
```

Expected: a new `agent-history-web` image builds successfully.

- [ ] **Step 3: Record the pre-deploy image and create a fresh database backup**

Run:

```bash
podman inspect agent-history-web --format '{{.Image}}'
cd /opt/agent-history-portal && ./scripts/backup.sh
```

Expected: one old image ID and one new verified backup path are captured.

- [ ] **Step 4: Restart through the existing systemd unit**

Run:

```bash
systemctl restart agent-history-portal.service
systemctl --no-pager --full status agent-history-portal.service
```

Expected: unit is active and the container health check becomes healthy.

- [ ] **Step 5: Have an administrator create the disposable smoke-test account**

From `/agent/admin/`, a server superuser creates one uniquely labelled, non-staff, non-superuser account solely for this deployment check. Generate and transfer its initial password through the approved protected channel; never place it in a command argument, transcript, Git, log, or general-purpose chat. Record only the non-identifying test-account label and the responsible administrator.

- [ ] **Step 6: Run anonymous and authenticated HTTPS smoke checks**

Run anonymous checks:

```bash
curl -sS -D- https://c2sml.cn/agent/api/session/
curl -sS -o /dev/null -w '%{http_code}\n' https://c2sml.cn/agent/accounts/password_reset/
```

Expected: first response is `401 application/json` with `{"authenticated":false}`; second prints `404`.

Use a temporary Cookie jar and the administrator-created non-admin account through the documented CSRF form flow, then verify the authenticated endpoint returns exactly four keys and no Cookie value. Credentials must enter through an interactive prompt or protected stdin, never argv or shell history.

- [ ] **Step 7: Disable or delete the disposable account**

Immediately after the smoke checks, the server administrator disables or deletes the temporary account through `/agent/admin/`, confirms that its existing Session now receives `401`, and destroys the temporary Cookie jar. The client gains no endpoint for this lifecycle action.

- [ ] **Step 8: Prove rollback inputs are recorded**

Run:

```bash
cd /opt/agent-history-portal
git branch --show-current
git log --oneline -3
git status --short
```

Expected: `feature/hermes-client-session-api`, the baseline and two feature commits, and an empty status. The implementation transcript must also contain the pre-deploy image ID and verified database backup path; a healthy deployment does not execute rollback.
